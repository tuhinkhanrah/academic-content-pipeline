#!/usr/bin/env python3
"""
ai_communicators.py - Pluggable AI Communication Backends.

Modes:
  1. ContextChatBackend   : Multi-turn Chat Session with rolling memory_span pruning.
  2. AgentSessionBackend  : Managed Agent Session with persistent environment_id.
  3. RemoteSandboxBackend : Remote Execution Sandbox with GCS staging & persistent output.
"""

import os
import io
import base64
import re
import sys
import time
import json
import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image
from google import genai
from google.genai import types

logger = logging.getLogger("academic_content_pipeline")


class BaseAICommunicator(ABC):
    """Abstract Base Class for all AI Communication Backends."""

    def __init__(
        self,
        client: Optional[genai.Client] = None,
        model_name: str = "gemini-3.5-flash",
        temperature: float = 0.1,
        verbose: bool = False,
    ):
        self.client = client or genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self.model_name = model_name
        self.temperature = temperature
        self.verbose = verbose

    @abstractmethod
    def generate(
        self,
        system_instruction: str,
        contents: List[Any],
        **kwargs,
    ) -> str:
        """Sends content to the model/agent and returns raw generated response text."""
        pass

    def close(self) -> None:
        """Optional cleanup lifecycle hook for communicators."""
        pass

    @staticmethod
    def strip_code_fences(text: str) -> str:
        """Extracts content from markdown code fences if present."""
        match = re.search(r"```(?:xml|html|tex|latex|json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()


# =======================================================================
# 1. Mode: With Context (Rolling Chat Session)
# =======================================================================

class ContextChatBackend(BaseAICommunicator):
    """
    Manages a stateful Gemini Chat Session with a configurable sliding window (memory_span).
    Prunes older messages when the history exceeds 2 * memory_span messages.
    """

    def __init__(
        self,
        client: Optional[genai.Client] = None,
        model_name: str = "gemini-3.5-flash",
        temperature: float = 0.1,
        memory_span: int = 3,
        attempt_limit: int = 5,
        retry_delay: float = 4.0,
        verbose: bool = False,
    ):
        super().__init__(client=client, model_name=model_name, temperature=temperature, verbose=verbose)
        self.memory_span = memory_span
        self.attempt_limit = attempt_limit
        self.retry_delay = retry_delay
        self.chat_session: Optional[Any] = None
        self.active_system_instruction: Optional[str] = None

    def _build_chat_config(self, system_instruction: str) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=self.temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _prune_history_if_needed(self):
        """Trims chat history to retain only the last `memory_span` user-model turns."""
        if not self.chat_session or self.memory_span <= 0:
            return

        try:
            history = self.chat_session.get_history()
            max_messages = self.memory_span * 2
            if len(history) > max_messages:
                trimmed = history[-max_messages:]
                while trimmed and getattr(trimmed[0], "role", "") != "user":
                    trimmed.pop(0)

                self.chat_session = self.client.chats.create(
                    model=self.model_name,
                    history=trimmed,
                    config=self._build_chat_config(self.active_system_instruction or ""),
                )
                logger.info(f"🔄 Pruned chat history to last {len(trimmed)} messages.")
        except Exception as e:
            logger.warning(f"Failed to prune chat history: {e}")

    def generate(
        self,
        system_instruction: str,
        contents: List[Any],
        reset_session: bool = False,
        **kwargs,
    ) -> str:
        if reset_session or not self.chat_session or self.active_system_instruction != system_instruction:
            self.active_system_instruction = system_instruction
            self.chat_session = self.client.chats.create(
                model=self.model_name,
                config=self._build_chat_config(system_instruction),
            )

        self._prune_history_if_needed()

        for attempt in range(1, self.attempt_limit + 1):
            try:
                logger.info(f"💬 Chat generation (attempt {attempt}/{self.attempt_limit})...")
                response = self.chat_session.send_message(message=contents)
                if response and response.text:
                    return self.strip_code_fences(response.text)
                logger.warning(f"Empty response received on attempt {attempt}.")
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "503" in err_str or "Quota exceeded" in err_str:
                    match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    suggested_delay = float(match.group(1)) + 2.0 if match else max(self.retry_delay * (2 ** (attempt - 1)), 25.0)
                    logger.warning(f"⚠️ Rate/Quota limit hit. Sleeping {suggested_delay:.1f}s before retry (attempt {attempt}/{self.attempt_limit})...")
                    time.sleep(suggested_delay)
                elif attempt < self.attempt_limit:
                    logger.warning(f"Chat API error (attempt {attempt}/{self.attempt_limit}): {e}")
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

        raise RuntimeError("Chat generation failed after reaching max attempts.")


# =======================================================================
# 2. Mode: With Agent (Managed Environment Session)
# =======================================================================

class AgentSessionBackend(BaseAICommunicator):
    """
    Manages a Gemini Agent Session that maintains state across interactions
    via a persistent environment_id.
    """

    def __init__(
        self,
        client: Optional[genai.Client] = None,
        agent_name: str = "antigravity-preview-05-2026",
        agent_type: str = "antigravity",
        model_name: str = "gemini-3.6-flash",
        attempt_limit: int = 5,
        retry_delay: float = 10.0,
        context_reset_interval: int = 7,
        environment_id: Optional[str] = None,
        verbose: bool = False,
    ):
        super().__init__(client=client, model_name=model_name, verbose=verbose)
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.attempt_limit = attempt_limit
        self.retry_delay = retry_delay
        self.context_reset_interval = context_reset_interval
        self.environment_id = environment_id
        self.last_interaction_id: Optional[str] = None
        self.turn_counter = 0

    def generate(
        self,
        system_instruction: str,
        contents: List[Any],
        reset_session: bool = False,
        **kwargs,
    ) -> str:
        self.turn_counter += 1

        if reset_session or (self.turn_counter > 1 and (self.turn_counter - 1) % self.context_reset_interval == 0):
            logger.info("🔄 Resetting agent turn history to keep session lean...")
            self.last_interaction_id = None

        text_parts = []
        multimodal_input = []

        for item in contents:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, Image.Image):
                buf = io.BytesIO()
                item.save(buf, format="PNG")
                b64_img = base64.b64encode(buf.getvalue()).decode("utf-8")
                multimodal_input.append({"type": "image", "data": b64_img, "mime_type": "image/png"})
            elif isinstance(item, Path) and item.exists():
                b64_img = base64.b64encode(item.read_bytes()).decode("utf-8")
                multimodal_input.append({"type": "image", "data": b64_img, "mime_type": "image/png"})

        user_text = "\n\n".join(text_parts)
        multimodal_input.append({"type": "text", "text": user_text})

        agent_config_payload = {
            "type": self.agent_type,
            "model": self.model_name,
        }

        for attempt in range(1, self.attempt_limit + 1):
            try:
                logger.info(f"🤖 Agent interaction (attempt {attempt}/{self.attempt_limit}, env: {self.environment_id or 'remote'})...")

                env_param = self.environment_id if self.environment_id else "remote"
                interaction_params: Dict[str, Any] = {
                    "agent": self.agent_name,
                    "agent_config": agent_config_payload,
                    "environment": env_param,
                    "system_instruction": system_instruction,
                    "input": multimodal_input,
                }

                if self.last_interaction_id:
                    interaction_params["previous_interaction_id"] = self.last_interaction_id

                interaction = self.client.interactions.create(**interaction_params)

                self.environment_id = getattr(interaction, "environment_id", self.environment_id)
                self.last_interaction_id = getattr(interaction, "id", self.last_interaction_id)

                if self.verbose:
                    self._log_interaction(interaction)

                output_text = getattr(interaction, "output_text", "")
                if not output_text and hasattr(interaction, "outputs") and interaction.outputs:
                    output_text = "\n".join([str(o) for o in interaction.outputs])

                if output_text:
                    return self.strip_code_fences(output_text)

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "503" in err_str or "Quota exceeded" in err_str:
                    match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    suggested_delay = float(match.group(1)) + 2.0 if match else max(self.retry_delay * (2 ** (attempt - 1)), 35.0)
                    logger.warning(f"⚠️ Rate/Quota limit hit. Sleeping {suggested_delay:.1f}s before retry (attempt {attempt}/{self.attempt_limit})...")
                    time.sleep(suggested_delay)
                elif attempt < self.attempt_limit:
                    logger.warning(f"Agent API error (attempt {attempt}/{self.attempt_limit}): {e}")
                    time.sleep(self.retry_delay)
                else:
                    raise

        raise RuntimeError("Agent interaction failed after max attempts.")

    def _log_interaction(self, interaction) -> None:
        steps = getattr(interaction, "steps", []) or []
        for step in steps:
            step_type = getattr(step, "type", "")
            summary = getattr(step, "summary", None)
            if summary:
                logger.info(f"  🧠 [Agent Step ({step_type})]: {summary}")

    def close(self) -> None:
        """Tears down the remote environment sandbox to release quotas and resources."""
        if self.environment_id:
            try:
                logger.info(f"🧹 Tearing down remote agent environment: {self.environment_id}...")
                if hasattr(self.client, "environments") and hasattr(self.client.environments, "delete"):
                    self.client.environments.delete(name=self.environment_id)
                elif hasattr(self.client, "agents") and hasattr(self.client.agents, "environments"):
                    self.client.agents.environments.delete(name=self.environment_id)
            except Exception as e:
                logger.debug(f"Environment cleanup notice: {e}")
            finally:
                self.environment_id = None
                self.last_interaction_id = None


