import contextlib
import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from academic_content_pipeline.moodle_categories import (
    build_category_path,
    build_exam_practice_paper_category_path,
    set_moodle_category,
)
from academic_content_pipeline.moodle_combiner import combined_name_for_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_path_mirrored_extraction
import run_path_mirrored_paper_generation
import run_path_mirrored_question_generation
import test_extraction_checkpoints as extraction_tests


class MoodleCategoryTests(unittest.TestCase):
    def test_builds_exam_year_and_language_category(self):
        self.assertEqual(
            build_exam_practice_paper_category_path(
                "Full Length Practice Papers",
                "NEET UG",
                "2027",
                ["English", "Bengali"],
            ),
            "$module$/top/Full Length Practice Papers/neet-ug/2027/english_bengali",
        )
        with self.assertRaisesRegex(ValueError, "four-digit"):
            build_exam_practice_paper_category_path(
                "Full Length Practice Papers", "NEET UG", "27", ["english"]
            )

    def test_paper_exam_categories_use_canonical_slugs(self):
        for exam, slug in (
            ("NEET UG", "neet-ug"),
            ("NEET", "neet-ug"),
            ("JEE Main", "jee-main"),
            ("JEE Advanced", "jee-advanced"),
            ("WBJEE", "wbjee"),
        ):
            with self.subTest(exam=exam):
                self.assertEqual(
                    build_exam_practice_paper_category_path(
                        "Papers", exam, "2027", ["english"]
                    ),
                    f"$module$/top/Papers/{slug}/2027/english",
                )

    def test_source_relative_path_and_language_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "neet_ug" / "ncert_exemplar" / "class11" / "biology" / "keep401.pdf"
            self.assertEqual(
                build_category_path(
                    "Chapterwise Question Bank", root, source, ["English", " Bengali "]
                ),
                "$module$/top/Chapterwise Question Bank/"
                "neet_ug/ncert_exemplar/class11/biology/keep401/english_bengali",
            )
            with self.assertRaisesRegex(ValueError, "not under"):
                build_category_path("Bank", root, root.parent / "other.pdf", ["english"])
            with self.assertRaisesRegex(ValueError, "single category"):
                build_category_path("Bank/Other", root, source, ["english"])

    def test_replaces_existing_markers_and_preserves_question_content(self):
        source = (
            '<quiz><question type="category"><category><text>old</text></category></question>'
            '<question type="multichoice"><questiontext><text><![CDATA[<p>A &amp; B</p>]]>'
            '</text></questiontext></question></quiz>'
        )
        result = set_moodle_category(source, "$module$/top/A & B/chapter/english")
        quiz = ET.fromstring(result)
        self.assertEqual([q.get("type") for q in quiz], ["category", "multichoice"])
        self.assertEqual(quiz[0].findtext("category/text"), "$module$/top/A & B/chapter/english")
        self.assertEqual(quiz[1].findtext("questiontext/text"), "<p>A &amp; B</p>")
        self.assertIn("<![CDATA[", result)

    def test_mirrored_runners_forward_category_and_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sources"
            source.mkdir()
            (source / "chapter.pdf").write_bytes(b"pdf")
            (source / "chapter.md").write_text("# Chapter", encoding="utf-8")
            spec = source / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            for runner, root_option in (
                (run_path_mirrored_extraction, "--source-root"),
                (run_path_mirrored_question_generation, "--source-root"),
                (run_path_mirrored_paper_generation, "--source-root"),
            ):
                with self.subTest(runner=runner.__name__), contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(
                        runner.main([
                            root_option, str(source), "--output-root", str(root / "output"),
                            "--top-category", "Question Bank", "--languages", "english,bengali",
                            "--dry-run",
                        ]),
                        0,
                    )
                    self.assertIn("--top-category", output.getvalue())
                    self.assertIn("--source-root", output.getvalue())
                    self.assertIn("english,bengali", output.getvalue())

    def test_extraction_rejects_removed_repair_options(self):
        for option in ("--repair", "--repair-backup-root"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as result:
                    run_path_mirrored_extraction.main([
                        "--source-root", ".", "--output-root", "output",
                        "--top-category", "Question Bank", option,
                    ])
                self.assertEqual(result.exception.code, 2)
                self.assertIn("unrecognized arguments", errors.getvalue())

    def test_subtree_limits_all_mirrored_runners_but_keeps_original_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sources"
            selected = source / "neet_ug" / "ncert_exemplar" / "class11" / "biology"
            other = source / "other"
            selected.mkdir(parents=True)
            other.mkdir()
            pdf = selected / "keep401.pdf"
            pdf.write_bytes(b"pdf")
            (other / "other.pdf").write_bytes(b"pdf")
            spec = selected / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            (other / "batch.yaml").write_text("exam_name: Other\ntest_count: 1\npapers: []\n", encoding="utf-8")
            subtree = selected.relative_to(source)

            for runner, execute, expected_source, output_root, expected_output in (
                (run_path_mirrored_extraction, "execute_pdf_command", pdf, root / "extract", root / "extract" / subtree),
                (run_path_mirrored_question_generation, "execute_command", pdf, root / "questions", root / "questions" / subtree / "keep401"),
                (
                    run_path_mirrored_paper_generation,
                    "execute_command",
                    spec,
                    root / "papers",
                    root / "papers" / "neet-ug" / "ncert_exemplar" / "class11" / "biology",
                ),
            ):
                with self.subTest(runner=runner.__name__), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()), \
                     patch.object(runner, execute, return_value=0) as execute_mock:
                    args = [
                        "--source-root", str(source), "--subtree", str(subtree),
                        "--output-root", str(output_root),
                        "--top-category", "NCERT Exemplar Question Bank",
                    ]
                    if runner is run_path_mirrored_extraction:
                        args.append("--resume")
                    self.assertEqual(runner.main(args), 0)
                    execute_mock.assert_called_once()
                    command, input_path = execute_mock.call_args.args
                    self.assertEqual(input_path, expected_source)
                    self.assertEqual(Path(command[command.index("--source-root") + 1]), source)
                    self.assertEqual(Path(command[command.index("--output-dir") + 1]), expected_output)
                    if runner is run_path_mirrored_extraction:
                        self.assertIn("--resume", command)
                    self.assertEqual(
                        build_category_path("NCERT Exemplar Question Bank", source, input_path, ["english"]),
                        "$module$/top/NCERT Exemplar Question Bank/"
                        + input_path.relative_to(source).with_suffix("").as_posix()
                        + "/english",
                    )

    def test_subtree_rejects_invalid_paths_and_conflicting_paper_spec(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sources"
            source.mkdir()
            (source / "file.pdf").write_bytes(b"pdf")
            for runner in (
                run_path_mirrored_extraction,
                run_path_mirrored_question_generation,
                run_path_mirrored_paper_generation,
            ):
                for subtree in (root, Path(".."), Path("missing"), Path("file.pdf")):
                    with self.subTest(runner=runner.__name__, subtree=subtree), \
                         contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            runner.main([
                                "--source-root", str(source), "--subtree", str(subtree),
                                "--output-root", str(root / "out"), "--top-category", "Bank",
                            ])
                        self.assertEqual(error.exception.code, 2)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_path_mirrored_paper_generation.main([
                    "--source-root", str(source), "--subtree", ".", "--spec", "batch.yaml",
                    "--output-root", str(root / "out"), "--top-category", "Bank",
                ])
            self.assertFalse((root / "out").exists())

    def test_subtree_rejects_symlink_outside_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "sources"
            outside = root / "outside"
            source.mkdir()
            outside.mkdir()
            link = source / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_path_mirrored_extraction.main([
                    "--source-root", str(source), "--subtree", "link",
                    "--output-root", str(root / "out"), "--top-category", "Bank",
                ])
            self.assertFalse((root / "out").exists())

    def test_subtree_extraction_resumes_completed_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "sources"
            subtree = Path("neet_ug") / "ncert_exemplar" / "class11" / "biology"
            source = source_root / subtree / "keep401.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"test pdf")
            other = source_root / "other" / "other.pdf"
            other.parent.mkdir()
            other.write_bytes(b"other pdf")
            output_root = root / "output"
            output_dir = output_root / subtree
            helpers = extraction_tests.ExtractionCheckpointTests()
            class ExemplarCommunicator(extraction_tests.FakeCommunicator):
                def generate(self, **kwargs):
                    page = kwargs["contents"].page_range[0]
                    self.calls.append(kwargs["contents"].page_range)
                    return f"<quiz>{extraction_tests.exemplar_question(f'Question {page}')}</quiz>"

            original = helpers.make_extractor(ExemplarCommunicator())
            original.standards = "neet_ug"
            original.top_category = "Bank"
            original.source_root = source_root
            xml = helpers.run_file(original, source, output_dir)
            before = xml.read_bytes()
            resumed = helpers.make_extractor(ExemplarCommunicator(), resume=True)

            def execute(command, pdf):
                resumed.standards = command[command.index("--standards") + 1]
                resumed.tags = command[command.index("--tags") + 1]
                resumed.top_category = command[command.index("--top-category") + 1]
                resumed.source_root = Path(command[command.index("--source-root") + 1])
                self.assertIn("--resume", command)
                self.assertEqual(helpers.run_file(
                    resumed, pdf, Path(command[command.index("--output-dir") + 1])
                ), xml)
                return 0

            with contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(run_path_mirrored_extraction, "execute_pdf_command", side_effect=execute) as mock_execute:
                self.assertEqual(run_path_mirrored_extraction.main([
                    "--source-root", str(source_root), "--subtree", str(subtree),
                    "--output-root", str(output_root), "--top-category", "Bank",
                    "--batch-size", "1", "--resume",
                ]), 0)
            mock_execute.assert_called_once()
            self.assertFalse(resumed.communicator.calls)
            self.assertEqual(xml.read_bytes(), before)
            combined = ET.parse(
                output_root / combined_name_for_directory(output_root)
            ).getroot()
            self.assertEqual(
                combined[0].findtext("category/text"),
                "$module$/top/Bank/neet_ug/ncert_exemplar/class11/biology/keep401/english",
            )
            self.assertEqual(len([question for question in combined if question.get("type") != "category"]), 3)


if __name__ == "__main__":
    unittest.main()
