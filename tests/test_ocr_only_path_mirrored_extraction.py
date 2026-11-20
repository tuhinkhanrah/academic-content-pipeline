import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_ocr_only_path_mirrored_extraction as runner


class OCRProgressTests(unittest.TestCase):
    def test_existing_output_is_reported_as_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            pdf = source / "paper.pdf"
            pdf.write_bytes(b"pdf")
            output_root = root / "output"
            output_root.mkdir()
            (output_root / "paper.xml").write_text("existing", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(runner.main([
                    "--source-root", str(source), "--output-root", str(output_root),
                ]), 0)
            self.assertIn(f"Skipping {pdf}: output already exists", output.getvalue())

    def test_sequential_reports_before_and_after_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            pdf = source / "paper.pdf"
            pdf.write_bytes(b"pdf")
            output = StringIO()
            engine = Mock()

            def process_pdf(**kwargs):
                self.assertIn(f"Processing {pdf}", output.getvalue())
                self.assertNotIn(f"Processed {pdf}", output.getvalue())
                return SimpleNamespace(full_markdown="text", pages=[1])

            engine.process_pdf.side_effect = process_pdf
            with patch.dict("os.environ", {"MISTRAL_API_KEY": "test"}), \
                    patch.object(runner, "MistralOCREngine", return_value=engine), \
                    redirect_stdout(output):
                self.assertEqual(runner.main([
                    "--source-root", str(source), "--output-root", str(root / "output"),
                ]), 0)
            self.assertIn(f"Processed {pdf}", output.getvalue())
            self.assertTrue((root / "output" / "paper.xml").exists())

    def test_batch_reports_cache_upload_status_and_saved_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            cached = source / "cached.pdf"
            pending = source / "pending.pdf"
            for pdf in (cached, pending):
                pdf.write_bytes(b"pdf")
            engine = Mock()
            engine.load_cached_result.side_effect = [
                SimpleNamespace(full_markdown="cached", pages=[1]),
                None,
            ]
            client = Mock()
            client.files.upload.side_effect = [
                SimpleNamespace(id="pdf-id"), SimpleNamespace(id="batch-input"),
            ]
            client.files.get_signed_url.return_value.url = "https://example.test/pdf"
            client.batch.jobs.create.return_value.id = "batch-id"
            client.batch.jobs.get.return_value = SimpleNamespace(
                status="SUCCESS", completed_requests=1, total_requests=1, output_file="result-id",
            )
            client.files.download.return_value = json.dumps({
                "custom_id": "pending.pdf",
                "response": {"status_code": 200, "body": {"pages": [{"markdown": "result"}]}},
            }).encode()
            output = StringIO()
            with patch.dict("os.environ", {"MISTRAL_API_KEY": "test"}), \
                    patch.object(runner, "MistralOCREngine", return_value=engine), \
                    patch.object(runner, "Mistral", return_value=client), \
                    redirect_stdout(output):
                self.assertEqual(runner.main([
                    "--source-root", str(source), "--output-root", str(root / "output"),
                    "--mode", "batch",
                ]), 0)
            log = output.getvalue()
            self.assertIn(f"Processed {cached} from OCR cache", log)
            self.assertIn(f"Uploading {pending} for batch OCR", log)
            self.assertIn("Batch status: SUCCESS (1/1 requests completed)", log)
            self.assertIn(f"Processed {pending}", log)
            self.assertTrue((root / "output" / "pending.xml").exists())

    def test_all_cached_does_not_submit_empty_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            engine = Mock()
            engine.load_cached_result.return_value = SimpleNamespace(full_markdown="cached", pages=[])
            client = Mock()
            output = StringIO()
            with patch.dict("os.environ", {"MISTRAL_API_KEY": "test"}), \
                    patch.object(runner, "MistralOCREngine", return_value=engine), \
                    patch.object(runner, "Mistral", return_value=client), \
                    redirect_stdout(output):
                self.assertEqual(runner.run_mistral_batch(
                    root, root / "output", [pdf], model="model", force=False,
                    disable_ocr_cache=False, verbose=False,
                ), 0)
            client.batch.jobs.create.assert_not_called()
            self.assertIn("no batch submitted", output.getvalue())

    def test_failed_batch_result_is_not_reported_as_processed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"pdf")
            engine = Mock()
            engine.load_cached_result.return_value = None
            client = Mock()
            client.files.upload.return_value.id = "file-id"
            client.files.get_signed_url.return_value.url = "https://example.test/pdf"
            client.batch.jobs.create.return_value.id = "batch-id"
            client.batch.jobs.get.return_value = SimpleNamespace(
                status="SUCCESS", completed_requests=1, total_requests=1, output_file="result-id",
            )
            client.files.download.return_value = json.dumps({
                "custom_id": "paper.pdf", "response": {"status_code": 400, "body": {"message": "bad PDF"}},
            }).encode()
            output = StringIO()
            with patch.dict("os.environ", {"MISTRAL_API_KEY": "test"}), \
                    patch.object(runner, "MistralOCREngine", return_value=engine), \
                    patch.object(runner, "Mistral", return_value=client), \
                    redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "Batch OCR failed"):
                    runner.run_mistral_batch(
                        root, root / "output", [pdf], model="model", force=False,
                        disable_ocr_cache=False, verbose=False,
                    )
            self.assertNotIn(f"Processed {pdf}", output.getvalue())
            self.assertFalse((root / "output" / "paper.xml").exists())


if __name__ == "__main__":
    unittest.main()
