import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_path_mirrored_question_generation import build_output_dir
from academic_content_pipeline.content_processors import QuestionGenerator, TutorialGenerator
from academic_content_pipeline.unified_pipeline import main as pipeline_main


class QuestionGenerationOutputDirTests(unittest.TestCase):
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
                        render=lambda result, directory, stem, *args: directory / stem
                    )
                    generator.process_file(source, output)
                    snapshot = communicator.requests[0]["prompt_snapshot_path"]
                    self.assertEqual(
                        snapshot.name, f"chapter_neet_ug_{suffix}_{label}_prompt.md"
                    )
                    self.assertTrue(snapshot.is_file())
            prompt_dir = output / "chapter" / ".tmp" / "prompts"
            self.assertEqual(len(list(prompt_dir.glob("*_prompt.md"))), 6)

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
