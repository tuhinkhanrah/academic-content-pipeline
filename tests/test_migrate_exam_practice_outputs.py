import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from migrate_exam_practice_outputs import migrate_exam_outputs, recategorize_exam_outputs
from academic_content_pipeline.paper_batch import PaperBatchRunner
import test_paper_batch as batch_tests


class MigrateExamPracticeOutputsTests(unittest.TestCase):
    def test_recategorized_batch_resumes_without_regeneration(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec = batch_tests.PaperBatchTests().make_inputs(root)
            year_root = root / "neet-ug" / "2027"
            generator = batch_tests.FakePaperGenerator(["first"])
            generator.renderer = SimpleNamespace(
                category_path="$module$/top/Papers/NEET UG/2027/english"
            )
            original = PaperBatchRunner(generator).run(
                spec, year_root, separate_language_dir=True
            )
            recategorize_exam_outputs(
                year_root,
                exam_name="neet-ug",
                previous_exam_name="NEET UG",
                year="2027",
                top_category="Papers",
                apply=True,
            )
            resumed = batch_tests.FakePaperGenerator([])
            resumed.renderer = SimpleNamespace(
                category_path="$module$/top/Papers/neet-ug/2027/english"
            )
            self.assertEqual(
                PaperBatchRunner(resumed).run(
                    spec, year_root, resume=True, separate_language_dir=True
                ),
                original,
            )
            self.assertEqual(resumed.calls, [])

    def test_recategorizes_existing_year_and_updates_hashes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            year_root = Path(temporary_directory) / "neet-ug" / "2027"
            language_dir = year_root / "english"
            language_dir.mkdir(parents=True)
            old_category = "$module$/top/Full Length Practice Papers/NEET UG/2027/english"
            xml_path = language_dir / "test.xml"
            original = (
                f'<quiz><question type="category"><category><text>{old_category}'
                '</text></category></question><question type="multichoice"/></quiz>'
            ).encode()
            xml_path.write_bytes(original)
            configuration = {"languages": ["english"], "category_path": old_category}
            config_hash = hashlib.sha256(
                json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str)
                .encode("utf-8")
            ).hexdigest()
            manifest_path = language_dir / "test_batch_generation.json"
            manifest_path.write_text(json.dumps({
                "configuration": configuration,
                "configuration_sha256": config_hash,
                "items": [{
                    "status": "completed", "output_file": "test.xml",
                    "output_sha256": hashlib.sha256(original).hexdigest(),
                }],
            }), encoding="utf-8")
            kwargs = dict(
                exam_name="neet-ug", previous_exam_name="NEET UG", year="2027",
                top_category="Full Length Practice Papers",
            )
            self.assertEqual(recategorize_exam_outputs(year_root, **kwargs), (1, 1))
            self.assertEqual(xml_path.read_bytes(), original)
            self.assertEqual(
                recategorize_exam_outputs(year_root, apply=True, **kwargs), (1, 1)
            )
            updated = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = "$module$/top/Full Length Practice Papers/neet-ug/2027/english"
            self.assertEqual(ET.parse(xml_path).getroot()[0].findtext("category/text"), expected)
            self.assertEqual(updated["configuration"]["category_path"], expected)
            self.assertEqual(updated["items"][0]["output_sha256"], hashlib.sha256(xml_path.read_bytes()).hexdigest())
            self.assertEqual(
                updated["configuration_sha256"],
                hashlib.sha256(json.dumps(updated["configuration"], sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest(),
            )
            self.assertEqual(recategorize_exam_outputs(year_root, **kwargs), (1, 1))

    def test_preview_and_migrate_outputs_and_checkpoint_category(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            exam_root = Path(temporary_directory) / "neet-ug"
            language_dir = exam_root / "english"
            language_dir.mkdir(parents=True)
            xml_path = language_dir / "neet_ug_english_test_001_full_paper.xml"
            original_xml = (
                b'<quiz><question type="multichoice"><name><text>Q</text></name>'
                b"</question></quiz>"
            )
            xml_path.write_bytes(original_xml)
            manifest_path = language_dir / "neet_ug_english_batch_generation.json"
            manifest = {
                "schema_version": 2,
                "status": "completed",
                "configuration": {"languages": ["english"]},
                "configuration_sha256": "old-hash",
                "items": [
                    {
                        "status": "completed",
                        "output_file": xml_path.name,
                        "output_sha256": hashlib.sha256(original_xml).hexdigest(),
                    }
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reference = exam_root / "reference"
            reference.mkdir()
            (reference / ".exam_owner").write_text("NEET UG", encoding="utf-8")

            self.assertEqual(
                migrate_exam_outputs(
                    exam_root,
                    year="2027",
                    exam_name="NEET UG",
                    top_category="Full Length Practice Papers",
                ),
                (1, 1),
            )
            self.assertFalse((exam_root / "2027").exists())
            self.assertTrue(xml_path.exists())

            self.assertEqual(
                migrate_exam_outputs(
                    exam_root,
                    year="2027",
                    exam_name="NEET UG",
                    top_category="Full Length Practice Papers",
                    apply=True,
                ),
                (1, 1),
            )
            migrated_root = exam_root / "2027"
            migrated_xml = migrated_root / "english" / xml_path.name
            self.assertTrue(migrated_xml.is_file())
            self.assertTrue((migrated_root / "reference" / ".exam_owner").is_file())
            self.assertFalse((exam_root / "english").exists())
            quiz = ET.parse(migrated_xml).getroot()
            self.assertEqual(
                quiz[0].findtext("category/text"),
                "$module$/top/Full Length Practice Papers/neet-ug/2027/english",
            )

            migrated_manifest_path = (
                migrated_root / "english" / manifest_path.name
            )
            migrated_manifest = json.loads(
                migrated_manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                migrated_manifest["configuration"]["category_path"],
                "$module$/top/Full Length Practice Papers/neet-ug/2027/english",
            )
            self.assertEqual(
                migrated_manifest["items"][0]["output_sha256"],
                hashlib.sha256(migrated_xml.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                migrated_manifest["configuration_sha256"],
                hashlib.sha256(
                    json.dumps(
                        migrated_manifest["configuration"],
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
            )

    def test_hash_mismatch_aborts_without_changing_original_tree(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            exam_root = Path(temporary_directory) / "neet-ug"
            language_dir = exam_root / "english"
            language_dir.mkdir(parents=True)
            xml_path = language_dir / "test.xml"
            xml_path.write_text(
                '<quiz><question type="multichoice"/></quiz>', encoding="utf-8"
            )
            manifest = {
                "configuration": {"languages": ["english"]},
                "items": [
                    {
                        "status": "completed",
                        "output_file": "test.xml",
                        "output_sha256": "incorrect",
                    }
                ],
            }
            (language_dir / "test_batch_generation.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                migrate_exam_outputs(
                    exam_root,
                    year="2027",
                    exam_name="NEET UG",
                    top_category="Full Length Practice Papers",
                    apply=True,
                )
            self.assertTrue(xml_path.is_file())
            self.assertFalse((exam_root / "2027").exists())


if __name__ == "__main__":
    unittest.main()
