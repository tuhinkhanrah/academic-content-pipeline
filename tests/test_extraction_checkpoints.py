import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from academic_content_pipeline.content_processors import (
    QuestionPaperExtractor,
    load_question_checkpoint,
)


class FakeOCR:
    def __init__(self):
        self.pages = [
            SimpleNamespace(page_num=1, markdown="Page 1", images={}),
            SimpleNamespace(page_num=2, markdown="Page 2", images={}),
            SimpleNamespace(page_num=3, markdown="Page 3", images={}),
        ]
        self.all_images = {}
        self.full_markdown = "\n".join(page.markdown for page in self.pages)

    def process_pdf(self, **kwargs):
        return self


class FakePageChecker:
    model_name = "page-check-model"

    def generate(self, **kwargs):
        return '{"is_instruction_page": false, "summary": ""}'


class FakeCommunicator:
    model_name = "test-model"

    def __init__(
        self,
        fail_on_call=None,
        question_prefix="Question",
        empty_pages=None,
        invalid_pages=None,
    ):
        self.calls = []
        self.fail_on_call = fail_on_call
        self.question_prefix = question_prefix
        self.empty_pages = set(empty_pages or [])
        self.invalid_pages = set(invalid_pages or [])

    def generate(self, **kwargs):
        self.calls.append(kwargs["contents"].page_range)
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("simulated network failure")
        page_number = kwargs["contents"].page_range[0]
        if page_number in self.empty_pages:
            return "<quiz></quiz>"
        if page_number in self.invalid_pages:
            return "No complete questions found."
        question_name = f"{self.question_prefix} {page_number}"
        return (
            '<quiz><question type="multichoice"><name><text>'
            f'{question_name}'
            '</text></name><questiontext format="html"><text>'
            f'{question_name} text'
            '</text></questiontext><answer fraction="100"><text>A</text>'
            '</answer></question></quiz>'
        )


class ExtractionCheckpointTests(unittest.TestCase):
    def make_extractor(self, communicator, *, resume=False, replace_pages=None):
        return QuestionPaperExtractor(
            communicator=communicator,
            page_check_communicator=FakePageChecker(),
            ocr_engine=FakeOCR(),
            batch_size=1,
            rate_limit_delay=0,
            resume=resume,
            replace_pages=replace_pages,
        )

    def run_file(self, extractor, pdf_path, output_dir):
        with (
            patch(
                "academic_content_pipeline.content_processors.assemble_prompt_files",
                return_value="system instruction",
            ),
            patch(
                "academic_content_pipeline.content_processors.build_language_instructions",
                return_value=("language instruction", []),
            ),
            patch(
                "academic_content_pipeline.content_processors.load_prompt_template",
                return_value="prompt",
            ),
        ):
            return extractor.process_file(pdf_path, output_dir)

    def test_resume_reuses_completed_batches_after_interruption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            first_communicator = FakeCommunicator(fail_on_call=2)

            with self.assertRaisesRegex(RuntimeError, "simulated network failure"):
                self.run_file(
                    self.make_extractor(first_communicator),
                    pdf_path,
                    output_dir,
                )

            manifest_path = output_dir / "paper_english.extract.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["batches"][0]["status"], "completed")
            self.assertEqual(manifest["batches"][1]["status"], "failed")
            manifest["configuration"].update(
                {
                    "page_check_model_name": "legacy-page-check-model",
                    "model_name": "changed-model",
                    "agent_name": "changed-agent",
                    "agent_type": "changed-agent-type",
                    "bucket_name": "changed-bucket",
                    "ocr_model": "changed-ocr-model",
                    "verify_online": not manifest["configuration"]["verify_online"],
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            partial_xml = output_dir / "paper_english.xml"
            self.assertTrue(partial_xml.is_file())
            partial_questions = load_question_checkpoint(partial_xml)
            self.assertIsNotNone(partial_questions)
            self.assertEqual(len(partial_questions), 1)

            resumed_communicator = FakeCommunicator()
            resumed_extractor = self.make_extractor(resumed_communicator, resume=True)
            resumed_extractor.page_check_communicator.model_name = "new-page-check-model"
            result_path = self.run_file(
                resumed_extractor,
                pdf_path,
                output_dir,
            )

            self.assertEqual(
                resumed_communicator.calls,
                [(2, 2), (3, 3)],
            )
            questions = load_question_checkpoint(result_path)
            self.assertIsNotNone(questions)
            self.assertEqual(len(questions), 3)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertTrue(all(batch["status"] == "completed" for batch in manifest["batches"]))

    def test_empty_question_batch_completes_and_preserves_other_questions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            communicator = FakeCommunicator(empty_pages={2})

            result_path = self.run_file(
                self.make_extractor(communicator),
                pdf_path,
                output_dir,
            )

            self.assertEqual(communicator.calls, [(1, 1), (2, 2), (3, 3)])
            questions = load_question_checkpoint(result_path)
            self.assertIsNotNone(questions)
            self.assertEqual(len(questions), 2)
            final_xml = result_path.read_text(encoding="utf-8")
            self.assertIn("Question 1 text", final_xml)
            self.assertIn("Question 3 text", final_xml)
            manifest = json.loads(
                (output_dir / "paper_english.extract.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["batches"][1]["status"], "completed")
            self.assertEqual(manifest["batches"][1]["question_count"], 0)

    def test_invalid_batch_keeps_questions_from_completed_batches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            communicator = FakeCommunicator(invalid_pages={2})

            with self.assertRaisesRegex(ValueError, "no valid Moodle quiz"):
                self.run_file(
                    self.make_extractor(communicator),
                    pdf_path,
                    output_dir,
                )

            partial_xml = output_dir / "paper_english.xml"
            questions = load_question_checkpoint(partial_xml)
            self.assertIsNotNone(questions)
            self.assertEqual(len(questions), 1)
            self.assertIn("Question 1 text", partial_xml.read_text(encoding="utf-8"))
            manifest = json.loads(
                (output_dir / "paper_english.extract.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["batches"][0]["status"], "completed")
            self.assertEqual(manifest["batches"][1]["status"], "failed")

    def test_resume_refuses_legacy_xml_without_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "paper.xml").write_text("<quiz />", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "has no extraction checkpoint"):
                self.run_file(
                    self.make_extractor(FakeCommunicator(), resume=True),
                    pdf_path,
                    output_dir,
                )

    def test_replace_pages_replaces_only_the_selected_checkpoint_batch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            result_path = self.run_file(
                self.make_extractor(FakeCommunicator()),
                pdf_path,
                output_dir,
            )

            replacement_communicator = FakeCommunicator(
                question_prefix="Updated question"
            )
            self.run_file(
                self.make_extractor(
                    replacement_communicator,
                    replace_pages=[2, 2],
                ),
                pdf_path,
                output_dir,
            )

            self.assertEqual(replacement_communicator.calls, [(2, 2)])
            final_xml = result_path.read_text(encoding="utf-8")
            self.assertIn("Question 1 text", final_xml)
            self.assertIn("Updated question 2 text", final_xml)
            self.assertIn("Question 3 text", final_xml)
            self.assertNotIn("Question 2 text", final_xml)



if __name__ == "__main__":
    unittest.main()
