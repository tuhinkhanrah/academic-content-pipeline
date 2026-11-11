import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_path_mirrored_paper_generation import (
    build_output_dir,
    command_for_spec,
    discover_batch_specs,
    infer_standard_from_path,
    main,
)
from academic_content_pipeline.unified_pipeline import main as pipeline_main


class PathMirroredPaperGenerationTests(unittest.TestCase):
    def test_discovers_batch_specs_but_not_referenced_paper_specs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            spec_root = Path(temporary_directory)
            exam_dir = spec_root / "wbjee"
            exam_dir.mkdir()
            (exam_dir / "batch.yaml").write_text(
                "exam_name: WBJEE\ntest_count: 100\npapers:\n"
                "  - id: mathematics\n    spec: mathematics.yaml\n",
                encoding="utf-8",
            )
            (exam_dir / "mathematics.yaml").write_text(
                "exam_name: WBJEE\nchapter_generation:\n"
                "  enabled: true\nsubjects: []\n",
                encoding="utf-8",
            )

            self.assertEqual(
                [exam_dir / "batch.yaml"],
                discover_batch_specs(spec_root),
            )

    def test_build_output_dir_mirrors_spec_parent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_root = root / "specs"
            spec_path = spec_root / "wbjee" / "batch.yaml"

            self.assertEqual(
                root / "outputs" / "wbjee",
                build_output_dir(spec_root, spec_path, root / "outputs"),
            )

    def test_infers_standard_from_spec_path(self):
        cases = {
            "jee_main/batch.template.yaml": "jee_main",
            "jee_advanced/paper1.template.yaml": "jee_advanced",
            "neet_ug/neet_ug.template.yaml": "neet_ug",
            "wbjee/mathematics.template.yaml": "wbjee",
            "other/custom_exam.yaml": "general",
        }
        for relative_path, expected_standard in cases.items():
            with self.subTest(relative_path=relative_path):
                self.assertEqual(
                    expected_standard,
                    infer_standard_from_path(Path(relative_path)),
                )

    def test_command_includes_batch_options(self):
        command = command_for_spec(
            Path("paper.yaml"),
            Path("output"),
            "agent",
            model_name="gemini-test",
            languages="english",
            standards="wbjee",
            tags="",
            difficulty_mix="easy:0.2,medium:0.5,hard:0.3",
            num_tests=1,
            max_paper_attempts=2,
            sample_pdf=None,
            exam_duration_minutes=None,
            resume=True,
            force=False,
        )

        self.assertIn("generate-paper", command)
        self.assertIn("--spec", command)
        self.assertEqual("1", command[command.index("--num-tests") + 1])
        self.assertEqual("2", command[command.index("--max-paper-attempts") + 1])
        self.assertIn("--resume", command)
        self.assertNotIn("--force", command)

    def test_dry_run_previews_batch_without_creating_output_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_root = root / "specs"
            exam_dir = spec_root / "neet"
            exam_dir.mkdir(parents=True)
            (exam_dir / "batch.yaml").write_text(
                "exam_name: NEET UG\ntest_count: 100\npapers: []\n",
                encoding="utf-8",
            )
            output_root = root / "output"
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "--spec-root",
                        str(spec_root),
                        "--output-root",
                        str(output_root),
                        "--dry-run",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertIn("Prepared 1 spec(s)", output.getvalue())
            self.assertFalse(output_root.exists())

    def test_reference_paper_is_forwarded_in_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exam_dir = root / "specs" / "neet"
            exam_dir.mkdir(parents=True)
            (exam_dir / "batch.yaml").write_text(
                "exam_name: NEET UG\ntest_count: 1\npapers: []\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                result = main([
                    "--spec-root", str(root / "specs"), "--output-root", str(root / "out"),
                    "--reference-paper", str(root / "reference.md"),
                    "--reference-analysis-mode", "full",
                    "--dry-run",
                ])
            self.assertEqual(result, 0)
            self.assertIn("--reference-paper", output.getvalue())
            self.assertIn("reference.md", output.getvalue())
            self.assertIn("--reference-analysis-mode full", output.getvalue())
            self.assertNotIn("--reference-batch-pages", output.getvalue())
            self.assertNotIn("--reference-batch-questions", output.getvalue())

    def test_multiple_paper_references_are_forwarded_in_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = root / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([
                    "--spec", str(spec), "--output-root", str(root / "out"),
                    "--reference-paper", "paper1=first.pdf",
                    "--reference-paper", "paper2=second.md", "--dry-run",
                ]), 0)
            self.assertEqual(output.getvalue().count("--reference-paper"), 2)
            self.assertIn("paper1=first.pdf", output.getvalue())
            self.assertIn("paper2=second.md", output.getvalue())

    def test_removed_reference_batch_options_are_rejected(self):
        for option in ("--reference-batch-pages", "--reference-batch-questions"):
            with self.subTest(option=option), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([option, "20", "--dry-run"])
                self.assertEqual(error.exception.code, 2)

    def test_template_reference_mode_and_disable_are_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = root / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            for disabled in (False, True):
                output = StringIO()
                args = [
                    "--spec", str(spec), "--output-root", str(root / "out"),
                    "--reference-analysis-mode", "full", "--dry-run",
                ]
                if disabled:
                    args.append("--no-reference-paper")
                with self.subTest(disabled=disabled), redirect_stdout(output):
                    self.assertEqual(main(args), 0)
                    self.assertIn("--reference-analysis-mode full", output.getvalue())
                    self.assertEqual("--no-reference-paper" in output.getvalue(), disabled)

    def test_disable_conflicts_with_explicit_reference(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            main(["--reference-paper", "reference.md", "--no-reference-paper"])
        self.assertEqual(error.exception.code, 2)

    def test_pipeline_cli_forwards_disable_and_analysis_mode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = root / "batch.yaml"
            spec.write_text("exam_name: Test\ntest_count: 1\npapers: []\n", encoding="utf-8")
            for prefix in (["generate-paper"], ["agent", "generate-paper"]):
                with self.subTest(prefix=prefix):
                    argv = [
                        "pipeline", *prefix, "--spec", str(spec), "--output-dir", str(root / "out"),
                        "--no-reference-paper", "--reference-analysis-mode", "full",
                    ]
                    with patch("sys.argv", argv), \
                         patch.dict("os.environ", {"GEMINI_API_KEY": "test", "MISTRAL_API_KEY": "test"}), \
                         patch("academic_content_pipeline.unified_pipeline.logger"), \
                         patch("academic_content_pipeline.unified_pipeline.setup_logger"), \
                         patch("academic_content_pipeline.unified_pipeline.build_communicator"), \
                         patch("academic_content_pipeline.unified_pipeline.MistralOCREngine"), \
                         patch("academic_content_pipeline.unified_pipeline.PaperGenerator") as generator, \
                         patch("academic_content_pipeline.unified_pipeline.PaperBatchRunner") as runner:
                        pipeline_main()
                    self.assertEqual(generator.call_args.kwargs["reference_analysis_mode"], "full")
                    self.assertTrue(runner.return_value.run.call_args.kwargs["no_reference_papers"])


if __name__ == "__main__":
    unittest.main()