# =======================================================================
# 3. Mode: With Remote Agent (Remote Sandbox + GCS Staging)
# =======================================================================

class RemoteSandboxBackend(BaseAICommunicator):
    """
    Executes in a Google Cloud remote sandbox environment.
    Uses GCS for staging and persistent artifact transfer.
    Keeps remote Python execution minimal (fetch, generate text, upload).
    """

    def __init__(
        self,
        client: Optional[genai.Client] = None,
        bucket_name: Optional[str] = None,
        agent_name: str = "antigravity-preview-05-2026",
        attempt_limit: int = 3,
        retry_delay: float = 15.0,
        verbose: bool = False,
    ):
        super().__init__(client=client, verbose=verbose)
        self.bucket_name = bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if not self.bucket_name:
            raise ValueError("GCS bucket name must be provided via --bucket-name or GCS_BUCKET_NAME env.")
        self.agent_name = agent_name
        self.attempt_limit = attempt_limit
        self.retry_delay = retry_delay
        self.last_environment_id: Optional[str] = None

    @staticmethod
    def get_gcloud_access_token() -> str:
        """Fetches active GCP OAuth access token from local gcloud CLI."""
        try:
            return subprocess.check_output(
                ["gcloud", "auth", "print-access-token"], text=True
            ).strip()
        except subprocess.CalledProcessError:
            raise RuntimeError("Failed to fetch GCP access token. Ensure 'gcloud auth login' is completed.")

    def upload_to_gcs(self, file_path: Path, prefix: str = "processing_queue") -> str:
        """Uploads a local file to GCS if not already present."""
        safe_filename = file_path.name.replace(" ", "_")
        gcs_target_uri = f"gs://{self.bucket_name}/{prefix}/{safe_filename}"

        try:
            subprocess.run(
                ["gcloud", "storage", "ls", gcs_target_uri],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"⚡ {file_path.name} already in GCS. Skipping upload.")
            return gcs_target_uri
        except subprocess.CalledProcessError:
            pass

        logger.info(f"🔄 Uploading {file_path.name} -> {gcs_target_uri}...")
        subprocess.run(["gcloud", "storage", "cp", str(file_path), gcs_target_uri], check=True)
        return gcs_target_uri

    def download_from_gcs(self, gcs_path: str, local_path: Path) -> None:
        """Downloads a result file from GCS."""
        gcs_uri = f"gs://{self.bucket_name}/{gcs_path}"
        logger.info(f"📥 Downloading result: {gcs_uri} -> {local_path}...")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gcloud", "storage", "cp", gcs_uri, str(local_path)], check=True)

    def generate(
        self,
        system_instruction: str,
        contents: List[Any],
        output_filename: str = "output_artifact.xml",
        **kwargs,
    ) -> str:
        """
        Runs remote sandbox execution.
        Uploads generated artifact to GCS and downloads to local destination.
        """
        gcp_token = self.get_gcloud_access_token()
        api_key = os.environ.get("GEMINI_API_KEY")
        gcs_upload_path = f"output/{output_filename}"
        bucket_upload_url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket_name}/o"
            f"?uploadType=media&name={gcs_upload_path}"
        )

        prompt_body = "\n\n".join([str(c) for c in contents if isinstance(c, str)])

        # Minimal Python execution code in remote sandbox (Requirement 8)
        execution_prompt = f"""
{prompt_body}

### 📋 EXECUTION & Persist Instructions:
Generate the complete content for `{output_filename}` adhering strictly to the system rules.
Save the file to `/workspace/{output_filename}` and upload it to GCS using this minimal Python script:

```python
import requests
with open('/workspace/{output_filename}', 'rb') as f:
    data = f.read()
resp = requests.post(
    '{bucket_upload_url}',
    headers={{'Content-Type': 'text/plain', 'Authorization': 'Bearer {gcp_token}'}},
    data=data
)
print('GCS Status:', resp.status_code)
if resp.status_code not in [200, 201]:
    raise RuntimeError('GCS Upload failed: ' + resp.text)
```
"""

        logger.info("🚀 Provisioning remote execution sandbox...")
        for attempt in range(1, self.attempt_limit + 1):
            try:
                interaction = self.client.interactions.create(
                    agent=self.agent_name,
                    input=execution_prompt,
                    environment={
                        "type": "remote",
                        "sources": [
                            {
                                "type": "inline",
                                "target": ".agents/AGENTS.md",
                                "content": system_instruction,
                            }
                        ],
                        "network": {
                            "allowlist": [
                                {
                                    "domain": "storage.googleapis.com",
                                    "transform": {"Authorization": f"Bearer {gcp_token}"},
                                },
                                {"domain": "*"},
                            ]
                        },
                        "env": {
                            "GEMINI_API_KEY": api_key,
                        },
                    },
                )

                if self.verbose:
                    logger.info("\n🔍 Agent Finished Execution. Output:\n" + "=" * 60)
                    logger.info(interaction.output_text)
                    logger.info("=" * 60 + "\n")

                self.last_environment_id = getattr(interaction, "environment_id", None)

                local_dest = Path("extracted_data") / "remote_downloads" / output_filename
                self.download_from_gcs(gcs_upload_path, local_dest)
                if local_dest.exists():
                    return local_dest.read_text(encoding="utf-8")
                return getattr(interaction, "output_text", "")

            except Exception as e:
                logger.warning(f"⚠️ Remote Sandbox Error (Attempt {attempt}/{self.attempt_limit}): {e}")
                if attempt < self.attempt_limit:
                    time.sleep(self.retry_delay)
                else:
                    raise

        raise RuntimeError("Remote sandbox execution failed.")

    def close(self) -> None:
        """Tears down the remote sandbox environment created during execution."""
        if self.last_environment_id:
            try:
                logger.info(f"🧹 Tearing down remote sandbox environment: {self.last_environment_id}...")
                if hasattr(self.client, "environments") and hasattr(self.client.environments, "delete"):
                    self.client.environments.delete(name=self.last_environment_id)
                elif hasattr(self.client, "agents") and hasattr(self.client.agents, "environments"):
                    self.client.agents.environments.delete(name=self.last_environment_id)
            except Exception as e:
                logger.debug(f"Remote environment cleanup notice: {e}")
            finally:
                self.last_environment_id = None
