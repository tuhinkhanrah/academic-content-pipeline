import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import migrate_chapter_question_categories as migration
from run_path_mirrored_question_generation import has_valid_adaptive_output_manifest


class ChapterCategoryMigrationTests(unittest.TestCase):
    def test_migration_recovers_and_validates_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            out = root / "moodle"
            chapter = source / "neet-ug" / "abcd001.md"
            chapter.parent.mkdir(parents=True)
            chapter.write_text("Chapter", encoding="utf-8")
            spec = root / "spec.yaml"
            spec.write_text("example: true", encoding="utf-8")
            folder = out / "neet-ug" / "abcd001"
            folder.mkdir(parents=True)
            prefix = "abcd001_neet_ug_english"
            name = prefix + "_standard_mcq.xml"
            translated = prefix + "_hindi_standard_mcq.xml"
            xml = folder / name
            payload = '<question type="multichoice"><questiontext><text><![CDATA[<p>A & B</p>]]></text></questiontext></question>'
            xml.write_text(f"<quiz>{payload}</quiz>", encoding="utf-8")
            (folder / translated).write_bytes(xml.read_bytes())
            ocr = folder / "abcd001.xml"
            ocr.write_text("<ocr_result/>", encoding="utf-8")
            checkpoint = folder / (prefix + "_question_types.json")
            checkpoint.write_text(json.dumps({
                "status": "completed", "allocated_questions": 1,
                "question_types": [{"type": "standard_mcq", "count": 1}],
                "files": [name, translated], "completed_files": [name, translated],
                "languages": ["english"], "translate_to": ["hindi"],
                "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
            }), encoding="utf-8")
            top = "chapterwise-question-bank"
            backup = root / "backup"
            self.assertEqual(migration.migrate(source, out, top, None)["xml"], 2)
            original = xml.read_bytes()
            atomic_write = migration._atomic_write

            def interrupt(path, data):
                if path == checkpoint:
                    raise OSError("interrupted")
                atomic_write(path, data)

            with patch.object(migration, "_atomic_write", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    migration.migrate(source, out, top, backup)
            migration.migrate(source, out, top, backup)
            self.assertEqual((backup / "original" / xml.relative_to(out)).read_bytes(), original)
            self.assertIn(payload, xml.read_text(encoding="utf-8"))
            self.assertEqual(ocr.read_text(encoding="utf-8"), "<ocr_result/>")
            self.assertTrue((out / "moodle_combined_moodle.xml").is_file())
            self.assertEqual(migration.migrate(source, out, top, None)["xml"], 0)
            categories = {
                prefix: f"$module$/top/{top}/neet-ug/abcd001/english",
                prefix + "_hindi": f"$module$/top/{top}/neet-ug/abcd001/english_hindi",
            }
            args = (folder, prefix, list(categories), spec, "english", ["hindi"], categories)
            self.assertTrue(has_valid_adaptive_output_manifest(*args))
            original_spec = spec.read_bytes()
            spec.write_text("example: false", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "settings differ"):
                has_valid_adaptive_output_manifest(*args)
            spec.write_bytes(original_spec)
            xml.write_bytes(xml.read_bytes().replace(b"A & B", b"C & D"))
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                has_valid_adaptive_output_manifest(*args)
