import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from academic_content_pipeline.content_processors import (
    QuestionPaperExtractor,
    is_ncert_exemplar_source,
    load_question_checkpoint,
    ncert_exemplar_section_context,
    validate_ncert_exemplar_questions,
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

    def test_category_is_in_partial_and_resumed_xml(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "neet_ug" / "chapter.pdf"
            source.parent.mkdir()
            source.write_bytes(b"test pdf")
            output = root / "output"
            extractor = self.make_extractor(FakeCommunicator(fail_on_call=2))
            extractor.top_category = "Chapterwise Question Bank"
            extractor.source_root = root
            extractor.languages = ["English", "Bengali"]
            with self.assertRaisesRegex(RuntimeError, "simulated network failure"):
                self.run_file(extractor, source, output)

            path = output / "chapter_english_bengali.xml"
            expected = "$module$/top/Chapterwise Question Bank/neet_ug/chapter/english_bengali"
            self.assertEqual(ET.parse(path).getroot()[0].findtext("category/text"), expected)

            resumed = self.make_extractor(FakeCommunicator(), resume=True)
            resumed.top_category = extractor.top_category
            resumed.source_root = root
            resumed.languages = extractor.languages
            self.run_file(resumed, source, output)
            quiz = ET.parse(path).getroot()
            self.assertEqual(quiz[0].findtext("category/text"), expected)
            self.assertEqual(len(load_question_checkpoint(path)), 3)
            resumed.top_category = "Other"
            with self.assertRaisesRegex(ValueError, "settings differ"):
                self.run_file(resumed, source, output)

    def test_checkpoint_mismatch_logs_compared_fields_for_resume_and_replace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.pdf"
            source.write_bytes(b"test pdf")
            output = root / "output"
            self.run_file(self.make_extractor(FakeCommunicator()), source, output)
            checkpoint = output / "chapter_english.extract.json"
            manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
            configuration = manifest["configuration"]
            configuration["batch_size"] = 42
            configuration["category_path"] = "$module$/top/" + "long-category-" * 20
            configuration.pop("tags")
            configuration["model_name"] = "ignored-model"
            checkpoint.write_text(json.dumps(manifest), encoding="utf-8")

            for operation in ("resume", "replace"):
                with self.subTest(operation=operation):
                    extractor = self.make_extractor(
                        FakeCommunicator(),
                        resume=operation == "resume",
                        replace_pages=[1, 1] if operation == "replace" else None,
                    )
                    with self.assertLogs("academic_content_pipeline", level="ERROR") as logs:
                        with self.assertRaisesRegex(ValueError, "settings differ"):
                            self.run_file(extractor, source, output)
                    message = "\n".join(logs.output)
                    self.assertIn('configuration.batch_size: saved=42, requested=1', message)
                    self.assertIn(f'configuration.category_path: saved="{configuration["category_path"]}", requested=<missing>', message)
                    self.assertIn('configuration.tags: saved=<missing>, requested=""', message)
                    self.assertNotIn("model_name", message)
                    self.assertFalse(extractor.communicator.calls)

    def test_invalid_checkpoint_configuration_is_logged(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.pdf"
            source.write_bytes(b"test pdf")
            output = root / "output"
            self.run_file(self.make_extractor(FakeCommunicator()), source, output)
            checkpoint = output / "chapter_english.extract.json"
            manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
            manifest["configuration"] = None
            checkpoint.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertLogs("academic_content_pipeline", level="ERROR") as logs:
                with self.assertRaisesRegex(ValueError, "settings differ"):
                    self.run_file(self.make_extractor(FakeCommunicator(), resume=True), source, output)
            self.assertIn("saved checkpoint has no valid configuration mapping", "\n".join(logs.output))

    def test_extraction_prompt_snapshots_include_language_and_standard(self):
        class RecordingCommunicator(FakeCommunicator):
            def __init__(self):
                super().__init__()
                self.requests = []

            def generate(self, **kwargs):
                self.requests.append((kwargs["prompt_snapshot_path"], kwargs["output_filename"]))
                return super().generate(**kwargs)

        class RecordingPageChecker(FakePageChecker):
            def __init__(self):
                self.requests = []

            def generate(self, **kwargs):
                self.requests.append((kwargs["prompt_snapshot_path"], kwargs["output_filename"]))
                return super().generate(**kwargs)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            for languages, suffix in (
                (["english"], "english"),
                (["english", "bengali"], "english_bengali"),
                (["english", "hindi"], "english_hindi"),
            ):
                communicator = RecordingCommunicator()
                checker = RecordingPageChecker()
                extractor = self.make_extractor(communicator)
                extractor.languages = languages
                extractor.standards = "neet_ug"
                extractor.page_check_communicator = checker
                extractor.ocr_engine.pages = extractor.ocr_engine.pages[:1]
                self.run_file(extractor, pdf_path, output_dir)

                stem = f"paper_neet_ug_{suffix}"
                snapshot, output_name = communicator.requests[0]
                self.assertEqual(snapshot.name, f"{stem}_pages_1_1_prompt.md")
                self.assertEqual(output_name, f"{stem}_pages_1_1.xml")
                self.assertTrue(snapshot.is_file())
                classifier_snapshot, classifier_output = checker.requests[0]
                self.assertEqual(classifier_snapshot.name, f"{stem}_instruction_page_check_prompt.md")
                self.assertEqual(classifier_output, f"{stem}_instruction_page_check.json")

            prompt_dir = output_dir / "paper" / ".tmp" / "prompts"
            self.assertEqual(len(list(prompt_dir.glob("*_pages_1_1_prompt.md"))), 3)

    def test_ncert_exemplar_extracts_only_mcqs_and_passes_context_between_batches(self):
        class ExemplarCommunicator(FakeCommunicator):
            def __init__(self):
                super().__init__()
                self.prompts = []
                self.instructions = []

            def generate(self, **kwargs):
                page_number = kwargs["contents"].page_range[0]
                self.prompts.append(kwargs["contents"].text)
                self.instructions.append(kwargs["system_instruction"])
                sections = {
                    1: [("MCQ_I", n) for n in range(1, 7)]
                    + [("MCQ_II", n) for n in range(7, 12)],
                    2: [],
                    3: [],
                }
                return "<quiz>" + "".join(
                    (
                        exemplar_question(f"keep312__{section}__12.{number}", single="false")
                        .replace('fraction="100"', 'fraction="50"')
                        .replace('fraction="0"', 'fraction="50"')
                        if section == "MCQ_II"
                        else exemplar_question(f"keep312__{section}__12.{number}")
                    )
                    for section, number in sections[page_number]
                ) + "</quiz>"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "ncert_exemplar" / "class11" / "physics" / "keep312.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"test pdf")
            communicator = ExemplarCommunicator()
            extractor = self.make_extractor(communicator)
            extractor.ocr_engine.pages[0].markdown = "# MCQ I"
            extractor.ocr_engine.pages[1].markdown = "# VSA"
            extractor.ocr_engine.pages[2].markdown = "# SA\n# LA"

            result_path = self.run_file(extractor, pdf_path, root / "output")

            questions = load_question_checkpoint(result_path)
            self.assertIsNotNone(questions)
            self.assertEqual(len(questions), 11)
            self.assertEqual(ET.fromstring(questions[6]).findtext("single"), "false")
            self.assertIn("Exclude all questions from VSA, SA and LA sections", communicator.instructions[0])
            self.assertIn("Do not convert them into MCQs", communicator.instructions[0])
            self.assertNotIn("<idnumber>", result_path.read_text(encoding="utf-8"))
            self.assertIn("NCERT Exemplar preceding section: MCQ I", communicator.prompts[1])
            self.assertIn("NCERT Exemplar preceding section: VSA", communicator.prompts[2])
            manifest = json.loads(
                (root / "output" / "keep312_english.extract.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("ncert_exemplar_prompt_sha256", manifest["configuration"])

    def test_ncert_exemplar_rejects_non_mcq_output_and_marks_batch_failed(self):
        class NonMCQCommunicator(FakeCommunicator):
            def generate(self, **kwargs):
                return '<quiz><question type="numerical"><name><text>Q</text></name></question></quiz>'

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "ncert_exemplar" / "paper.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"test pdf")
            with self.assertRaisesRegex(ValueError, "only multichoice or matching"):
                self.run_file(
                    self.make_extractor(NonMCQCommunicator()),
                    pdf_path,
                    root / "output",
                )
            manifest = json.loads(
                (root / "output" / "paper_english.extract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["batches"][0]["status"], "failed")

    def test_ncert_exemplar_extracts_matching_with_subquestion_image(self):
        class MatchingCommunicator(FakeCommunicator):
            def generate(self, **kwargs):
                node = exemplar_matching_question().replace(
                    "<text>Item 1</text>",
                    '<text><![CDATA[<img src="@@PLUGINFILE@@/diagram.png">]]></text>',
                )
                return f"<quiz>{node}</quiz>"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "ncert_exemplar" / "paper.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"test pdf")
            missing = self.make_extractor(MatchingCommunicator())
            missing.ocr_engine.pages = missing.ocr_engine.pages[:1]
            with self.assertRaisesRegex(ValueError, "missing embedded files"):
                self.run_file(missing, pdf_path, root / "missing-output")

            diagram = root / "diagram.png"
            diagram.write_bytes(b"diagram")
            extractor = self.make_extractor(MatchingCommunicator())
            extractor.ocr_engine.pages = extractor.ocr_engine.pages[:1]
            extractor.ocr_engine.all_images = {"diagram.png": str(diagram)}

            result = self.run_file(extractor, pdf_path, root / "output")

            question = ET.parse(result).getroot().find("question")
            self.assertEqual(question.get("type"), "matching")
            self.assertEqual(question.find("subquestion/file").get("name"), "diagram.png")
            self.assertEqual(question.findtext("subquestion/answer/text"), "Answer 1")

    def test_ncert_exemplar_requires_referenced_images_to_be_embedded(self):
        class ImageCommunicator(FakeCommunicator):
            def generate(self, **kwargs):
                node = exemplar_question("paper__MCQ_I__1").replace(
                    "<text>Question</text>",
                    '<text><![CDATA[<img src="@@PLUGINFILE@@/diagram.png">]]></text>',
                )
                return f"<quiz>{node}</quiz>"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "ncert_exemplar" / "paper.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"test pdf")
            extractor = self.make_extractor(ImageCommunicator())
            extractor.ocr_engine.pages = extractor.ocr_engine.pages[:1]
            output_dir = root / "missing-output"
            with self.assertRaisesRegex(ValueError, "missing embedded files"):
                self.run_file(extractor, pdf_path, output_dir)
            manifest = json.loads(
                (output_dir / "paper_english.extract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["batches"][0]["status"], "failed")
            self.assertFalse((output_dir / "paper_english.xml").exists())

            diagram = root / "diagram.png"
            diagram.write_bytes(b"diagram")
            extractor = self.make_extractor(ImageCommunicator())
            extractor.ocr_engine.pages = extractor.ocr_engine.pages[:1]
            extractor.ocr_engine.all_images = {"diagram.png": str(diagram)}
            result = self.run_file(extractor, pdf_path, root / "valid-output")
            self.assertIn('<file name="diagram.png"', result.read_text(encoding="utf-8"))

    def test_ncert_exemplar_resume_allows_changed_prompt_and_reuses_batches(self):
        class ExemplarCommunicator(FakeCommunicator):
            def generate(self, **kwargs):
                super().generate(**kwargs)
                page_number = kwargs["contents"].page_range[0]
                return "<quiz>" + exemplar_question(f"Question {page_number}") + "</quiz>"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "ncert_exemplar" / "paper.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"test pdf")
            output_dir = root / "output"
            communicator = ExemplarCommunicator(fail_on_call=2)
            with self.assertRaisesRegex(RuntimeError, "simulated network failure"):
                self.run_file(self.make_extractor(communicator), pdf_path, output_dir)
            manifest_path = output_dir / "paper_english.extract.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["configuration"]["ncert_exemplar_prompt_sha256"] = "old prompt"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            resumed = ExemplarCommunicator()
            result_path = self.run_file(
                self.make_extractor(resumed, resume=True),
                pdf_path,
                output_dir,
            )
            self.assertEqual(resumed.calls, [(2, 2), (3, 3)])
            questions = load_question_checkpoint(result_path)
            self.assertIsNotNone(questions)
            self.assertEqual(len(questions), 3)
            self.assertIn("Question 1", questions[0])

    def test_legacy_prompt_hash_is_ignored_but_other_settings_must_match(self):
        current = {"languages": ["english"]}
        self.assertTrue(
            QuestionPaperExtractor._checkpoint_configuration_matches(
                {"languages": ["english"], "ncert_exemplar_prompt_sha256": "old"}, current
            )
        )
        self.assertFalse(
            QuestionPaperExtractor._checkpoint_configuration_matches(
                {"languages": ["hindi"], "ncert_exemplar_prompt_sha256": "new"}, current
            )
        )

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



def exemplar_question(name, *, question_type="multichoice", single="true"):
    return (
        f'<question type="{question_type}"><name><text>{name}</text></name>'
        '<questiontext><text>Question</text></questiontext>'
        '<generalfeedback><text>Solution</text></generalfeedback>'
        f'<single>{single}</single><answer fraction="100"><text>A</text></answer>'
        '<answer fraction="0"><text>B</text></answer></question>'
    )


def exemplar_matching_question():
    return (
        '<question type="matching"><name><text>Matching</text></name>'
        '<questiontext><text>Match items</text></questiontext>'
        '<generalfeedback><text>Solution</text></generalfeedback>'
        '<subquestion format="html"><text>Item 1</text><answer><text>Answer 1</text></answer></subquestion>'
        '<subquestion format="html"><text>Item 2</text><answer><text>Answer 2</text></answer></subquestion>'
        '<subquestion format="html"><text></text><answer><text>Distractor</text></answer></subquestion>'
        '</question>'
    )


class ExemplarValidationTests(unittest.TestCase):
    def test_exemplar_rules_apply_only_to_the_exemplar_directory(self):
        self.assertTrue(
            is_ncert_exemplar_source(Path("papers") / "NCERT_EXEMPLAR" / "chapter.pdf")
        )
        self.assertFalse(
            is_ncert_exemplar_source(Path("papers") / "ncert_exemplar_archive" / "chapter.pdf")
        )

    def test_section_context_retains_answers_across_pages(self):
        pages = [
            SimpleNamespace(page_num=1, markdown="# MCQ I"),
            SimpleNamespace(page_num=2, markdown="# VSA"),
            SimpleNamespace(page_num=3, markdown="# ANSWERS\n## I. Multiple Choice Questions (Type-I)"),
            SimpleNamespace(page_num=4, markdown="Continued answer key"),
        ]
        self.assertEqual(
            ncert_exemplar_section_context(pages),
            {1: None, 2: "MCQ I", 3: "VSA", 4: "Answers"},
        )

    def test_question_identifiers_are_optional_and_do_not_affect_validation(self):
        node = exemplar_question("keep312__MCQ_I__12.1")
        with_id = node.replace("</name>", "</name><idnumber>legacy</idnumber>")
        validate_ncert_exemplar_questions([node, with_id, with_id])

    def test_invalid_grading_is_rejected(self):
        node = exemplar_question("Question 1")
        invalid = node.replace('fraction="100"', 'fraction="50"')
        with self.assertRaisesRegex(ValueError, "grading"):
            validate_ncert_exemplar_questions([invalid])

    def test_multi_correct_answers_are_accepted(self):
        node = (
            exemplar_question("keep312__MCQ_II__12.7", single="false")
            .replace('fraction="100"', 'fraction="50"')
            .replace('fraction="0"', 'fraction="50"')
        )
        validate_ncert_exemplar_questions([node])

    def test_matching_accepts_distractors_and_rejects_malformed_pairs(self):
        node = exemplar_matching_question()
        validate_ncert_exemplar_questions([node])
        for invalid in (
            node.replace("<text>Item 2</text>", "<text></text>"),
            node.replace("<text>Answer 2</text>", "<text></text>"),
            node.replace("<text>Item 2</text>", ""),
            node.replace(
                '<subquestion format="html"><text></text><answer><text>Distractor</text></answer></subquestion>',
                "",
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "matching question"):
                    validate_ncert_exemplar_questions([invalid])

    def test_language_checker_checks_missing_files_and_question_counts_without_ids(self):
        checker = Path(__file__).resolve().parents[1] / "scripts" / "check_ncert_exemplar_xml.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            node = exemplar_question("keep312__MCQ_I__12.1")
            english = root / "keep312_neet_ug_english.xml"
            bengali = root / "keep312_neet_ug_english_bengali.xml"
            hindi = root / "keep312_neet_ug_english_hindi.xml"
            english.write_text(f"<quiz>{node}</quiz>", encoding="utf-8")
            bengali.write_text(f"<quiz>{node}</quiz>", encoding="utf-8")
            command = [sys.executable, str(checker), "--root", str(root)]
            missing = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("missing english_hindi", missing.stderr)

            other = exemplar_question("keep312__MCQ_I__12.2")
            hindi.write_text(f"<quiz>{node}{other}</quiz>", encoding="utf-8")
            different = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(different.returncode, 0)
            self.assertIn("question count mismatch", different.stderr)
            hindi.write_text(f"<quiz>{other}</quiz>", encoding="utf-8")
            consistent = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(consistent.returncode, 0, consistent.stderr)
            self.assertIn("Equal counts do not prove", consistent.stdout)
            matching = exemplar_matching_question()
            for path in (english, bengali, hindi):
                path.write_text(f"<quiz>{matching}</quiz>", encoding="utf-8")
            matching_result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(matching_result.returncode, 0, matching_result.stderr)
            category = '<question type="category"><category><text>$module$/top/Bank/english</text></category></question>'
            for path in (english, bengali, hindi):
                path.write_text(f"<quiz>{category}{matching}</quiz>", encoding="utf-8")
            categorized = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(categorized.returncode, 0, categorized.stderr)


if __name__ == "__main__":
    unittest.main()
