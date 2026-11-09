import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import yaml

from academic_content_pipeline.paper_batch import PaperBatchRunner


class FakePaperGenerator:
    output_format = "xml"
    languages = ["english"]
    standards = "test"
    tags = ""
    difficulty_mix = "easy:0.2,medium:0.5,hard:0.3"
    pdf_engine = "html"
    exam_duration_minutes = None

    def __init__(self, responses, *, fail_after_saving_response=False):
        self.communicator = SimpleNamespace(model_name="test-model")
        self.responses = list(responses)
        self.calls = []
        self.fail_after_saving_response = fail_after_saving_response

    def process_spec(
        self,
        spec_path,
        output_dir,
        *,
        output_stem,
        additional_prompt,
        raw_response_path,
        raw_response_override,
    ):
        self.calls.append(
            {
                "raw_response_override": raw_response_override,
                "additional_prompt": additional_prompt,
                "spec_data": yaml.safe_load(spec_path.read_text(encoding="utf-8")),
            }
        )
        spec_data = self.calls[-1]["spec_data"]
        subjects = spec_data.get("subjects", [])
        question_count = sum(
            int(subject.get("total_questions", 0)) for subject in subjects
        ) or int(spec_data.get("total_questions", 0))
        question_count = question_count or 1
        subject_names = [subject.get("name", "") for subject in subjects]
        output_dir.mkdir(parents=True, exist_ok=True)
        if raw_response_override is None:
            response = self.responses.pop(0)
            raw_response_path.parent.mkdir(parents=True, exist_ok=True)
            raw_response_path.write_text(response, encoding="utf-8")
            if self.fail_after_saving_response:
                self.fail_after_saving_response = False
                raise RuntimeError("interrupted after AI response")
        else:
            response = raw_response_override

        questions = "".join(
            '<question type="multichoice"><name><text>'
            f"{subject_names[0] if subject_names else 'Question'} {index}</text></name>"
            '<questiontext format="html"><text>'
            f"{response} {index}"
            '</text></questiontext><answer fraction="100"><text>Answer</text>'
            '</answer></question>'
            for index in range(1, question_count + 1)
        )
        output_path = output_dir / f"{output_stem}.xml"
        output_path.write_text(f"<quiz>{questions}</quiz>", encoding="utf-8")
        return output_path


class PaperBatchTests(unittest.TestCase):
    def make_inputs(self, root, *, test_count=1, papers=None):
        spec_path = root / "batch.yaml"
        paper_paths = papers or [("paper", "paper.yaml")]
        paper_entries = "\n".join(
            f"  - id: {paper_id}\n    spec: {path}"
            for paper_id, path in paper_paths
        )
        spec_path.write_text(
            f"exam_name: Sample Exam\ntest_count: {test_count}\n"
            "uniqueness_scope: all_papers\npapers:\n"
            f"{paper_entries}\n",
            encoding="utf-8",
        )
        (root / "paper.yaml").write_text(
            "exam_name: Sample Exam\nsubjects:\n"
            "  - name: Biology\n    total_questions: 1\n    syllabus_file: syllabus.md\n",
            encoding="utf-8",
        )
        (root / "syllabus.md").write_text("Test syllabus", encoding="utf-8")
        return spec_path

    def test_resume_reuses_ai_response_saved_before_interruption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            (root / "paper.yaml").write_text(
                "exam_name: Sample Exam\nsubjects:\n"
                "  - name: Mathematics\n    total_questions: 1\n    syllabus_file: syllabus.md\n"
                "  - name: Physics\n    total_questions: 1\n    syllabus_file: syllabus.md\n",
                encoding="utf-8",
            )
            output_dir = root / "output"
            first_generator = FakePaperGenerator(
                ["saved response"],
                fail_after_saving_response=True,
            )
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                PaperBatchRunner(first_generator).run(spec_path, output_dir)

            resumed_generator = FakePaperGenerator(["physics response"])
            result = PaperBatchRunner(resumed_generator).run(
                spec_path,
                output_dir,
                resume=True,
            )

            self.assertEqual(len(result), 1)
            self.assertEqual(
                [
                    call["raw_response_override"]
                    for call in resumed_generator.calls
                ],
                ["saved response", None],
            )

    def test_duplicate_question_is_rejected_and_retried(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root, test_count=2)
            generator = FakePaperGenerator(["same question", "same question", "new question"])

            results = PaperBatchRunner(generator).run(
                spec_path,
                root / "output",
                max_attempts=3,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(len(generator.calls), 3)
            self.assertIn("Previously used question stems", generator.calls[2]["additional_prompt"])

    def test_failed_batch_checkpoint_is_marked_failed(self):
        class InvalidXmlGenerator(FakePaperGenerator):
            def process_spec(self, *args, **kwargs):
                output_path = super().process_spec(*args, **kwargs)
                output_path.write_text("<quiz><question></quiz>", encoding="utf-8")
                return output_path

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            generator = InvalidXmlGenerator(["invalid response"] * 3)

            with self.assertRaisesRegex(ValueError, "after 3 attempt"):
                PaperBatchRunner(generator).run(spec_path, output_dir)

            manifest = json.loads(
                (output_dir / "sample_exam_batch_generation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["items"][0]["status"], "failed")

    def test_generates_and_combines_subject_question_counts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            (root / "paper.yaml").write_text(
                "exam_name: Sample Exam\n"
                "subjects:\n"
                "  - name: Mathematics\n"
                "    total_questions: 2\n"
                "    syllabus_file: syllabus.md\n"
                "  - name: Physics\n"
                "    total_questions: 1\n"
                "    syllabus_file: syllabus.md\n"
                "sections:\n"
                "  - subject: Mathematics\n"
                "    question_count: 2\n"
                "    question_types:\n"
                "      - type: standard_mcq\n"
                "        count: 2\n"
                "  - subject: Physics\n"
                "    question_count: 1\n"
                "    question_types:\n"
                "      - type: standard_mcq\n"
                "        count: 1\n",
                encoding="utf-8",
            )
            generator = FakePaperGenerator(["math response", "physics response"])

            results = PaperBatchRunner(generator).run(spec_path, root / "output")

            self.assertEqual(len(generator.calls), 2)
            self.assertEqual(
                [
                    (
                        call["spec_data"]["subjects"][0]["name"],
                        call["spec_data"]["subjects"][0]["total_questions"],
                    )
                    for call in generator.calls
                ],
                [("Mathematics", 2), ("Physics", 1)],
            )
            self.assertTrue(
                all("Generate exactly" in call["additional_prompt"] for call in generator.calls)
            )
            self.assertEqual(
                [
                    sum(
                        allocation["count"]
                        for allocation in call["spec_data"]["question_types"]["types"]
                    )
                    for call in generator.calls
                ],
                [2, 1],
            )
            self.assertEqual(
                len(
                    [
                        question
                        for question in ET.parse(results[0]).getroot()
                        if question.tag == "question"
                    ]
                ),
                3,
            )

    def test_resume_rejects_changed_syllabus(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            generator = FakePaperGenerator(
                ["saved response"],
                fail_after_saving_response=True,
            )
            with self.assertRaises(RuntimeError):
                PaperBatchRunner(generator).run(spec_path, output_dir)

            (root / "syllabus.md").write_text("Changed syllabus", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differ from the saved checkpoint"):
                PaperBatchRunner(FakePaperGenerator([])).run(
                    spec_path,
                    output_dir,
                    resume=True,
                )


if __name__ == "__main__":
    unittest.main()
