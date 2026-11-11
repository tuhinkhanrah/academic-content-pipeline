import unittest
from pathlib import Path

from academic_content_pipeline.chapter_generation import (
    allocate_matrix,
    group_chapters,
    parse_syllabus_chapters,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYLLABUS_ROOT = REPOSITORY_ROOT / "prompts" / "generator" / "syllabus"


class ChapterGenerationTests(unittest.TestCase):
    def test_all_current_exam_syllabuses_have_deterministic_chapters(self):
        for exam in ("jee_advanced", "jee_main", "neet_ug", "wbjee"):
            for path in sorted((SYLLABUS_ROOT / exam).glob("*.md")):
                with self.subTest(syllabus=path):
                    chapters = parse_syllabus_chapters(path)
                    self.assertTrue(chapters)
                    self.assertEqual(len({chapter["id"] for chapter in chapters}), len(chapters))
                    self.assertTrue(all(chapter["content"].startswith("#") for chapter in chapters))

    def test_integer_matrix_preserves_exact_margins(self):
        matrix = allocate_matrix([5, 3, 2], [4, 3, 3])

        self.assertEqual([sum(row) for row in matrix], [5, 3, 2])
        self.assertEqual(
            [
                sum(matrix[row_index][column_index] for row_index in range(len(matrix)))
                for column_index in range(len(matrix[0]))
            ],
            [4, 3, 3],
        )

    def test_groups_chapters_without_exceeding_target(self):
        chapters = [
            {"id": "ch001", "title": "One", "content": "One"},
            {"id": "ch002", "title": "Two", "content": "Two"},
            {"id": "ch003", "title": "Three", "content": "Three"},
        ]

        groups = group_chapters(
            chapters,
            {"ch001": 4, "ch002": 5, "ch003": 2},
            max_questions=6,
        )

        self.assertEqual(
            [
                ([chapter["id"] for chapter in group["chapters"]], group["question_count"])
                for group in groups
            ],
            [(["ch001"], 4), (["ch002"], 5), (["ch003"], 2)],
        )


if __name__ == "__main__":
    unittest.main()
