import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_path_mirrored_question_generation import build_output_dir


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


if __name__ == "__main__":
    unittest.main()
