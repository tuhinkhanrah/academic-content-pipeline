import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import httpx
from google import genai
from google.genai import types

from academic_content_pipeline.ai_communicators import (
    AgentSessionBackend, BatchAPIBackend, ContextChatBackend, ImageAttachment,
    MultimodalBatch, RemoteSandboxBackend, SingleShotBackend,
)


class InteractionLoggingTests(unittest.TestCase):
    def test_precount_serializes_through_real_sdk_developer_api(self):
        requests = []

        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"totalTokens": 42})

        client = genai.Client(
            api_key="test",
            http_options=types.HttpOptions(client_args={"transport": httpx.MockTransport(transport)}),
        )
        backend = AgentSessionBackend(client=client, model_name="test", attempt_limit=1)
        with patch.object(client.interactions, "create", return_value=SimpleNamespace(
            output_text="answer", usage=SimpleNamespace(total_input_tokens=43)
        )), self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            backend.generate(
                "rules", MultimodalBatch(text="prompt", images=[
                    ImageAttachment("fig", Image.new("RGB", (2, 2)))
                ]), purpose="SDK test"
            )
        self.assertEqual(requests[0]["contents"][0]["parts"][0]["text"], "rules")
        self.assertIn("inlineData", requests[0]["contents"][1]["parts"][0])
        self.assertEqual(requests[0]["contents"][1]["parts"][1]["text"], "prompt")
        self.assertIn("42 tokens", "\n".join(logs.output))

    def test_single_shot_counts_prompt_and_logs_actual_usage(self):
        requests = []
        models = SimpleNamespace(
            count_tokens=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(total_tokens=101),
            generate_content=lambda **kwargs: SimpleNamespace(
                text="answer", usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=103, candidates_token_count=7, total_token_count=115,
                    thoughts_token_count=5, cached_content_token_count=0,
                )
            ),
        )
        backend = SingleShotBackend(client=SimpleNamespace(models=models), model_name="model", attempt_limit=1)
        with tempfile.TemporaryDirectory() as temporary_directory:
            image = Path(temporary_directory) / "diagram.png"
            Image.new("RGB", (2, 2)).save(image)
            batch = MultimodalBatch(text="prompt", images=[ImageAttachment("fig", image)])
            with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
                self.assertEqual(backend.generate("rules", batch, purpose="paper"), "answer")
            requests[0]["contents"][1][2].close()
        self.assertEqual(requests[0]["model"], "model")
        self.assertEqual(requests[0]["contents"][0], ["rules"])
        self.assertEqual(requests[0]["contents"][1][0], "prompt")
        self.assertIsInstance(requests[0]["contents"][1][2], Image.Image)
        messages = "\n".join(logs.output)
        self.assertIn("101 tokens", messages)
        self.assertIn("input=103, output=7, total=115, thinking=5, cached=0", messages)
        self.assertIn("tool_input=unavailable", messages)

    def test_chat_counts_retained_history_and_marks_missing_usage(self):
        counts = []
        history = [types.Content(role="user", parts=[types.Part.from_text(text="previous")])]
        chat = SimpleNamespace(
            get_history=lambda: history,
            send_message=lambda **kwargs: SimpleNamespace(text="answer"),
        )
        client = SimpleNamespace(
            chats=SimpleNamespace(create=lambda **kwargs: chat),
            models=SimpleNamespace(count_tokens=lambda **kwargs: counts.append(kwargs) or SimpleNamespace(total_tokens=42)),
        )
        backend = ContextChatBackend(client=client, attempt_limit=1)
        with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            backend.generate("rules", "new", purpose="chat")
        self.assertEqual(counts[0]["contents"], [["rules"]] + history + [["new"]])
        self.assertIn("input=unavailable, output=unavailable, total=unavailable", "\n".join(logs.output))

    def test_agent_counts_images_and_reports_interaction_usage(self):
        counts = []
        requests = []
        usage = SimpleNamespace(
            total_input_tokens=20, total_output_tokens=3, total_tokens=29,
            total_thought_tokens=4, total_cached_tokens=2, total_tool_use_tokens=2,
        )
        client = SimpleNamespace(
            models=SimpleNamespace(count_tokens=lambda **kwargs: counts.append(kwargs) or SimpleNamespace(total_tokens=18)),
            interactions=SimpleNamespace(create=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(
                output_text="result", environment_id="env", id="turn", usage=usage
            )),
        )
        backend = AgentSessionBackend(client=client, model_name="model", attempt_limit=1)
        image = Image.new("RGB", (2, 2))
        batch = MultimodalBatch(text="prompt", images=[ImageAttachment("fig", image)])
        with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            backend.generate("rules", batch, purpose="reference")
            backend.generate("rules", batch, purpose="next")
        self.assertEqual(counts[0]["contents"][0], ["rules"])
        self.assertEqual(len(counts[1]["contents"]), 1)
        self.assertEqual(requests[1]["previous_interaction_id"], "turn")
        self.assertIsNotNone(counts[0]["contents"][1][0].inline_data.data)
        self.assertEqual(counts[0]["contents"][1][1], "prompt")
        messages = "\n".join(logs.output)
        self.assertIn("agent history, tools and internal instructions excluded", messages)
        self.assertIn("input=20, output=3, total=29, thinking=4, cached=2, tool_input=2", messages)

    def test_count_failure_is_explicit_and_does_not_prevent_generation(self):
        def count(**kwargs):
            raise ValueError("count unavailable")
        client = SimpleNamespace(
            models=SimpleNamespace(count_tokens=count, generate_content=lambda **kwargs: SimpleNamespace(
                text="answer", usage_metadata=None
            ))
        )
        backend = SingleShotBackend(client=client, attempt_limit=1)
        with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            self.assertEqual(backend.generate("rules", "prompt", purpose="test"), "answer")
        messages = "\n".join(logs.output)
        self.assertIn("unavailable (ValueError: count unavailable)", messages)
        self.assertIn("input=unavailable", messages)

    def test_batch_reads_nested_dictionary_usage(self):
        inline = {"response": {
            "candidates": [{"content": {"parts": [{"text": "result"}]}}],
            "usageMetadata": {"promptTokenCount": 13, "candidatesTokenCount": 4, "totalTokenCount": 17},
        }}
        batches = SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(name="job"),
            get=lambda **kwargs: SimpleNamespace(state="JOB_STATE_SUCCEEDED", dest=SimpleNamespace(inlined_responses=[inline])),
            delete=lambda **kwargs: SimpleNamespace(done=True),
        )
        client = SimpleNamespace(
            batches=batches,
            models=SimpleNamespace(count_tokens=lambda **kwargs: SimpleNamespace(total_tokens=12)),
        )
        backend = BatchAPIBackend(client=client, attempt_limit=1)
        with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            self.assertEqual(backend.generate("rules", "request", purpose="batch"), "result")
        self.assertIn("input=13, output=4, total=17", "\n".join(logs.output))

    def test_remote_marks_precount_unavailable_and_logs_provider_usage(self):
        interaction = SimpleNamespace(
            output_text="result", environment_id="env",
            usage={"total_input_tokens": 10, "total_output_tokens": 5, "total_tokens": 17},
        )
        backend = RemoteSandboxBackend(
            client=SimpleNamespace(interactions=SimpleNamespace(create=lambda **kwargs: interaction)),
            bucket_name="bucket", attempt_limit=1,
        )
        with patch.object(backend, "get_gcloud_access_token", return_value="token"), \
             patch.object(backend, "download_from_gcs"), \
             self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            self.assertEqual(backend.generate("rules", "request", output_filename="not-present-unit-test.xml"), "result")
        messages = "\n".join(logs.output)
        self.assertIn("remote agent model is not specified", messages)
        self.assertIn("input=10, output=5, total=17", messages)

    def test_agent_logs_purpose_for_each_attempt_without_sending_metadata(self):
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                raise RuntimeError("temporary error")
            return SimpleNamespace(
                output_text="result", environment_id="env-1", id="turn-1"
            )

        client = SimpleNamespace(interactions=SimpleNamespace(create=create))
        backend = AgentSessionBackend(client=client, attempt_limit=2, retry_delay=0)
        with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
            response = backend.generate(
                system_instruction="instructions",
                contents="request",
                purpose="plan chapter question allocations for Physics",
            )
        self.assertEqual(response, "result")
        messages = "\n".join(logs.output)
        self.assertIn("plan chapter question allocations for Physics (attempt 1/2", messages)
        self.assertIn("plan chapter question allocations for Physics (attempt 2/2", messages)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all("purpose" not in request for request in requests))


if __name__ == "__main__":
    unittest.main()
