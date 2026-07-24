#!/usr/bin/env python3
"""
Prompt Cache Manager for pdf2moodle-qbank

Handles SHA256 fingerprinting of system prompt markdown files, tracks active
caches in `.cache_registry.json`, and automatically invalidates outdated remote
prompt caches on Google's GenAI servers when local markdown files are edited.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger("prompt_cache_manager")
REGISTRY_FILE = Path(".cache_registry.json")


def _load_registry() -> dict:
    """Loads the local cache registry file if present."""
    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_registry(data: dict) -> None:
    """Saves updated cache mappings to the local JSON registry."""
    try:
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save cache registry: {e}")


def get_or_create_prompt_cache(
    client: genai.Client,
    prompt_path: Path,
    prompt_text: str,
    model_name: str,
    ttl_seconds: int = 86400,
    force_refresh: bool = False,
    enable_code_execution: bool = False,
) -> Optional[str]:
    """
    Returns a valid Gemini CachedContent resource name for the given system prompt.
    Automatically invalidates and recreates the cache if the local file hash or tools change.
    """
    prompt_path = Path(prompt_path)
    
    # Hash includes code_execution state so changing flags automatically refreshes cache
    hash_payload = f"{prompt_text}__code_exec={enable_code_execution}"
    current_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
    
    registry = _load_registry()
    str_path = str(prompt_path.resolve())
    cached_info = registry.get(str_path, {})

    old_cache_name = cached_info.get("cache_name")
    old_hash = cached_info.get("hash")

    needs_recreation = force_refresh or (current_hash != old_hash) or not old_cache_name

    if not needs_recreation and old_cache_name:
        try:
            client.caches.get(name=old_cache_name)
            logger.info(f"⚡ [PROMPT CACHE HIT] Reusing active prompt cache: {old_cache_name}")
            return old_cache_name
        except Exception:
            logger.warning("⚠️ Remote prompt cache expired or deleted. Recreating...")
            needs_recreation = True

    if old_cache_name:
        try:
            logger.info(f"🗑️ Deleting outdated prompt cache: {old_cache_name}")
            client.caches.delete(name=old_cache_name)
        except Exception as e:
            logger.debug(f"Cache cleanup info: {e}")

    safe_display_name = f"prompt-{re.sub(r'[^a-zA-Z0-9_-]', '-', prompt_path.stem)}"

    logger.info(f"📦 [PROMPT CACHE CREATING] Uploading system prompt '{prompt_path.name}' to Gemini Context Cache...")

    # Embed tools directly into CreateCachedContentConfig if enabled
    tools = None
    if enable_code_execution:
        logger.info("🛠️ Enabling Python Code Execution inside Cached Content...")
        tools = [types.Tool(code_execution=types.ToolCodeExecution())]

    cache_config = types.CreateCachedContentConfig(
        system_instruction=prompt_text,
        display_name=safe_display_name,
        ttl=f"{ttl_seconds}s",
        tools=tools,
    )

    try:
        new_cache = client.caches.create(
            model=model_name,
            config=cache_config
        )
        registry[str_path] = {
            "hash": current_hash,
            "cache_name": new_cache.name,
            "model": model_name,
            "code_execution": enable_code_execution,
        }
        _save_registry(registry)
        logger.info(f"✅ System prompt cached successfully! Resource Name: {new_cache.name}")
        return new_cache.name
    except Exception as e:
        logger.error(f"Failed to create prompt cache on Google servers: {e}")
        return None