import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_moodle_categories as backfill
import test_extraction_checkpoints as extraction_tests
import test_paper_batch as paper_tests
from academic_content_pipeline.paper_batch import PaperBatchRunner


QUESTION = (
    '<quiz><question type="multichoice"><questiontext><text>'
    '<![CDATA[<p>A &amp; B</p>]]></text></questiontext></question></quiz>'
)


class BackfillMoodleCategoriesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "sources"
        self.inputs = root / "inputs"
        self.output = root / "categorized"
        self.source.mkdir()
        self.inputs.mkdir()
        self.common = [
            "--source-root", str(self.source), "--input-root", str(self.inputs),
            "--output-root", str(self.output), "--top-category", "Chapterwise Question Bank",
            "--languages", "english", "--languages", "english,bengali",
        ]

    def run_script(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = backfill.main(self.common + list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_copies_categories_for_extraction_and_separate_question_types(self):
        relative = Path("neet_ug") / "class11" / "biology"
        (self.source / relative).mkdir(parents=True)
        (self.source / relative / "keep401.pdf").write_bytes(b"pdf")
        (self.source / relative / "keep402.pdf").write_bytes(b"pdf")
        (self.inputs / relative / "keep402").mkdir(parents=True)
        english = self.inputs / relative / "keep401_neet_ug_english.xml"
        bilingual = self.inputs / relative / "keep401_neet_ug_english_bengali.xml"
        typed = self.inputs / relative / "keep402" / "keep402_neet_ug_english_bengali_standard_mcq.xml"
        for path in (english, bilingual, typed):
            path.write_text(QUESTION, encoding="utf-8")
        (self.inputs / relative / "keep401.xml").write_text("<ocr_result/>", encoding="utf-8")
        combined = self.inputs / relative / "class11_biology_combined_moodle.xml"
        combined.write_text(QUESTION, encoding="utf-8")
        (self.inputs / relative / "keep401_neet_ug_english.extract.json").write_text("checkpoint", encoding="utf-8")

        result, output, errors = self.run_script("--dry-run")
        self.assertEqual(result, 0, errors)
        self.assertIn("Previewed 3", output)
        self.assertFalse(self.output.exists())
        result, _, errors = self.run_script()
        self.assertEqual(result, 0, errors)
        for path, language, chapter in (
            (english, "english", "keep401"),
            (bilingual, "english_bengali", "keep401"),
            (typed, "english_bengali", "keep402"),
        ):
            quiz = ET.parse(self.output / path.relative_to(self.inputs)).getroot()
            self.assertEqual(
                quiz[0].findtext("category/text"),
                f"$module$/top/Chapterwise Question Bank/neet_ug/class11/biology/{chapter}/{language}",
            )
            self.assertEqual(quiz[1].findtext("questiontext/text"), "<p>A &amp; B</p>")
            self.assertEqual(path.read_text(encoding="utf-8"), QUESTION)
        self.assertFalse((self.output / relative / combined.name).exists())
        self.assertFalse((self.output / relative / "keep401_neet_ug_english.extract.json").exists())

    def test_batch_spec_and_split_outputs(self):
        spec = self.source / "jee_advanced" / "paper1.template.yaml"
        spec.parent.mkdir()
        spec.write_text("exam_name: JEE\n", encoding="utf-8")
        folder = self.inputs / "english_bengali"
        folder.mkdir()
        xml = folder / "jee_advanced_english_bengali_test_001_paper1.xml"
        xml.write_text(QUESTION, encoding="utf-8")
        result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 0, errors)
        quiz = ET.parse(self.output / "english_bengali" / xml.name).getroot()
        self.assertEqual(
            quiz[0].findtext("category/text"),
            "$module$/top/Chapterwise Question Bank/jee_advanced/paper1.template/english_bengali",
        )

    def test_ambiguous_source_and_unknown_language_write_nothing(self):
        (self.source / "chapter.pdf").write_bytes(b"pdf")
        (self.source / "chapter.md").write_text("chapter", encoding="utf-8")
        (self.inputs / "chapter_english.xml").write_text(QUESTION, encoding="utf-8")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("Ambiguous source", errors)
        self.assertFalse(self.output.exists())
        (self.source / "chapter.md").unlink()
        (self.inputs / "chapter_english.xml").rename(self.inputs / "chapter_english_hindi.xml")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("language set cannot be determined", errors)
        self.assertFalse(self.output.exists())

    def test_source_names_are_normalized_and_legacy_requires_explicit_language(self):
        (self.source / "My Chapter.pdf").write_bytes(b"pdf")
        xml = self.inputs / "My_Chapter_english_paper_01.xml"
        xml.write_text(QUESTION, encoding="utf-8")
        legacy = self.inputs / "My Chapter.xml"
        legacy.write_text(QUESTION, encoding="utf-8")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("language set cannot be determined", errors)
        self.assertFalse(self.output.exists())
        result, _, errors = self.run_script("--legacy-languages", "english")
        self.assertEqual(result, 0, errors)
        quiz = ET.parse(self.output / xml.name).getroot()
        self.assertEqual(
            quiz[0].findtext("category/text"),
            "$module$/top/Chapterwise Question Bank/My Chapter/english",
        )

    def test_rejects_bad_xml_and_output_root_overlap(self):
        (self.source / "chapter.pdf").write_bytes(b"pdf")
        (self.inputs / "chapter_english.xml").write_text("<quiz>", encoding="utf-8")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("no element found", errors)
        self.assertFalse(self.output.exists())
        self.output = self.inputs / "output"
        self.common[self.common.index("--output-root") + 1] = str(self.output)
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("outside", errors)

    def test_replaces_existing_category_but_skips_multi_category_xml(self):
        (self.source / "chapter.pdf").write_bytes(b"pdf")
        xml = self.inputs / "chapter_english.xml"
        marker = '<question type="category"><category><text>old</text></category></question>'
        xml.write_text(QUESTION.replace("<quiz>", "<quiz>" + marker), encoding="utf-8")
        other = self.inputs / "chapter_english_paper_02.xml"
        other.write_text(QUESTION.replace("<quiz>", "<quiz>" + marker + marker), encoding="utf-8")
        result, output, errors = self.run_script()
        self.assertEqual(result, 0, errors)
        self.assertIn("multiple category markers", output)
        quiz = ET.parse(self.output / xml.name).getroot()
        self.assertEqual(quiz[0].findtext("category/text"), "$module$/top/Chapterwise Question Bank/chapter/english")
        self.assertEqual(sum(q.get("type") == "category" for q in quiz), 1)
        self.assertFalse((self.output / other.name).exists())

    def test_subtree_uses_full_source_relative_path(self):
        source_folder = self.source / "neet_ug" / "biology"
        input_folder = self.inputs / "neet_ug" / "biology"
        source_folder.mkdir(parents=True)
        input_folder.mkdir(parents=True)
        (source_folder / "keep401.pdf").write_bytes(b"pdf")
        (input_folder / "keep401_english.xml").write_text(QUESTION, encoding="utf-8")
        (self.inputs / "unmatched_english.xml").write_text(QUESTION, encoding="utf-8")
        result, _, errors = self.run_script("--subtree", str(Path("neet_ug") / "biology"))
        self.assertEqual(result, 0, errors)
        quiz = ET.parse(self.output / "neet_ug" / "biology" / "keep401_english.xml").getroot()
        self.assertEqual(
            quiz[0].findtext("category/text"),
            "$module$/top/Chapterwise Question Bank/neet_ug/biology/keep401/english",
        )

    def in_place(self):
        self.common[self.common.index("--output-root") + 1] = str(self.inputs)
        backup = self.inputs.parent / "backup"
        self.common.extend(["--backup-root", str(backup)])
        return backup

    def make_extraction(self):
        source = self.source / "chapter.pdf"
        source.write_bytes(b"test pdf")
        helpers = extraction_tests.ExtractionCheckpointTests()
        extractor = helpers.make_extractor(extraction_tests.FakeCommunicator())
        output = helpers.run_file(extractor, source, self.inputs)
        return source, output, output.with_suffix(".extract.json"), helpers

    def test_in_place_extraction_checkpoint_and_resume(self):
        source, output, checkpoint, helpers = self.make_extraction()
        old_output, old_checkpoint = output.read_bytes(), checkpoint.read_bytes()
        backup = self.in_place()
        result, _, errors = self.run_script("--dry-run")
        self.assertEqual(result, 0, errors)
        self.assertFalse(backup.exists())
        self.assertEqual(output.read_bytes(), old_output)
        result, _, errors = self.run_script()
        self.assertEqual(result, 0, errors)
        self.assertEqual((backup / output.name).read_bytes(), old_output)
        self.assertEqual((backup / checkpoint.name).read_bytes(), old_checkpoint)
        category = "$module$/top/Chapterwise Question Bank/chapter/english"
        manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(manifest["configuration"]["category_path"], category)
        self.assertEqual(manifest["final_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(ET.parse(output).getroot()[0].findtext("category/text"), category)
        resumed = helpers.make_extractor(extraction_tests.FakeCommunicator(), resume=True)
        resumed.top_category = "Chapterwise Question Bank"
        resumed.source_root = self.source
        self.assertEqual(helpers.run_file(resumed, source, self.inputs), output)
        self.assertFalse(resumed.communicator.calls)

    def test_in_place_without_checkpoint_is_idempotent(self):
        (self.source / "chapter.pdf").write_bytes(b"pdf")
        output = self.inputs / "chapter_english.xml"
        output.write_text(QUESTION, encoding="utf-8")
        backup = self.in_place()
        result, _, errors = self.run_script()
        self.assertEqual(result, 0, errors)
        self.assertEqual((backup / output.name).read_text(encoding="utf-8"), QUESTION)
        updated = output.read_bytes()
        self.common[self.common.index("--backup-root") + 1] = str(self.inputs.parent / "backup2")
        result, _, errors = self.run_script()
        self.assertEqual(result, 0, errors)
        self.assertEqual(output.read_bytes(), updated)

    def test_in_place_batch_updates_all_items_and_resumes(self):
        spec = paper_tests.PaperBatchTests().make_inputs(self.source, test_count=2)
        generator = paper_tests.FakePaperGenerator(["first", "second"])
        runner = PaperBatchRunner(generator)
        outputs = runner.run(spec, self.inputs)
        checkpoint = self.inputs / "sample_exam_english_batch_generation.json"
        old = {path.name: path.read_bytes() for path in [*outputs, checkpoint]}
        backup = self.in_place()
        result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 0, errors)
        for name, data in old.items():
            self.assertEqual((backup / name).read_bytes(), data)
        manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
        category = "$module$/top/Chapterwise Question Bank/batch/english"
        self.assertEqual(manifest["configuration"]["category_path"], category)
        for item, path in zip(manifest["items"], outputs):
            self.assertEqual(item["output_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(ET.parse(path).getroot()[0].findtext("category/text"), category)
        generator.renderer = SimpleNamespace(category_path=category)
        self.assertEqual(runner.run(spec, self.inputs, resume=True), outputs)

    def test_in_place_rejects_changed_or_incomplete_extraction_without_writes(self):
        source, output, checkpoint, _ = self.make_extraction()
        backup = self.in_place()
        original = output.read_bytes()
        output.write_bytes(original + b"\n")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("hash mismatch", errors)
        self.assertFalse(backup.exists())
        output.write_bytes(original)
        manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
        manifest["status"] = "in_progress"
        checkpoint.write_text(json.dumps(manifest), encoding="utf-8")
        result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("incomplete", errors)
        self.assertEqual(output.read_bytes(), original)
        self.assertFalse(backup.exists())
        self.assertTrue(source.exists())

    def test_interrupted_extraction_blocks_resume_and_backups_restore(self):
        source, output, checkpoint, helpers = self.make_extraction()
        old_output, old_checkpoint = output.read_bytes(), checkpoint.read_bytes()
        backup = self.in_place()
        real_atomic_write = backfill.atomic_write

        def fail_output(path, data):
            if path == output:
                raise OSError("simulated interruption")
            real_atomic_write(path, data)

        with patch.object(backfill, "atomic_write", side_effect=fail_output):
            result, _, errors = self.run_script()
        self.assertEqual(result, 1)
        self.assertIn("simulated interruption", errors)
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["status"], "category_backfill_pending")
        resumed = helpers.make_extractor(extraction_tests.FakeCommunicator(), resume=True)
        resumed.top_category = "Chapterwise Question Bank"
        resumed.source_root = self.source
        with self.assertRaisesRegex(ValueError, "category backfill is incomplete"):
            helpers.run_file(resumed, source, self.inputs)
        replacement = helpers.make_extractor(extraction_tests.FakeCommunicator(), replace_pages=[1, 1])
        replacement.top_category = "Chapterwise Question Bank"
        replacement.source_root = self.source
        with self.assertRaisesRegex(ValueError, "category backfill is incomplete"):
            helpers.run_file(replacement, source, self.inputs)
        output.write_bytes((backup / output.name).read_bytes())
        checkpoint.write_bytes((backup / checkpoint.name).read_bytes())
        self.assertEqual(output.read_bytes(), old_output)
        self.assertEqual(checkpoint.read_bytes(), old_checkpoint)
        restored = helpers.make_extractor(extraction_tests.FakeCommunicator(), resume=True)
        self.assertEqual(helpers.run_file(restored, source, self.inputs), output)

    def test_batch_preflight_rejects_missing_selection_and_wrong_spec(self):
        spec = paper_tests.PaperBatchTests().make_inputs(self.source, test_count=2)
        outputs = PaperBatchRunner(paper_tests.FakePaperGenerator(["first", "second"])).run(spec, self.inputs)
        old = [path.read_bytes() for path in outputs]
        backup = self.in_place()
        other_spec = self.source / "other.yaml"
        other_spec.write_text("test_count: 2\npapers: []\n", encoding="utf-8")
        result, _, errors = self.run_script("--spec", str(other_spec))
        self.assertEqual(result, 1)
        self.assertIn("does not match", errors)
        outputs[1].rename(outputs[1].with_suffix(".hidden"))
        result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 1)
        self.assertIn("All outputs", errors)
        self.assertFalse(backup.exists())
        self.assertEqual(outputs[0].read_bytes(), old[0])

    def test_interrupted_batch_blocks_resume_and_backups_restore(self):
        spec = paper_tests.PaperBatchTests().make_inputs(self.source, test_count=2)
        generator = paper_tests.FakePaperGenerator(["first", "second"])
        runner = PaperBatchRunner(generator)
        outputs = runner.run(spec, self.inputs)
        checkpoint = self.inputs / "sample_exam_english_batch_generation.json"
        original = {path: path.read_bytes() for path in [*outputs, checkpoint]}
        backup = self.in_place()
        real_atomic_write = backfill.atomic_write

        def fail_second_output(path, data):
            if path == outputs[1]:
                raise OSError("simulated interruption")
            real_atomic_write(path, data)

        with patch.object(backfill, "atomic_write", side_effect=fail_second_output):
            result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 1)
        self.assertIn("simulated interruption", errors)
        self.assertNotEqual(outputs[0].read_bytes(), original[outputs[0]])
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8"))["status"], "category_backfill_pending")
        with self.assertRaisesRegex(ValueError, "category backfill is incomplete"):
            runner.run(spec, self.inputs, resume=True)
        for path in outputs:
            path.write_bytes((backup / path.name).read_bytes())
        checkpoint.write_bytes((backup / checkpoint.name).read_bytes())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(runner.run(spec, self.inputs, resume=True), outputs)

    def test_batch_rejects_changed_output_and_configuration_hash(self):
        spec = paper_tests.PaperBatchTests().make_inputs(self.source)
        outputs = PaperBatchRunner(paper_tests.FakePaperGenerator(["first"])).run(spec, self.inputs)
        checkpoint = self.inputs / "sample_exam_english_batch_generation.json"
        backup = self.in_place()
        original = outputs[0].read_bytes()
        outputs[0].write_bytes(original + b"\n")
        result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 1)
        self.assertIn("hash mismatch", errors)
        outputs[0].write_bytes(original)
        manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
        manifest["configuration_sha256"] = "invalid"
        checkpoint.write_text(json.dumps(manifest), encoding="utf-8")
        result, _, errors = self.run_script("--spec", str(spec))
        self.assertEqual(result, 1)
        self.assertIn("configuration hash mismatch", errors)
        self.assertFalse(backup.exists())
        self.assertEqual(outputs[0].read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
