import json
import hashlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import yaml

from academic_content_pipeline.content_processors import PaperGenerator
from academic_content_pipeline.paper_batch import (
    REPOSITORY_ROOT,
    PaperBatchRunner,
    _combine_subject_xml,
    _portable_path_reference,
)


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
                "spec_path": Path(spec_path),
                "spec_data": yaml.safe_load(spec_path.read_text(encoding="utf-8")),
                "reference_profile": getattr(self, "reference_profile", None),
            }
        )
        spec_data = self.calls[-1]["spec_data"]
        subjects = spec_data.get("subjects", [])
        question_count = sum(
            int(subject.get("total_questions", 0)) for subject in subjects
        ) or int(spec_data.get("total_questions", 0))
        question_count = question_count or 1
        subject_names = [subject.get("name", "") for subject in subjects]
        chapter_quotas = re.findall(
            r"^- \[(?P<chapter_id>[^\]]+)\].*?: (?P<count>\d+) question\(s\)$",
            additional_prompt,
            flags=re.MULTILINE,
        )
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

        question_names = []
        if chapter_quotas:
            question_names.extend(
                f"[{chapter_id}] {response} {index}"
                for chapter_id, count in chapter_quotas
                for index in range(1, int(count) + 1)
            )
        else:
            question_names.extend(
                f"{subject_names[0] if subject_names else 'Question'} {index}"
                for index in range(1, question_count + 1)
            )
        questions = "".join(
            '<question type="multichoice"><name><text>'
            f"{name}</text></name>"
            '<questiontext format="html"><text>'
            f"{response} {index}"
            '</text></questiontext><answer fraction="100"><text>Answer</text>'
            '</answer></question>'
            for index, name in enumerate(question_names, start=1)
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

    def test_batch_reference_defaults_cli_precedence_and_disable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root, papers=[("paper1", "paper.yaml"), ("paper2", "paper.yaml")])
            refs = root / "refs"
            refs.mkdir()
            for name in ("default1", "default2", "override", "shared"):
                (refs / f"{name}.md").write_text(f"1. {name}", encoding="utf-8")
            spec.write_text(
                "exam_name: Sample Exam\ntest_count: 1\nuniqueness_scope: all_papers\npapers:\n"
                "  - id: paper1\n    spec: paper.yaml\n    reference_paper: refs/default1.md\n"
                "  - id: paper2\n    spec: paper.yaml\n    reference_paper: refs/default2.md\n",
                encoding="utf-8",
            )
            cases = [
                ("defaults", {}, None, False, ["default1", "default2"]),
                ("keyed", {"paper1": refs / "override.md"}, None, False, ["override", "default2"]),
                ("shared", {}, refs / "shared.md", False, ["shared", "shared"]),
                ("disabled", {}, None, True, [None, None]),
            ]
            for case, mapped, shared, disabled, expected in cases:
                with self.subTest(case=case):
                    generator = FakePaperGenerator(["distinct first", "distinct second"])
                    generator.reference_papers = mapped
                    generator.reference_paper = shared
                    generator.reference_profile = {"stale": True}
                    generator.ocr_engine = object()

                    def analyze(**kwargs):
                        text = kwargs["contents"].text
                        marker = next(name for name in ("default1", "default2", "override", "shared") if name in text)
                        return json.dumps({"subjects": {"Biology": {
                            "style": marker, "chapters": {}, "examples": []
                        }}})

                    generator.communicator.generate = analyze
                    out = root / case
                    PaperBatchRunner(generator).run(spec, out, no_reference_papers=disabled)
                    actual = [
                        profile["subjects"]["Biology"]["style"] if profile else None
                        for profile in (call["reference_profile"] for call in generator.calls)
                    ]
                    self.assertEqual(actual, expected)
                    config = json.loads((out / "sample_exam_batch_generation.json").read_text(encoding="utf-8"))["configuration"]
                    if case in {"defaults", "keyed"}:
                        fingerprints = config["reference_papers"]
                        self.assertEqual(fingerprints["paper1"]["sha256"], hashlib.sha256((refs / f"{expected[0]}.md").read_bytes()).hexdigest())
                        self.assertEqual(fingerprints["paper2"]["sha256"], hashlib.sha256((refs / f"{expected[1]}.md").read_bytes()).hexdigest())
                    elif case == "shared":
                        self.assertEqual(config["reference_paper"]["sha256"], hashlib.sha256(shared.read_bytes()).hexdigest())
                    else:
                        self.assertNotIn("reference_paper", config)
                        self.assertNotIn("reference_papers", config)

            first = refs / "default1.md"
            with self.assertRaisesRegex(ValueError, "settings differ"):
                PaperBatchRunner(FakePaperGenerator([])).run(
                    spec, root / "defaults", resume=True, no_reference_papers=True
                )
            first.write_text("1. changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                PaperBatchRunner(FakePaperGenerator([])).run(spec, root / "defaults", resume=True)

    def test_overridden_or_disabled_defaults_need_not_exist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root)
            content = spec.read_text(encoding="utf-8").replace(
                "    spec: paper.yaml", "    spec: paper.yaml\n    reference_paper: missing.md"
            )
            spec.write_text(content, encoding="utf-8")
            replacement = root / "override.md"
            replacement.write_text("1. valid", encoding="utf-8")
            generator = FakePaperGenerator(["first"])
            generator.reference_papers = {"paper": replacement}
            generator.ocr_engine = object()
            generator.communicator.generate = lambda **kwargs: json.dumps({
                "subjects": {"Biology": {"style": "Style", "chapters": {}, "examples": []}}
            })
            PaperBatchRunner(generator).run(spec, root / "override")
            PaperBatchRunner(FakePaperGenerator(["first"])).run(spec, root / "disabled", no_reference_papers=True)
            with self.assertRaises(FileNotFoundError):
                PaperBatchRunner(FakePaperGenerator([])).run(spec, root / "missing")

    def test_batch_rejects_invalid_template_reference_and_conflicting_disable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root)
            original = spec.read_text(encoding="utf-8")
            for value in ("null", "42", "''", "[a, b]"):
                with self.subTest(value=value):
                    spec.write_text(original.replace(
                        "    spec: paper.yaml", f"    spec: paper.yaml\n    reference_paper: {value}"
                    ), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "non-empty path string"):
                        PaperBatchRunner(FakePaperGenerator([])).run(spec, root / "out")
            spec.write_text(original, encoding="utf-8")
            generator = FakePaperGenerator([])
            generator.reference_papers = {"paper": root / "reference.md"}
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                PaperBatchRunner(generator).run(spec, root / "out", no_reference_papers=True)

    def test_paper_specific_references_are_isolated_and_resume_safely(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root, papers=[("paper1", "paper.yaml"), ("paper2", "paper.yaml")])
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("1. FIRST_REFERENCE", encoding="utf-8")
            second.write_text("1. SECOND_REFERENCE", encoding="utf-8")
            analyses = []

            def setup(generator):
                generator.reference_papers = {"paper1": first, "paper2": second}
                generator.ocr_engine = object()

                def analyze(**kwargs):
                    text = kwargs["contents"].text
                    marker = "FIRST_REFERENCE" if "FIRST_REFERENCE" in text else "SECOND_REFERENCE"
                    expected_id = "paper1" if marker == "FIRST_REFERENCE" else "paper2"
                    self.assertIn(f"paper {expected_id}", kwargs["purpose"])
                    analyses.append(marker)
                    return json.dumps({"subjects": {"Biology": {
                        "style": marker, "chapters": {}, "examples": []
                    }}})

                generator.communicator.generate = analyze
                return generator

            out = root / "out"
            interrupted = setup(FakePaperGenerator(["response one"], fail_after_saving_response=True))
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                PaperBatchRunner(interrupted).run(spec, out)
            self.assertEqual(analyses, ["FIRST_REFERENCE", "SECOND_REFERENCE"])
            profiles1 = list((out / "reference" / "paper1").glob("*/reference_style_profile.json"))
            profiles2 = list((out / "reference" / "paper2").glob("*/reference_style_profile.json"))
            self.assertEqual(len(profiles1), 1)
            self.assertEqual(len(profiles2), 1)
            legacy_profiles = []
            for profile in (profiles1[0], profiles2[0]):
                legacy = out / "reference" / "sample_exam" / profile.parent.parent.name / profile.parent.name
                legacy.parent.mkdir(parents=True, exist_ok=True)
                profile.parent.rename(legacy)
                legacy_profiles.append(legacy / profile.name)
            original = legacy_profiles[1].read_text(encoding="utf-8")
            legacy_profiles[1].write_text(original + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "paper2.*missing or changed"):
                PaperBatchRunner(setup(FakePaperGenerator([]))).run(spec, out, resume=True)
            profiles2[0].write_text(original, encoding="utf-8")
            resumed = setup(FakePaperGenerator(["response two"]))
            PaperBatchRunner(resumed).run(spec, out, resume=True)
            self.assertEqual(len(analyses), 2)
            self.assertTrue(all(path.is_file() for path in legacy_profiles))
            self.assertEqual(
                [call["reference_profile"]["subjects"]["Biology"]["style"] for call in resumed.calls],
                ["FIRST_REFERENCE", "SECOND_REFERENCE"],
            )
            second.write_text("1. CHANGED", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                PaperBatchRunner(setup(FakePaperGenerator([]))).run(spec, out, resume=True)

    def test_unmapped_paper_has_no_profile_and_unknown_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root, papers=[("paper1", "paper.yaml"), ("paper2", "paper.yaml")])
            reference = root / "reference.md"
            reference.write_text("1. Reference", encoding="utf-8")
            generator = FakePaperGenerator(["first", "second"])
            generator.reference_papers = {"paper1": reference}
            generator.ocr_engine = object()
            generator.communicator.generate = lambda **kwargs: json.dumps({
                "subjects": {"Biology": {"style": "Style", "chapters": {}, "examples": []}}
            })
            PaperBatchRunner(generator).run(spec, root / "out")
            self.assertIsNotNone(generator.calls[0]["reference_profile"])
            self.assertIsNone(generator.calls[1]["reference_profile"])
            generator.reference_papers = {"missing": reference}
            with self.assertRaisesRegex(ValueError, "Unknown reference paper ID"):
                PaperBatchRunner(generator).run(spec, root / "other")

    def test_interrupted_paper_analysis_resumes_without_repeating_completed_batches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root, papers=[("paper1", "paper.yaml"), ("paper2", "paper.yaml")])
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("1. FIRST", encoding="utf-8")
            second.write_text("\n".join(f"{n}. SECOND {n}" for n in range(1, 23)), encoding="utf-8")
            spec.write_text(
                "exam_name: Sample Exam\ntest_count: 1\nuniqueness_scope: all_papers\npapers:\n"
                "  - id: paper1\n    spec: paper.yaml\n    reference_paper: first.md\n"
                "  - id: paper2\n    spec: paper.yaml\n    reference_paper: second.md\n",
                encoding="utf-8",
            )
            calls = []

            def setup(generator, fail=False):
                generator.ocr_engine = object()

                def generate(**kwargs):
                    purpose = kwargs["purpose"]
                    calls.append(purpose)
                    if fail and "paper paper2, batch 2/2" in purpose:
                        raise RuntimeError("analysis interrupted")
                    return json.dumps({"subjects": {"Biology": {
                        "style": "Style", "chapters": {}, "examples": []
                    }}})

                generator.communicator.generate = generate
                return generator

            with self.assertRaisesRegex(RuntimeError, "analysis interrupted"):
                PaperBatchRunner(setup(FakePaperGenerator([]), fail=True)).run(spec, root / "out")
            self.assertEqual(len(calls), 3)
            for paper_id in ("paper1", "paper2"):
                new_parent = root / "out" / "reference" / paper_id
                existing = next(new_parent.iterdir())
                old_parent = root / "out" / "reference" / "sample_exam" / paper_id
                old_parent.mkdir(parents=True, exist_ok=True)
                existing.rename(old_parent / existing.name)
            PaperBatchRunner(setup(FakePaperGenerator(["first", "second"]))).run(
                spec, root / "out", resume=True
            )
            self.assertEqual(len(calls), 4)
            self.assertIn("paper paper2, batch 2/2", calls[-1])

    def test_real_generator_receives_only_corresponding_paper_profile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root, papers=[("paper1", "paper.yaml"), ("paper2", "paper.yaml")])
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("1. FIRST_REFERENCE", encoding="utf-8")
            second.write_text("1. SECOND_REFERENCE", encoding="utf-8")
            spec.write_text(
                "exam_name: Sample Exam\ntest_count: 1\nuniqueness_scope: all_papers\npapers:\n"
                "  - id: paper1\n    spec: paper.yaml\n    reference_paper: ignored.md\n"
                "  - id: paper2\n    spec: paper.yaml\n    reference_paper: second.md\n",
                encoding="utf-8",
            )
            prompts = []

            def generate(**kwargs):
                text = kwargs["contents"].text
                if "assessment style" in kwargs["system_instruction"]:
                    marker = "FIRST_STYLE" if "FIRST_REFERENCE" in text else "SECOND_STYLE"
                    return json.dumps({"subjects": {"Biology": {
                        "style": marker, "chapters": {}, "examples": []
                    }}})
                prompts.append(text)
                marker = f"distinct question {len(prompts)}"
                return (
                    '<quiz><question type="multichoice"><name><text>'
                    f'{marker}</text></name><questiontext format="html"><text>{marker}'
                    '</text></questiontext><answer fraction="100"><text>Answer</text>'
                    '</answer></question></quiz>'
                )

            communicator = SimpleNamespace(model_name="test", generate=generate)
            generator = PaperGenerator(
                communicator=communicator, ocr_engine=object(),
                reference_papers={"paper1": first},
            )
            result = PaperBatchRunner(generator).run(spec, root / "out")
            self.assertEqual(len(result), 2)
            self.assertIn("FIRST_STYLE", prompts[0])
            self.assertNotIn("SECOND_STYLE", prompts[0])
            self.assertIn("SECOND_STYLE", prompts[1])
            self.assertNotIn("FIRST_STYLE", prompts[1])

    def test_reference_checkpoint_reuses_profile_and_rejects_changed_reference(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            reference = root / "reference.md"
            reference.write_text("1. Biology reference", encoding="utf-8")
            calls = []

            def analyze(**kwargs):
                calls.append(kwargs)
                return json.dumps({
                    "subjects": {"Biology": {
                        "style": "Conceptual questions.", "chapters": {}, "examples": []
                    }}
                })

            def setup(generator):
                generator.reference_paper = reference
                generator.ocr_engine = object()
                generator.staging_dir = root
                generator.communicator.generate = analyze
                return generator

            generator = setup(FakePaperGenerator(["saved"], fail_after_saving_response=True))
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                PaperBatchRunner(generator).run(spec_path, root / "out")
            self.assertEqual(len(calls), 1)
            manifest_path = root / "out" / "sample_exam_batch_generation.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(original_manifest)
            self.assertEqual(manifest["configuration"]["reference_analysis"]["batch_pages"], 20)
            manifest["configuration"]["reference_analysis"]["batch_pages"] = 30
            manifest["configuration_sha256"] = "previous-custom-batch-size"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                PaperBatchRunner(setup(FakePaperGenerator([]))).run(
                    spec_path, root / "out", resume=True
                )
            manifest_path.write_text(original_manifest, encoding="utf-8")
            profile_path = root / "out" / ".sample_exam_batch_generation" / "reference_style_profile.json"
            saved_profile = profile_path.read_text(encoding="utf-8")
            profile_path.write_text(saved_profile + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference style profile is missing or changed"):
                PaperBatchRunner(setup(FakePaperGenerator([]))).run(
                    spec_path, root / "out", resume=True
                )
            profile_path.write_text(saved_profile, encoding="utf-8")
            resumed = setup(FakePaperGenerator([]))
            PaperBatchRunner(resumed).run(spec_path, root / "out", resume=True)
            self.assertEqual(len(calls), 1)
            reference.write_text("Changed reference", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                PaperBatchRunner(setup(FakePaperGenerator([]))).run(
                    spec_path, root / "out", resume=True
                )

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
            first_generator.communicator.agent_name = "first-agent"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                PaperBatchRunner(first_generator).run(spec_path, output_dir)

            resumed_generator = FakePaperGenerator(["physics response"])
            resumed_generator.communicator.model_name = "different-model"
            resumed_generator.communicator.agent_name = "different-agent"
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

    def test_resume_without_checkpoint_starts_new_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            generator = FakePaperGenerator(["fresh response"])

            results = PaperBatchRunner(generator).run(
                spec_path,
                output_dir,
                resume=True,
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(len(generator.calls), 1)
            manifest = json.loads(
                (output_dir / "sample_exam_batch_generation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["status"], "completed")

    def test_resume_with_larger_test_count_appends_without_replacing_existing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            first_result = PaperBatchRunner(
                FakePaperGenerator(["first test response"])
            ).run(spec_path, output_dir, num_tests=1)
            first_content = first_result[0].read_bytes()

            results = PaperBatchRunner(
                FakePaperGenerator(["second test response"])
            ).run(
                spec_path,
                output_dir,
                num_tests=2,
                resume=True,
            )

            self.assertEqual(
                [path.name for path in results],
                [
                    "sample_exam_test_001_paper.xml",
                    "sample_exam_test_002_paper.xml",
                ],
            )
            self.assertEqual(first_result[0].read_bytes(), first_content)
            self.assertTrue(results[1].is_file())
            manifest = json.loads(
                (output_dir / "sample_exam_batch_generation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["test_count"], 2)
            self.assertEqual(
                [item["status"] for item in manifest["items"]],
                ["completed", "completed"],
            )

    def test_resume_of_completed_batch_logs_existing_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            PaperBatchRunner(FakePaperGenerator(["first response"])).run(
                spec_path,
                output_dir,
            )

            with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
                results = PaperBatchRunner(FakePaperGenerator([])).run(
                    spec_path,
                    output_dir,
                    resume=True,
                )

            log_output = "\n".join(logs.output)
            self.assertEqual(len(results), 1)
            self.assertIn("already completed; verified existing output", log_output)
            self.assertIn("1 total question(s) added", log_output)
            self.assertIn("Batch is already complete", log_output)

    def test_checkpoint_paths_are_portable_and_legacy_absolute_paths_migrate(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec_path = self.make_inputs(root)
            output_dir = root / "output"
            PaperBatchRunner(FakePaperGenerator(["initial response"])).run(
                spec_path,
                output_dir,
            )

            manifest_path = output_dir / "sample_exam_batch_generation.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            configuration = manifest["configuration"]
            serialized = json.dumps(manifest)
            self.assertNotIn(str(root), serialized)
            self.assertTrue(
                all(
                    not Path(path).is_absolute()
                    and not re.match(r"^[A-Za-z]:[\\/]", path)
                    for path in configuration["source_fingerprints"]
                )
            )

            old_configuration = json.loads(json.dumps(configuration))
            old_configuration["papers"][0]["spec_path"] = str(root / "paper.yaml")
            old_fingerprints = {}
            for portable_path, digest in old_configuration[
                "source_fingerprints"
            ].items():
                filename = portable_path.rsplit("/", 1)[-1]
                old_fingerprints[str(root / filename)] = digest
            old_configuration["source_fingerprints"] = old_fingerprints
            manifest["configuration"] = old_configuration
            manifest["configuration_sha256"] = hashlib.sha256(
                json.dumps(
                    old_configuration,
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            resumed_results = PaperBatchRunner(FakePaperGenerator([])).run(
                spec_path,
                output_dir,
                resume=True,
            )
            migrated = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(len(resumed_results), 1)
            self.assertNotIn(str(root), json.dumps(migrated))
            self.assertEqual(
                migrated["configuration"]["papers"][0]["spec_path"],
                "external/paper.yaml",
            )

    def test_old_checkout_absolute_path_rebases_to_repository_relative_path(self):
        relative_path = "src/academic_content_pipeline/resources/catalogs/mcq_types.yaml"
        old_path = f"/home/previous-user/Workspace/academic-content-pipeline/{relative_path}"

        self.assertTrue((REPOSITORY_ROOT / Path(relative_path)).is_file())
        self.assertEqual(relative_path, _portable_path_reference(old_path))

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

    def test_combining_subject_xml_preserves_cdata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            subject_paths = [
                root / "physics.xml",
                root / "chemistry.xml",
            ]
            subject_paths[0].write_text(
                '<quiz><question type="multichoice"><questiontext format="html">'
                '<text><![CDATA[<p>Physics & energy</p>]]></text>'
                "</questiontext></question></quiz>",
                encoding="utf-8",
            )
            subject_paths[1].write_text(
                '<quiz><question type="multichoice"><questiontext format="html">'
                '<text><![CDATA[<p>Chemistry & reactions</p>]]></text>'
                "</questiontext></question></quiz>",
                encoding="utf-8",
            )

            combined_path = _combine_subject_xml(subject_paths, root / "combined.xml")
            combined_xml = combined_path.read_text(encoding="utf-8")
            parsed = ET.parse(combined_path).getroot()

            self.assertEqual(combined_xml.count("<![CDATA["), 2)
            self.assertEqual(
                [
                    question.findtext("./questiontext/text")
                    for question in parsed.findall("question")
                ],
                ["<p>Physics & energy</p>", "<p>Chemistry & reactions</p>"],
            )

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
            checkpoint_text = json.dumps(manifest)
            self.assertNotIn(str(root), checkpoint_text)
            self.assertIn("external/", checkpoint_text)

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
            for call in generator.calls:
                syllabus_path = Path(
                    call["spec_data"]["subjects"][0]["syllabus_file"]
                )
                self.assertFalse(syllabus_path.is_absolute())
                self.assertTrue((call["spec_path"].parent / syllabus_path).is_file())
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

    def test_chapter_generation_groups_quotas_and_tags_questions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            batch_path = root / "batch.yaml"
            paper_path = root / "paper.yaml"
            batch_path.write_text(
                "exam_name: Sample Exam\ntest_count: 1\npapers:\n"
                "  - id: paper\n    spec: paper.yaml\n",
                encoding="utf-8",
            )
            (root / "syllabus.md").write_text(
                "# Biology Syllabus\n\n"
                "## Unit One\nCell structure and function.\n\n"
                "## Unit Two\nGenetics and inheritance.\n",
                encoding="utf-8",
            )
            paper_path.write_text(
                "exam_name: Sample Exam\n"
                "chapter_generation:\n"
                "  enabled: true\n"
                "  max_questions_per_request: 2\n"
                "  chapter_counts:\n"
                "    Unit One: 1\n"
                "    Unit Two: 1\n"
                "subjects:\n"
                "  - name: Biology\n"
                "    total_questions: 2\n"
                "    syllabus_file: syllabus.md\n"
                "sections:\n"
                "  - id: biology\n"
                "    name: Biology\n"
                "    subject: Biology\n"
                "    question_count: 2\n"
                "    question_types:\n"
                "      - type: standard_mcq\n"
                "        count: 2\n"
                "    scoring:\n"
                "      correct: 4\n"
                "      incorrect: -1\n"
                "      unanswered: 0\n",
                encoding="utf-8",
            )
            generator = FakePaperGenerator(["chapter response"])

            with self.assertLogs("academic_content_pipeline", level="INFO") as logs:
                results = PaperBatchRunner(generator).run(batch_path, root / "output")

            self.assertEqual(len(generator.calls), 1)
            log_output = "\n".join(logs.output)
            self.assertIn("requesting 2 question(s)", log_output)
            self.assertIn("received 2/2 question(s)", log_output)
            self.assertIn("Test set 1 completed: 2 total question(s) added", log_output)
            for call in generator.calls:
                syllabus_path = Path(
                    call["spec_data"]["subjects"][0]["syllabus_file"]
                )
                self.assertFalse(syllabus_path.is_absolute())
                self.assertTrue((call["spec_path"].parent / syllabus_path).is_file())
            self.assertIn("[ch001-unit-one] Unit One: 1 question(s)", generator.calls[0]["additional_prompt"])
            self.assertIn("[ch002-unit-two] Unit Two: 1 question(s)", generator.calls[0]["additional_prompt"])
            question_names = [
                " ".join(question.find("name").itertext())
                for question in ET.parse(results[0]).getroot()
                if question.tag == "question"
            ]
            self.assertEqual(
                question_names,
                [
                    "[ch001-unit-one] chapter response 1",
                    "[ch002-unit-two] chapter response 1",
                ],
            )

    def test_ai_chapter_plan_has_subject_and_paper_purpose(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = self.make_inputs(root)
            (root / "paper.yaml").write_text(
                "exam_name: Sample Exam\nchapter_generation:\n  enabled: true\n"
                "subjects:\n  - name: Biology\n    total_questions: 1\n    syllabus_file: syllabus.md\n"
                "sections:\n  - id: biology\n    name: Biology\n    subject: Biology\n"
                "    question_count: 1\n    question_types:\n"
                "      - type: standard_mcq\n        count: 1\n"
                "    scoring:\n      correct: 4\n      incorrect: -1\n",
                encoding="utf-8",
            )
            (root / "syllabus.md").write_text(
                "# Biology\n## Cells\nCell structure.\n", encoding="utf-8"
            )
            generator = FakePaperGenerator(["chapter response"])
            calls = []

            def plan(**kwargs):
                calls.append(kwargs)
                return json.dumps({"chapters": [{"id": "ch001-cells", "questions": 1}]})

            generator.communicator.generate = plan
            PaperBatchRunner(generator).run(spec, root / "output")
            self.assertEqual(len(calls), 1)
            self.assertIn("plan chapter question allocations for Biology", calls[0]["purpose"])
            self.assertIn("paper paper", calls[0]["purpose"])

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
            with self.assertLogs("academic_content_pipeline", level="ERROR") as logs:
                with self.assertRaisesRegex(
                    ValueError,
                    "settings differ from the saved checkpoint",
                ):
                    PaperBatchRunner(FakePaperGenerator([])).run(
                        spec_path,
                        output_dir,
                        resume=True,
                    )
            log_output = "\n".join(logs.output)
            self.assertIn("source_fingerprints", log_output)
            self.assertIn("syllabus.md", log_output)
            self.assertIn("saved=", log_output)
            self.assertIn("requested=", log_output)


if __name__ == "__main__":
    unittest.main()
