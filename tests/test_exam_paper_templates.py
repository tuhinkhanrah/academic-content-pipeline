import unittest
import tempfile
from pathlib import Path

from academic_content_pipeline.content_processors import (
    build_global_difficulty_hint,
    build_paper_metadata,
    build_subject_difficulty_contract,
    build_question_type_contract,
)
from academic_content_pipeline.paper_batch import PaperBatchRunner, _read_spec
from academic_content_pipeline.template_tools import (
    DEFAULT_MANIFEST_PATH,
    generate_exam_templates,
    validate_exam_templates,
)
from academic_content_pipeline.pipeline_utils import format_instruction_profile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "academic_content_pipeline"
    / "resources"
    / "spec_templates"
)
MCQ_TYPES_PATH = (
    REPOSITORY_ROOT
    / "src"
    / "academic_content_pipeline"
    / "resources"
    / "catalogs"
    / "mcq_types.yaml"
)


class ExamPaperTemplateTests(unittest.TestCase):
    def test_jee_main_physics_diagram_allocation(self):
        paper = _read_spec(TEMPLATE_ROOT / "jee_main" / "jee_main.template.yaml")
        for sections in (paper["sections"], paper["instruction_profile"]["sections"]):
            section = next(item for item in sections if item["id"] == "physics_section_a")
            counts = {item["type"]: item["count"] for item in section["question_types"]}
            self.assertEqual(counts["diagram_image"], 7)
            self.assertEqual(counts["standard_mcq"], 8)
            self.assertEqual(sum(counts.values()), 20)
        bank = _read_spec(
            TEMPLATE_ROOT / "jee_main" / "physics_chapter_bank.template.yaml"
        )
        weights = {
            item["type"]: item["weight"] for item in bank["question_types"]["types"]
        }
        self.assertAlmostEqual(weights["diagram_image"], 7 / 25)
        self.assertAlmostEqual(weights["standard_mcq"], 8 / 25)

    def test_physics_diagram_allocations_across_exams(self):
        cases = {
            "neet_ug": ({"neet_ug.template.yaml": (12, 45)}, 12 / 45),
            "jee_main": ({"jee_main.template.yaml": (7, 25)}, 7 / 25),
            "jee_advanced": (
                {"paper1.template.yaml": (3, 16), "paper2.template.yaml": (3, 18)},
                6 / 34,
            ),
            "wbjee": ({"physics_chemistry.template.yaml": (11, 40)}, 11 / 40),
        }
        for exam, (papers, expected_weight) in cases.items():
            with self.subTest(exam=exam):
                for filename, (expected_diagrams, expected_total) in papers.items():
                    paper = _read_spec(TEMPLATE_ROOT / exam / filename)
                    for sections in (
                        paper["sections"],
                        paper["instruction_profile"]["sections"],
                    ):
                        physics_sections = [
                            section for section in sections
                            if section.get("subject", "all") in {"Physics", "all"}
                        ]
                        self.assertEqual(
                            sum(section["question_count"] for section in physics_sections),
                            expected_total,
                        )
                        self.assertEqual(
                            sum(
                                allocation["count"]
                                for section in physics_sections
                                for allocation in section["question_types"]
                                if allocation["type"] == "diagram_image"
                            ),
                            expected_diagrams,
                        )
                bank = _read_spec(TEMPLATE_ROOT / exam / "physics_chapter_bank.template.yaml")
                weights = {
                    item["type"]: item["weight"]
                    for item in bank["question_types"]["types"]
                }
                self.assertAlmostEqual(weights["diagram_image"], expected_weight, places=6)

    def test_instruction_profile_converts_scoring_to_moodle_xml_fields(self):
        profile = {
            "sections": [
                {
                    "name": "Physics",
                    "subject": "Physics",
                    "question_count": 45,
                    "scoring": {
                        "correct": 4,
                        "incorrect": -1,
                        "unanswered": 0,
                    },
                }
            ]
        }

        xml_instructions = format_instruction_profile(profile, output_format="xml")
        pdf_instructions = format_instruction_profile(profile, output_format="pdf")

        self.assertIn(
            "<defaultgrade>4.0000000</defaultgrade>, "
            "<penalty>0.2500000</penalty>",
            xml_instructions,
        )
        self.assertIn(
            "override any generic XML template defaults",
            xml_instructions,
        )
        self.assertIn(
            "do not use a generic defaultgrade value such as 1.0000000",
            xml_instructions,
        )
        self.assertIn(
            "absolute incorrect-answer mark divided by the correct-answer mark",
            xml_instructions,
        )
        self.assertNotIn("<defaultgrade>", pdf_instructions)

    def test_difficulty_contract_explains_ratios_as_exact_subject_quotas(self):
        spec = {}
        subjects = [{"name": "Physics", "total_questions": 45}]

        contract = build_subject_difficulty_contract(spec, subjects)
        fallback = build_global_difficulty_hint(spec)

        self.assertIn(
            "Easy = exactly 9 questions (20%); Medium = exactly 23 questions (50%); "
            "Hard = exactly 13 questions (30%)",
            contract,
        )
        self.assertIn("counts for each subject must sum", contract)
        self.assertIn("percentages as question counts", contract)
        self.assertIn("Fallback proportions (shares of questions", fallback)
        self.assertIn("Easy 20%, Medium 50%, Hard 30%", fallback)

    def test_template_generation_and_validator_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "templates"
            generated = generate_exam_templates(
                manifest_path=DEFAULT_MANIFEST_PATH,
                output_dir=output_dir,
            )
            self.assertEqual(len(generated), 22)
            self.assertEqual(
                validate_exam_templates(
                    manifest_path=DEFAULT_MANIFEST_PATH,
                    template_dir=output_dir,
                ),
                [],
            )

            with self.assertRaises(FileExistsError):
                generate_exam_templates(
                    manifest_path=DEFAULT_MANIFEST_PATH,
                    output_dir=output_dir,
                )

            bank_path = (
                output_dir
                / "neet_ug"
                / "biology_chapter_bank.template.yaml"
            )
            bank = _read_spec(bank_path)
            bank["question_types"]["types"][0]["weight"] += 0.1
            bank_path.write_text(
                __import__("yaml").safe_dump(bank, sort_keys=False),
                encoding="utf-8",
            )
            errors = validate_exam_templates(
                manifest_path=DEFAULT_MANIFEST_PATH,
                template_dir=output_dir,
            )
            self.assertTrue(any("biology_chapter_bank" in error for error in errors))

    def test_all_subject_chapter_bank_templates_match_exam_rules(self):
        banks = {
            "neet_ug": {
                "physics": ("physics.md", ["neet_ug.md"]),
                "chemistry": ("chemistry.md", ["neet_ug.md"]),
                "biology": ("biology.md", ["neet_ug.md"]),
            },
            "jee_main": {
                "mathematics": ("mathematics.md", ["jee_main.md"]),
                "physics": ("physics.md", ["jee_main.md"]),
                "chemistry": ("chemistry.md", ["jee_main.md"]),
            },
            "jee_advanced": {
                "mathematics": ("mathematics.md", ["paper1.md", "paper2.md"]),
                "physics": ("physics.md", ["paper1.md", "paper2.md"]),
                "chemistry": ("chemistry.md", ["paper1.md", "paper2.md"]),
            },
            "wbjee": {
                "mathematics": ("mathematics.md", ["mathematics.md"]),
                "physics": ("physics.md", ["physics_chemistry.md"]),
                "chemistry": ("chemistry.md", ["physics_chemistry.md"]),
            },
        }

        for exam, subjects in banks.items():
            for subject, (syllabus_name, instruction_names) in subjects.items():
                with self.subTest(exam=exam, subject=subject):
                    spec_path = (
                        TEMPLATE_ROOT
                        / exam
                        / f"{subject}_chapter_bank.template.yaml"
                    )
                    spec = _read_spec(spec_path)
                    self.assertEqual(spec["total_questions"], 120)
                    profile = spec["question_types"]
                    self.assertEqual(profile["mode"], "adaptive")
                    if exam in {"jee_advanced", "jee_main", "wbjee"} and subject in {
                        "physics",
                        "chemistry",
                    }:
                        configured_types = {
                            item["type"] for item in profile["types"]
                        }
                        self.assertTrue(
                            {
                                "diagram_image",
                                "match_list",
                                "statement_based_2",
                            }.issubset(configured_types)
                        )
                    self.assertAlmostEqual(
                        sum(item["weight"] for item in profile["types"]),
                        1.0,
                    )
                    build_question_type_contract(
                        spec,
                        MCQ_TYPES_PATH,
                        spec["total_questions"],
                    )
                    syllabus_path = (
                        REPOSITORY_ROOT
                        / "prompts"
                        / "generator"
                        / "syllabus"
                        / exam
                        / syllabus_name
                    )
                    self.assertTrue(syllabus_path.is_file(), syllabus_path)
                    for instruction_name in instruction_names:
                        instruction_path = (
                            REPOSITORY_ROOT
                            / "prompts"
                            / "instructions"
                            / exam
                            / instruction_name
                        )
                        self.assertTrue(instruction_path.is_file(), instruction_path)

    def test_all_batch_templates_resolve_to_valid_paper_blueprints(self):
        batch_paths = [
            TEMPLATE_ROOT / "neet_ug" / "batch.template.yaml",
            TEMPLATE_ROOT / "jee_main" / "batch.template.yaml",
            TEMPLATE_ROOT / "jee_advanced" / "batch.template.yaml",
            TEMPLATE_ROOT / "wbjee" / "batch.template.yaml",
        ]
        runner = PaperBatchRunner(object())

        for batch_path in batch_paths:
            with self.subTest(batch=batch_path.parent.name):
                batch_spec = _read_spec(batch_path)
                self.assertEqual(batch_spec["test_count"], 100)
                papers = runner._resolve_papers(batch_path, batch_spec)
                self.assertEqual(len(papers), len(batch_spec["papers"]))

                for paper in papers:
                    spec_data = paper["spec_data"]
                    subjects = spec_data["subjects"]
                    total_questions = sum(
                        subject["total_questions"] for subject in subjects
                    )
                    self.assertEqual(paper["question_count"], total_questions)
                    self.assertEqual(
                        sum(
                            allocation["count"]
                            for allocation in spec_data["question_types"]["types"]
                        ),
                        total_questions,
                    )
                    metadata = build_paper_metadata(spec_data, subjects, None)
                    self.assertEqual(
                        metadata["total_marks"],
                        spec_data["total_marks"],
                    )
                    build_question_type_contract(
                        spec_data,
                        MCQ_TYPES_PATH,
                        total_questions,
                    )

                    for subject in subjects:
                        syllabus = REPOSITORY_ROOT / subject["syllabus_file"]
                        self.assertTrue(syllabus.is_file(), syllabus)

                    section_questions = {}
                    for section in spec_data["sections"]:
                        section_subject = section["subject"]
                        if section_subject == "all":
                            for subject in subjects:
                                section_questions[subject["name"]] = (
                                    section_questions.get(subject["name"], 0)
                                    + section["question_count"]
                                )
                        else:
                            section_questions[section_subject] = (
                                section_questions.get(section_subject, 0)
                                + section["question_count"]
                            )
                    self.assertEqual(
                        section_questions,
                        {
                            subject["name"]: subject["total_questions"]
                            for subject in subjects
                        },
                    )

    def test_templates_match_exam_paper_counts(self):
        expected = {
            "neet_ug": {"full_paper": 180},
            "jee_main": {"paper1": 75},
            "jee_advanced": {"paper1": 48, "paper2": 54},
            "wbjee": {"mathematics": 75, "physics_chemistry": 80},
        }
        runner = PaperBatchRunner(object())

        for exam, paper_counts in expected.items():
            with self.subTest(exam=exam):
                batch_path = TEMPLATE_ROOT / exam / "batch.template.yaml"
                papers = runner._resolve_papers(batch_path, _read_spec(batch_path))
                self.assertEqual(
                    {
                        paper["id"]: paper["question_count"]
                        for paper in papers
                    },
                    paper_counts,
                )

    def test_neet_full_paper_preserves_subject_chapter_bank_type_mix(self):
        neet_dir = TEMPLATE_ROOT / "neet_ug"
        paper = _read_spec(neet_dir / "neet_ug.template.yaml")
        for subject in paper["subjects"]:
            subject_key = subject["name"].casefold()
            chapter_bank = _read_spec(
                neet_dir / f"{subject_key}_chapter_bank.template.yaml"
            )
            total_questions = subject["total_questions"]
            weighted_counts = [
                (
                    allocation["type"],
                    allocation["weight"] * total_questions,
                )
                for allocation in chapter_bank["question_types"]["types"]
            ]
            expected_counts = {
                name: int(count) for name, count in weighted_counts
            }
            remainder = total_questions - sum(expected_counts.values())
            for name, _ in sorted(
                weighted_counts,
                key=lambda item: item[1] - int(item[1]),
                reverse=True,
            )[:remainder]:
                expected_counts[name] += 1

            section = next(
                section
                for section in paper["sections"]
                if section["subject"] == subject["name"]
            )
            self.assertEqual(
                {
                    allocation["type"]: allocation["count"]
                    for allocation in section["question_types"]
                },
                expected_counts,
            )

        self.assertEqual(
            sum(
                allocation["count"]
                for allocation in paper["question_types"]["types"]
            ),
            180,
        )


if __name__ == "__main__":
    unittest.main()
