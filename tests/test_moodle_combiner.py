import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from academic_content_pipeline.moodle_combiner import COMBINED_NAME, rebuild_combined_xml
from academic_content_pipeline.unified_pipeline import main as pipeline_main

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_path_mirrored_extraction as extraction
import run_path_mirrored_paper_generation as papers
import run_path_mirrored_question_generation as questions


def question_xml(category, name):
    return (
        f'<quiz><question type="category"><category><text>{category}</text></category></question>'
        f'<question type="multichoice"><name><text>{name}</text></name>'
        '<questiontext><text><![CDATA[<p>A &amp; B</p>]]></text></questiontext>'
        '</question></quiz>'
    )


class MoodleCombinerTests(unittest.TestCase):
    def test_combines_each_level_preserves_categories_and_ignores_combined_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            biology = root / "class11" / "biology"
            physics = root / "class11" / "physics"
            biology.mkdir(parents=True)
            physics.mkdir()
            (biology / "one_english.xml").write_text(question_xml("$module$/top/Bank/one/english", "One"), encoding="utf-8")
            (physics / "two_english_bengali.xml").write_text(question_xml("$module$/top/Bank/two/english_bengali", "Two"), encoding="utf-8")
            (biology / "legacy_combined_moodle.xml").write_text(question_xml("duplicate", "Duplicate"), encoding="utf-8")
            (biology / "ocr.xml").write_text("<ocr_result/>", encoding="utf-8")
            (biology / "categories.xml").write_text(
                '<quiz><question type="category"/><question type="category"/></quiz>',
                encoding="utf-8",
            )
            hidden = biology / ".checkpoints"
            hidden.mkdir()
            (hidden / "batch.xml").write_text(question_xml("hidden", "Hidden"), encoding="utf-8")

            paths = rebuild_combined_xml(root)
            self.assertEqual(set(paths), {root / COMBINED_NAME, root / "class11" / COMBINED_NAME, biology / COMBINED_NAME, physics / COMBINED_NAME})
            quiz = ET.parse(root / COMBINED_NAME).getroot()
            self.assertEqual(
                [q.findtext("category/text") for q in quiz if q.get("type") == "category"],
                ["$module$/top/Bank/one/english", "$module$/top/Bank/two/english_bengali"],
            )
            self.assertEqual([q.findtext("name/text") for q in quiz if q.get("type") != "category"], ["One", "Two"])
            self.assertIn("<![CDATA[<p>A &amp; B</p>]]>", (root / COMBINED_NAME).read_text(encoding="utf-8"))
            before = {path: path.read_bytes() for path in paths}
            self.assertEqual(set(rebuild_combined_xml(root)), set(paths))
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            (physics / "two_english_bengali.xml").unlink()
            rebuild_combined_xml(root)
            self.assertEqual(len(ET.parse(physics / COMBINED_NAME).getroot()), 0)
            self.assertEqual(
                [q.findtext("name/text") for q in ET.parse(root / COMBINED_NAME).getroot() if q.get("type") != "category"],
                ["One"],
            )

    def test_uncategorized_or_unowned_files_fail_before_any_combined_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            chapter = root / "chapter"
            chapter.mkdir()
            xml = chapter / "questions.xml"
            xml.write_text(question_xml("$module$/top/Bank/chapter/english", "Q"), encoding="utf-8")
            destination = root / COMBINED_NAME
            destination.write_text("<quiz/>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unrelated combined"):
                rebuild_combined_xml(root)
            self.assertEqual(destination.read_text(encoding="utf-8"), "<quiz/>")
            self.assertFalse((chapter / COMBINED_NAME).exists())
            destination.unlink()
            xml.write_text('<quiz><question type="multichoice"/></quiz>', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "leading category"):
                rebuild_combined_xml(root)
            self.assertFalse(destination.exists())

    def test_rejects_modified_and_incomplete_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            xml = root / "chapter_english.xml"
            xml.write_text(question_xml("$module$/top/Bank/chapter/english", "Q"), encoding="utf-8")
            checkpoint = xml.with_suffix(".extract.json")
            manifest = {
                "schema_version": 1, "status": "in_progress", "output_file": xml.name,
                "final_sha256": hashlib.sha256(xml.read_bytes()).hexdigest(),
            }
            checkpoint.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete or does not match"):
                rebuild_combined_xml(root)
            manifest["status"] = "completed"
            manifest["final_sha256"] = "wrong"
            checkpoint.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete or does not match"):
                rebuild_combined_xml(root)
            self.assertFalse((root / COMBINED_NAME).exists())
            checkpoint.unlink()
            manifest = {
                "schema_version": 2, "status": "completed", "items": [
                    {"status": "completed", "output_file": xml.name, "output_sha256": hashlib.sha256(xml.read_bytes()).hexdigest()},
                    {"status": "completed", "output_file": "missing.xml", "output_sha256": "missing"},
                ],
            }
            (root / "exam_batch_generation.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                rebuild_combined_xml(root)
            self.assertFalse((root / COMBINED_NAME).exists())
            (root / "exam_batch_generation.json").unlink()
            adaptive = root / "chapter_english_question_types.json"
            adaptive.write_text(json.dumps({
                "status": "in_progress", "files": [xml.name, "missing.xml"],
                "completed_files": [xml.name],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Question-generation checkpoint is incomplete"):
                rebuild_combined_xml(root)
            self.assertFalse((root / COMBINED_NAME).exists())

    def test_existing_lock_blocks_combination_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "chapter.xml").write_text(question_xml("$module$/top/Bank/chapter/english", "Q"), encoding="utf-8")
            lock = root / ".moodle-combiner.lock"
            lock.write_text("another process", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale lock"):
                rebuild_combined_xml(root)
            self.assertFalse((root / COMBINED_NAME).exists())
            self.assertEqual(lock.read_text(encoding="utf-8"), "another process")

    def test_all_direct_commands_rebuild_after_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.pdf"
            source.write_bytes(b"pdf")
            spec = root / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            single_spec = root / "single.yaml"
            single_spec.write_text("exam_name: Test\nsubjects: []\n", encoding="utf-8")
            for task, batch_spec in (("extract", False), ("generate-questions", False), ("generate-paper", True), ("generate-papers", True), ("generate-paper", False)):
                output = root / (task + ("-batch" if batch_spec else "-single"))
                argv = [
                    "pipeline", "agent", task, "--source-root", str(root),
                    "--top-category", "Bank", "--output-dir", str(output),
                ]
                argv.extend(["--spec", str(spec if batch_spec else single_spec)] if "paper" in task else ["--input-file", str(source)])

                def write_output(*args, **kwargs):
                    output.mkdir(parents=True, exist_ok=True)
                    path = output / "result.xml"
                    path.write_text(question_xml("$module$/top/Bank/source/english", task), encoding="utf-8")
                    return path

                with self.subTest(task=task), \
                     patch("sys.argv", argv), \
                     patch.dict("os.environ", {"GEMINI_API_KEY": "test", "MISTRAL_API_KEY": "test"}), \
                     patch("academic_content_pipeline.unified_pipeline.setup_logger"), \
                     patch("academic_content_pipeline.unified_pipeline.build_communicator"), \
                     patch("academic_content_pipeline.unified_pipeline.SingleShotBackend"), \
                     patch("academic_content_pipeline.unified_pipeline.MistralOCREngine"), \
                     patch("academic_content_pipeline.unified_pipeline.QuestionPaperExtractor") as extractor, \
                     patch("academic_content_pipeline.unified_pipeline.QuestionGenerator") as generator, \
                     patch("academic_content_pipeline.unified_pipeline.PaperGenerator") as paper_generator, \
                     patch("academic_content_pipeline.unified_pipeline.PaperBatchRunner") as batch:
                    extractor.return_value.process_file.side_effect = write_output
                    generator.return_value.process_file.side_effect = write_output
                    paper_generator.return_value.process_spec.side_effect = write_output
                    batch.return_value.run.side_effect = write_output
                    pipeline_main()
                    quiz = ET.parse(output / COMBINED_NAME).getroot()
                    self.assertEqual(quiz[1].findtext("name/text"), task)

    def test_mirrored_runners_combine_after_jobs_and_not_on_dry_run_or_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            for folder in ("first", "second"):
                directory = source / folder
                directory.mkdir(parents=True)
                (directory / f"{folder}.pdf").write_bytes(b"pdf")
                (directory / "batch.yaml").write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")

            def execute(command, path):
                self.assertIn("--defer-combined-xml", command)
                output_dir = Path(command[command.index("--output-dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / f"{path.stem}.xml").write_text(
                    question_xml(f"$module$/top/Bank/{path.parent.name}/english", path.stem),
                    encoding="utf-8",
                )
                return 0

            for runner, method in ((extraction, "execute_pdf_command"), (questions, "execute_command"), (papers, "execute_command")):
                output = root / runner.__name__
                args = ["--source-root", str(source), "--output-root", str(output), "--top-category", "Bank", "--parallel-workers", "2"]
                with self.subTest(runner=runner.__name__), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()), \
                     patch.object(runner, method, side_effect=execute):
                    self.assertEqual(runner.main(args + ["--dry-run"]), 0)
                    self.assertFalse((output / COMBINED_NAME).exists())
                    self.assertEqual(runner.main(args), 0)
                    quiz = ET.parse(output / COMBINED_NAME).getroot()
                    self.assertEqual(len([q for q in quiz if q.get("type") != "category"]), 2)
                    self.assertTrue((output / "first" / COMBINED_NAME).exists())
                    self.assertTrue((output / "second" / COMBINED_NAME).exists())
                    before = (output / COMBINED_NAME).read_bytes()
                    with patch.object(runner, method, return_value=1):
                        self.assertNotEqual(runner.main(args + ["--force"]), 0)
                    self.assertEqual((output / COMBINED_NAME).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
