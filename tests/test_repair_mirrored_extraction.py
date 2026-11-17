import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rebuild_ncert_exemplar_xml as rebuild
import run_path_mirrored_extraction as mirrored
import test_extraction_checkpoints as extraction_tests


class ExemplarCommunicator(extraction_tests.FakeCommunicator):
    def generate(self, **kwargs):
        self.calls.append(kwargs["contents"].page_range)
        page = kwargs["contents"].page_range[0]
        return f"<quiz>{extraction_tests.exemplar_question(f'Question {page}')}</quiz>"


class RepairMirroredExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "neet_ug" / "ncert_exemplar" / "class11"
        self.pdf = self.source / "biology" / "keep401.pdf"
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b"source PDF")
        self.output = root / "bank" / "class11"
        self.backup = root / "repair-backup"
        self.output_dir = self.output / "biology"
        self.harness = extraction_tests.ExtractionCheckpointTests()
        communicator = ExemplarCommunicator()
        extractor = self.harness.make_extractor(communicator)
        extractor.standards = "neet_ug"
        self.xml = self.harness.run_file(extractor, self.pdf, self.output_dir)
        self.manifest_path = self.output_dir / "keep401_neet_ug_english.extract.json"
        self.batch_dir = self.output_dir / ".keep401_neet_ug_english.extract_batches"
        self.batch_path = self.batch_dir / "pages_1_1.xml"
        self.responses = {path.name: path.read_bytes() for path in self.batch_dir.glob("*.response.txt")}
        self.xml.write_text("<quiz>changed</quiz>", encoding="utf-8")
        self.batch_path.write_text("<quiz>changed batch</quiz>", encoding="utf-8")

    def command(self, *extra):
        return [
            "--source-root", str(self.source),
            "--output-root", str(self.output),
            "--repair",
            "--repair-backup-root", str(self.backup),
            *extra,
        ]

    def run_repair(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            patch.object(mirrored.subprocess, "run", side_effect=AssertionError("AI command invoked")),
        ):
            result = mirrored.main(self.command(*extra))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_existing_output_lookup_keeps_language_normalization(self):
        legacy = self.output_dir / "legacy.xml"
        legacy.write_text("<quiz/>", encoding="utf-8")
        self.assertEqual(
            mirrored.find_existing_output(self.output_dir, "legacy", "general", " English,english "),
            legacy,
        )
        self.assertEqual(
            mirrored.generated_output_path(self.output_dir, "keep401", "NEET_UG", "English,Bengali,english").name,
            "keep401_neet_ug_english_bengali.xml",
        )

    def test_repairs_xml_batches_and_hash_and_resumes_without_ai(self):
        result, _, errors = self.run_repair()
        self.assertEqual(result, 0, errors)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["final_sha256"], hashlib.sha256(self.xml.read_bytes()).hexdigest())
        self.assertEqual(len(ET.parse(self.xml).getroot().findall("question")), 3)
        self.assertEqual(ET.parse(self.batch_path).getroot().findtext("question/name/text"), "Question 1")
        self.assertEqual((self.backup / "biology" / self.xml.name).read_text(), "<quiz>changed</quiz>")
        self.assertEqual(
            (self.backup / "biology" / self.batch_dir.name / self.batch_path.name).read_text(),
            "<quiz>changed batch</quiz>",
        )
        self.assertEqual(
            self.responses,
            {path.name: path.read_bytes() for path in self.batch_dir.glob("*.response.txt")},
        )

        communicator = ExemplarCommunicator()
        extractor = self.harness.make_extractor(communicator, resume=True)
        extractor.standards = "neet_ug"
        with patch.object(communicator, "generate", side_effect=AssertionError("AI command invoked")):
            self.assertEqual(self.harness.run_file(extractor, self.pdf, self.output_dir), self.xml)
            self.assertEqual(communicator.calls, [])
            manifest["status"] = "in_progress"
            self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.harness.run_file(extractor, self.pdf, self.output_dir)
        self.assertEqual(communicator.calls, [])
        self.assertEqual(len(ET.parse(self.xml).getroot().findall("question")), 3)

    def test_repair_keeps_checkpoint_category(self):
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        category = "$module$/top/Bank/biology/keep401/english"
        manifest["configuration"]["category_path"] = category
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result, _, errors = self.run_repair()
        self.assertEqual(result, 0, errors)
        quiz = ET.parse(self.xml).getroot()
        self.assertEqual(quiz[0].findtext("category/text"), category)
        self.assertEqual(len(quiz.findall("question")), 4)

    def test_dry_run_does_not_change_files_or_create_backup(self):
        self.pdf.with_name("keep402.pdf").write_bytes(b"unextracted PDF")
        before = self.manifest_path.read_bytes(), self.xml.read_bytes(), self.batch_path.read_bytes()
        result, output, errors = self.run_repair("--dry-run")
        self.assertEqual(result, 0, errors)
        self.assertIn("Would repair", output)
        self.assertIn("keep402.pdf", output)
        self.assertIn("without output skipped", output)
        self.assertEqual(before, (self.manifest_path.read_bytes(), self.xml.read_bytes(), self.batch_path.read_bytes()))
        self.assertFalse(self.backup.exists())

    def test_subtree_repair_keeps_original_mirrored_root(self):
        other = self.source / "physics" / "other.pdf"
        other.parent.mkdir()
        other.write_bytes(b"other PDF")
        args = [
            "--source-root", str(self.source.parent),
            "--output-root", str(self.output.parent),
            "--subtree", str(Path("class11") / "biology"),
            "--repair", "--repair-backup-root", str(self.backup),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = mirrored.main(args)
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertNotIn("other.pdf", stdout.getvalue())
        self.assertEqual(
            (self.backup / "class11" / "biology" / self.xml.name).read_text(),
            "<quiz>changed</quiz>",
        )
        self.assertEqual(len(ET.parse(self.xml).getroot().findall("question")), 3)

    def test_repair_recreates_missing_output_and_records_original_absence(self):
        self.xml.unlink()
        result, _, errors = self.run_repair()
        self.assertEqual(result, 0, errors)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["final_sha256"], hashlib.sha256(self.xml.read_bytes()).hexdigest())
        self.assertFalse((self.backup / "biology" / self.xml.name).exists())
        info = self.backup / "biology" / (self.manifest_path.name + ".repair-info.json")
        self.assertFalse(json.loads(info.read_text(encoding="utf-8"))["output_existed"])

    def test_preflight_failure_leaves_all_files_untouched(self):
        second_pdf = self.pdf.with_name("keep402.pdf")
        second_pdf.write_bytes(b"second source PDF")
        second_extractor = self.harness.make_extractor(ExemplarCommunicator())
        second_extractor.standards = "neet_ug"
        second_xml = self.harness.run_file(second_extractor, second_pdf, self.output_dir)
        second_xml.write_text("<quiz>also changed</quiz>", encoding="utf-8")
        (self.batch_dir / "pages_1_1.response.txt").write_text("<quiz></quiz>", encoding="utf-8")
        before = (
            self.manifest_path.read_bytes(), self.xml.read_bytes(),
            self.batch_path.read_bytes(), second_xml.read_bytes(),
        )
        result, _, errors = self.run_repair()
        self.assertEqual(result, 1)
        self.assertIn("checkpoint expects", errors)
        self.assertEqual(before, (
            self.manifest_path.read_bytes(), self.xml.read_bytes(),
            self.batch_path.read_bytes(), second_xml.read_bytes(),
        ))
        self.assertFalse(self.backup.exists())

    def test_changed_source_pdf_is_rejected(self):
        self.pdf.write_bytes(b"different PDF")
        result, _, errors = self.run_repair()
        self.assertEqual(result, 1)
        self.assertIn("Source PDF changed", errors)
        self.assertEqual(self.xml.read_text(), "<quiz>changed</quiz>")
        self.assertFalse(self.backup.exists())

    def test_interrupted_repair_blocks_resume_and_preserves_backup(self):
        original_write = rebuild.write_text_atomically

        def fail_final(path, text):
            if Path(path) == self.xml:
                raise OSError("simulated write failure")
            return original_write(path, text)

        with patch.object(rebuild, "write_text_atomically", side_effect=fail_final):
            result, _, errors = self.run_repair()
        self.assertEqual(result, 1)
        self.assertIn("repair_pending", errors)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "repair_pending")
        self.assertEqual(manifest["repair_backup_root"], str(self.backup))
        self.assertTrue((self.backup / "biology" / self.manifest_path.name).is_file())
        extractor = self.harness.make_extractor(extraction_tests.FakeCommunicator(fail_on_call=1), resume=True)
        extractor.standards = "neet_ug"
        with self.assertRaisesRegex(ValueError, "unfinished repair transaction"):
            self.harness.run_file(extractor, self.pdf, self.output_dir)

        shutil.copy2(self.backup / "biology" / self.xml.name, self.xml)
        backup_batches = self.backup / "biology" / self.batch_dir.name
        for batch in backup_batches.glob("*.xml"):
            shutil.copy2(batch, self.batch_dir / batch.name)
        shutil.copy2(self.backup / "biology" / self.manifest_path.name, self.manifest_path)
        self.backup = self.backup.with_name("repair-backup-retry")
        result, _, errors = self.run_repair()
        self.assertEqual(result, 0, errors)
        restored = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["final_sha256"], hashlib.sha256(self.xml.read_bytes()).hexdigest())

    def test_rejects_conflicting_options_and_backup_locations(self):
        for extra in (("--resume",), ("--force",), ("--replace-pages", "1", "1")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    mirrored.main(self.command(*extra))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mirrored.main([
                    "--source-root", str(self.source), "--output-root", str(self.output),
                    "--repair", "--repair-backup-root", str(self.output / "backup"),
                ])
        self.backup.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mirrored.main(self.command())


if __name__ == "__main__":
    unittest.main()
