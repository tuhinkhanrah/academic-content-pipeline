import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_path_mirrored_question_generation import build_output_dir
from run_path_mirrored_tutorial import main as tutorial_batch_main
from academic_content_pipeline.content_processors import (
    QuestionGenerator,
    TutorialGenerator,
    build_tutorial_language_instruction,
)
from academic_content_pipeline.unified_pipeline import main as pipeline_main


class QuestionGenerationOutputDirTests(unittest.TestCase):
    def test_question_generation_without_top_category(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "exam" / "chapter.md"
            source.parent.mkdir()
            source.write_text("# Chapter", encoding="utf-8")
            generator = QuestionGenerator(
                communicator=SimpleNamespace(generate=lambda **kwargs: (
                    '<quiz><question type="multichoice"><name><text>One</text></name>'
                    '</question></quiz>'
                )),
                ocr_engine=object(),
                languages=["english"],
                source_root=root,
            )
            result = generator.process_file(source, root / "out")
            path = result[0] if isinstance(result, list) else result
            self.assertEqual(
                ET.parse(path).getroot()[0].findtext("category/text"),
                "$module$/top/exam/chapter/english",
            )

    def test_separate_type_translation_uses_cached_master_without_category(self):
        xml = '<quiz><question type="multichoice"><name><text>One</text></name></question></quiz>'
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.md"
            source.write_text("# Chapter", encoding="utf-8")
            spec = root / "spec.yaml"
            spec.write_text(
                "total_questions: 1\nquestion_types:\n  mode: exact\n"
                "  types:\n  - type: standard_mcq\n    count: 1\n",
                encoding="utf-8",
            )
            options = dict(
                communicator=SimpleNamespace(generate=lambda **kwargs: xml),
                ocr_engine=object(),
                languages=["english"],
                spec_path=spec,
                separate_by_question_type=True,
                top_category="Bank",
                source_root=root,
            )
            QuestionGenerator(**options).process_file(source, root / "out")
            seen = []

            def translate(communicator, artifact, *args, **kwargs):
                seen.append(ET.fromstring(artifact))
                return artifact

            with patch("academic_content_pipeline.content_processors.translate_assessment_artifact", side_effect=translate):
                paths = QuestionGenerator(**options, translate_to=["bengali"]).process_file(source, root / "out")
            self.assertEqual(len(seen), 1)
            self.assertEqual([q.get("type") for q in seen[0]], ["multichoice"])
            translated = next(path for path in paths if "english_bengali" in path.name)
            self.assertEqual(
                ET.parse(translated).getroot()[0].findtext("category/text"),
                "$module$/top/Bank/chapter/english_bengali",
            )

    def test_split_and_translated_outputs_have_language_specific_categories(self):
        xml = (
            '<quiz><question type="multichoice"><name><text>One</text></name></question>'
            '<question type="multichoice"><name><text>Two</text></name></question></quiz>'
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "neet_ug" / "chapter.md"
            source.parent.mkdir()
            source.write_text("# Chapter", encoding="utf-8")
            generator = QuestionGenerator(
                communicator=SimpleNamespace(generate=lambda **kwargs: xml),
                ocr_engine=object(),
                languages=["english"],
                num_questions=2,
                num_papers=2,
                translate_to=["Bengali"],
                top_category="Chapterwise Question Bank",
                source_root=root,
            )
            with patch(
                "academic_content_pipeline.content_processors.translate_assessment_artifact",
                side_effect=lambda communicator, artifact, *args, **kwargs: artifact,
            ):
                paths = generator.process_file(source, root / "out")

            self.assertEqual(len(paths), 4)
            for path, language in zip(paths, ("english", "english", "english_bengali", "english_bengali")):
                quiz = ET.parse(path).getroot()
                self.assertEqual(
                    quiz[0].findtext("category/text"),
                    f"$module$/top/Chapterwise Question Bank/neet_ug/chapter/{language}",
                )
                self.assertEqual([q.get("type") for q in quiz], ["category", "multichoice"])

    def test_chapter_code_inputs_get_a_dedicated_output_directory(self):
        source_root = Path("source")
        input_path = (
            source_root
            / "neet_ug"
            / "ncert"
            / "class11"
            / "biology"
            / "kebo1dd"
            / "kebo101.pdf"
        )

        self.assertEqual(
            Path("output") / "neet_ug" / "ncert" / "class11" / "biology" / "kebo1dd" / "kebo101",
            build_output_dir(source_root, input_path, Path("output")),
        )

    def test_non_chapter_inputs_keep_the_mirrored_parent_directory(self):
        source_root = Path("source")
        input_path = source_root / "neet_ug" / "papers" / "neet_2024.pdf"

        self.assertEqual(
            Path("output") / "neet_ug" / "papers",
            build_output_dir(source_root, input_path, Path("output")),
        )

    def test_question_and_tutorial_prompt_names_include_languages(self):
        class Communicator:
            def __init__(self):
                self.requests = []

            def generate(self, **kwargs):
                self.requests.append(kwargs)
                return "<quiz />"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.md"
            source.write_text("# Chapter\nContent", encoding="utf-8")
            output = root / "output"
            for languages, suffix in (
                (["english"], "english"),
                (["english", "bengali"], "english_bengali"),
                (["english", "hindi"], "english_hindi"),
            ):
                for generator_class, label in (
                    (QuestionGenerator, "generation"),
                    (TutorialGenerator, "tutorial"),
                ):
                    communicator = Communicator()
                    generator = generator_class(
                        communicator=communicator,
                        ocr_engine=object(),
                        languages=languages,
                        standards="neet_ug",
                    )
                    generator.renderer = SimpleNamespace(
                        render=lambda result, directory, stem, *args, **kwargs: directory / f"{stem}.pdf",
                        render_html=lambda result, directory, stem, *args, **kwargs: directory / f"{stem}.html",
                    )
                    generator.process_file(source, output)
                    snapshot = communicator.requests[0]["prompt_snapshot_path"]
                    self.assertEqual(
                        snapshot.name, f"chapter_neet_ug_{suffix}_{label}_prompt.md"
                    )
                    self.assertTrue(snapshot.is_file())
            prompt_dir = output / "chapter" / ".tmp" / "prompts"
            self.assertEqual(len(list(prompt_dir.glob("*_prompt.md"))), 6)

    def test_tutorial_originality_guidance_reaches_model_for_pdf_and_markdown(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image = root / "figure.png"
            image.write_bytes(b"reference image")
            for suffix, languages in (
                (".pdf", ["bengali"]),
                (".md", ["english"]),
                (".md", ["english", "bengali"]),
            ):
                with self.subTest(suffix=suffix, languages=languages):
                    source = root / f"chapter{suffix}"
                    source.write_text("Chapter concepts", encoding="utf-8")
                    requests = []
                    ocr = SimpleNamespace(
                        process_pdf=lambda **kwargs: SimpleNamespace(
                            full_markdown="Chapter concepts",
                            all_images={"figure": str(image)},
                        )
                    )
                    generator = TutorialGenerator(
                        communicator=SimpleNamespace(
                            generate=lambda **kwargs: requests.append(kwargs) or "<html></html>"
                        ),
                        ocr_engine=ocr,
                        languages=languages,
                    )
                    generator.renderer = SimpleNamespace(
                        render=lambda result, directory, stem, *args, **kwargs: directory / f"{stem}.pdf",
                        render_html=lambda result, directory, stem, *args, **kwargs: directory / f"{stem}.html",
                    )
                    generator.process_file(source, root / "output")

                    request = requests[0]
                    system = request["system_instruction"]
                    batch = request["contents"]
                    self.assertIn("[ORIGINALITY AND SOURCE USE]", system)
                    self.assertIn("do not trace or imitate", system)
                    self.assertIn("# Originality and Source Use", batch.text)
                    self.assertIn("Invent fresh scenarios and numerical values", batch.text)
                    self.assertIn("Chapter concepts", batch.text)
                    self.assertEqual(len(batch.images), 1 if suffix == ".pdf" else 0)
                    if suffix == ".pdf":
                        self.assertEqual(batch.images[0].reference_id, "figure")
                        self.assertIn("use an independent tutorial structure", batch.text)
                        self.assertNotIn("preserve the chapter structure", batch.text)

    def test_single_language_tex_tutorial_keeps_existing_structure_instruction(self):
        instruction = build_tutorial_language_instruction(["bengali"], "tex")
        self.assertIn("preserve the chapter structure", instruction)

    def test_tutorial_is_rendered_in_source_work_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            source = source_root / "biology" / "kebo1dd" / "kebo101.md"
            source.parent.mkdir(parents=True)
            source.write_text("Chapter concepts", encoding="utf-8")
            output = root / "output"
            work_dir = output / "biology" / "kebo1dd" / "kebo101"
            for render_pdf, extension in ((False, "html"), (True, "pdf")):
                with self.subTest(render_pdf=render_pdf):
                    generator = TutorialGenerator(
                        communicator=SimpleNamespace(generate=lambda **kwargs: "<html></html>"),
                        ocr_engine=object(),
                        languages=["english"],
                        standards="neet_ug",
                        generate_pdf=render_pdf,
                    )
                    rendered = []
                    generator.renderer = SimpleNamespace(
                        render=lambda result, directory, stem, *args, **kwargs: rendered.append("pdf") or directory / f"{stem}.pdf",
                        render_html=lambda result, directory, stem, *args, **kwargs: rendered.append("html") or directory / f"{stem}.html",
                    )

                    path = generator.process_file(source, output, input_root=source_root)

                    self.assertEqual(path, work_dir / f"kebo101_neet_ug_english.{extension}")
                    self.assertEqual(rendered, [extension])
            self.assertTrue((work_dir / ".tmp" / "prompts").is_dir())
            self.assertTrue((work_dir / "markdown").is_dir())
            self.assertFalse((work_dir / "kebo101").exists())

    def test_tutorial_batch_skips_only_output_in_chapter_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "neet_ug"
            source = source_root / "biology" / "kebo101.md"
            source.parent.mkdir(parents=True)
            source.write_text("Chapter concepts", encoding="utf-8")
            output = root / "output"
            output_parent = output / "biology"
            output_parent.mkdir(parents=True)
            (output_parent / "kebo101_neet_ug_english.pdf").write_bytes(b"old")
            args = ["--source-root", str(source_root), "--output-root", str(output)]

            with patch("run_path_mirrored_tutorial.execute_command", return_value=0) as execute:
                self.assertEqual(tutorial_batch_main(args), 0)
                self.assertEqual(execute.call_count, 1)
                command = execute.call_args.args[0]
                self.assertNotIn("--generate-pdf", command)

            target = output_parent / "kebo101" / "kebo101_neet_ug_english.html"
            target.parent.mkdir()
            target.write_bytes(b"existing")
            with patch("run_path_mirrored_tutorial.execute_command", return_value=0) as execute:
                self.assertEqual(tutorial_batch_main(args), 0)
                execute.assert_not_called()
                self.assertEqual(tutorial_batch_main(args + ["--force"]), 0)
                self.assertEqual(execute.call_count, 1)

            with patch("run_path_mirrored_tutorial.execute_command", return_value=0) as execute:
                self.assertEqual(tutorial_batch_main(args + ["--generate-pdf"]), 0)
                self.assertEqual(execute.call_count, 1)
                self.assertIn("--generate-pdf", execute.call_args.args[0])

            (output_parent / "kebo101" / "kebo101_neet_ug_english.pdf").write_bytes(b"pdf")
            with patch("run_path_mirrored_tutorial.execute_command", return_value=0) as execute:
                self.assertEqual(tutorial_batch_main(args + ["--generate-pdf"]), 0)
                execute.assert_not_called()

    def test_html_renderer_writes_html_without_pdf_compilation(self):
        from academic_content_pipeline.output_renderers import OutputRenderer

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            renderer = OutputRenderer("pdf", "html")

            with patch("academic_content_pipeline.pipeline_utils.subprocess.run") as run:
                path = renderer.render_html(
                    "<!DOCTYPE html><html><body>ok</body></html>",
                    output,
                    "tutorial",
                )

            self.assertEqual(path, output / "tutorial.html")
            self.assertIn("ok", path.read_text(encoding="utf-8"))
            self.assertFalse((output / "tutorial.pdf").exists())
            run.assert_not_called()

    def test_pdf_renderer_still_saves_html_and_invokes_pdf_compiler(self):
        from academic_content_pipeline.output_renderers import OutputRenderer

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            renderer = OutputRenderer("pdf", "html")
            with patch("academic_content_pipeline.pipeline_utils.subprocess.run") as run:
                path = renderer.render("<!DOCTYPE html><html><body>ok</body></html>", output, "tutorial")

            self.assertEqual(path, output / "tutorial.pdf")
            self.assertTrue((output / "tutorial.html").is_file())
            self.assertEqual(run.call_count, 1)

    def test_unified_tutorial_cli_forwards_generate_pdf_option(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter.md"
            source.write_text("Chapter concepts", encoding="utf-8")
            for generate_pdf in (False, True):
                with self.subTest(generate_pdf=generate_pdf):
                    argv = [
                        "pipeline",
                        "context",
                        "generate-tutorial",
                        "--input-file",
                        str(source),
                        "--output-dir",
                        str(root / "output"),
                    ]
                    if generate_pdf:
                        argv.append("--generate-pdf")
                    with patch("sys.argv", argv), \
                         patch.dict("os.environ", {"GEMINI_API_KEY": "test", "MISTRAL_API_KEY": "test"}), \
                         patch("academic_content_pipeline.unified_pipeline.setup_logger"), \
                         patch("academic_content_pipeline.unified_pipeline.build_communicator"), \
                         patch("academic_content_pipeline.unified_pipeline.MistralOCREngine"), \
                         patch("academic_content_pipeline.unified_pipeline.TutorialGenerator") as generator_class:
                        pipeline_main()

                    self.assertEqual(
                        generator_class.call_args.kwargs["generate_pdf"],
                        generate_pdf,
                    )

    def test_tutorial_batch_render_pdf_keeps_output_beside_html(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            html = root / "chapter" / "tutorial.html"
            html.parent.mkdir()
            html.write_text("<html></html>", encoding="utf-8")
            with patch("run_path_mirrored_tutorial.execute_command", return_value=0) as execute:
                self.assertEqual(tutorial_batch_main(["--source-root", str(root), "--render-pdf"]), 0)
            command, input_path = execute.call_args.args
            self.assertEqual(input_path, html)
            self.assertIn("--render-pdf", command)
            self.assertNotIn("--output-dir", command)

    def test_translation_command_writes_language_specific_chunk_prompts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "chapter_english.xml"
            source_xml = (
                '<quiz><question type="multichoice"><name><text>Q</text></name>'
                '<questiontext><text>Question</text></questiontext>'
                '<generalfeedback><text>Solution</text></generalfeedback>'
                '<answer fraction="100"><text>A</text></answer></question></quiz>'
            )
            source.write_text(source_xml, encoding="utf-8")
            requests = []
            communicator = SimpleNamespace(
                generate=lambda **kwargs: requests.append(kwargs) or source_xml
            )
            argv = [
                "pipeline", "translate-artifacts", "--input-file", str(source),
                "--output-dir", str(root / "output"), "--translate-to", "bengali,hindi",
            ]
            with patch("sys.argv", argv), \
                 patch.dict("os.environ", {"GEMINI_API_KEY": "test"}), \
                 patch("academic_content_pipeline.unified_pipeline.setup_logger"), \
                 patch("academic_content_pipeline.unified_pipeline.build_communicator", return_value=communicator):
                pipeline_main()

            self.assertEqual(
                [request["prompt_snapshot_path"].name for request in requests],
                [
                    "chapter_english_bengali_translation_prompt_part_001_of_001.md",
                    "chapter_english_hindi_translation_prompt_part_001_of_001.md",
                ],
            )
            self.assertTrue(all(request["prompt_snapshot_path"].is_file() for request in requests))


if __name__ == "__main__":
    unittest.main()
