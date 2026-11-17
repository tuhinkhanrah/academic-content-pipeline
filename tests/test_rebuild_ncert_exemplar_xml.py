import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rebuild_ncert_exemplar_xml as rebuild


def question(name, text="Question"):
    return (
        f'<question type="multichoice"><name><text>{name}</text></name>'
        f'<questiontext><text>{text}</text></questiontext>'
        '<generalfeedback><text>Solution</text></generalfeedback><single>true</single>'
        '<answer fraction="100"><text>A</text></answer>'
        '<answer fraction="0"><text>B</text></answer></question>'
    )


class RebuildExemplarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "staging"
        self.folder = self.source / "class11" / "biology"
        self.folder.mkdir(parents=True)
        self.manifest_path = self.folder / "keep401_neet_ug_english.extract.json"
        self.batch_dir = self.folder / ".keep401_neet_ug_english.extract_batches"
        self.batch_dir.mkdir()
        self.manifest = {
            "schema_version": 1,
            "status": "completed",
            "source_pdf": "keep401.pdf",
            "source_sha256": "a" * 64,
            "configuration": {"page_range": None},
            "output_file": "keep401_neet_ug_english.xml",
            "batch_ids": ["pages_1_5", "pages_6_10"],
            "batches": [
                {"id": "pages_1_5", "status": "completed", "response_file": "one.txt", "question_count": 1},
                {"id": "pages_6_10", "status": "completed", "response_file": "two.txt", "question_count": 0},
            ],
        }
        self.save_manifest()
        (self.batch_dir / "one.txt").write_text(f"<quiz>{question('original')}</quiz>", encoding="utf-8")
        (self.batch_dir / "two.txt").write_text("<quiz></quiz>", encoding="utf-8")
        (self.batch_dir / "pages_1_5.xml").write_text("<quiz>corrupted batch</quiz>", encoding="utf-8")
        (self.folder / self.manifest["output_file"]).write_text("modified original", encoding="utf-8")

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def run_rebuild(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = rebuild.main(["--source-root", str(self.source), "--output-root", str(self.output)])
        return result, stdout.getvalue(), stderr.getvalue()

    def write_cache(self, images):
        ocr = self.folder / "keep401" / "ocr"
        cache = ocr / "cache"
        cache.mkdir(parents=True)
        key = self.manifest["source_sha256"] + ":full:ocr-v1"
        cache_file = cache / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        cache_file.write_text(
            json.dumps({"cache_key": key, "pdf_hash": self.manifest["source_sha256"], "images": images}),
            encoding="utf-8",
        )
        return ocr

    def test_rebuild_uses_responses_not_modified_xml_or_batch(self):
        result, _, errors = self.run_rebuild()
        self.assertEqual(result, 0, errors)
        result_xml = self.output / "class11" / "biology" / self.manifest["output_file"]
        self.assertEqual(ET.parse(result_xml).getroot().findtext("question/name/text"), "original")
        self.assertEqual((self.folder / self.manifest["output_file"]).read_text(), "modified original")
        self.assertFalse(list(self.output.rglob("*.extract.json")))

    def test_rebuild_preserves_checkpoint_category(self):
        category = "$module$/top/Bank/class11/biology/keep401/english"
        self.manifest["configuration"]["category_path"] = category
        self.save_manifest()
        result, _, errors = self.run_rebuild()
        self.assertEqual(result, 0, errors)
        quiz = ET.parse(self.output / "class11" / "biology" / self.manifest["output_file"]).getroot()
        self.assertEqual(quiz[0].findtext("category/text"), category)
        self.assertEqual(quiz[1].findtext("name/text"), "original")

    def test_invalid_missing_incomplete_and_mismatched_responses_are_reported(self):
        for case in ("missing", "incomplete", "mismatch", "unfinished"):
            with self.subTest(case=case):
                if case == "missing":
                    (self.batch_dir / "one.txt").unlink()
                elif case == "incomplete":
                    (self.batch_dir / "one.txt").write_text("<quiz><question", encoding="utf-8")
                elif case == "mismatch":
                    (self.batch_dir / "one.txt").write_text("<quiz></quiz>", encoding="utf-8")
                else:
                    self.manifest["status"] = "in_progress"
                    self.save_manifest()
                with self.assertRaises((OSError, ValueError)) as caught:
                    rebuild.rebuild_manifest(self.manifest_path)
                self.assertTrue(str(caught.exception))
                (self.batch_dir / "one.txt").write_text(f"<quiz>{question('original')}</quiz>", encoding="utf-8")
                self.manifest["status"] = "completed"
                self.save_manifest()

    def test_rebuild_embeds_exact_cached_image_and_fails_when_missing(self):
        text = '<![CDATA[<img src="@@PLUGINFILE@@/img-0.jpeg"/>]]>'
        (self.batch_dir / "one.txt").write_text(f"<quiz>{question('picture', text)}</quiz>", encoding="utf-8")
        ocr = self.write_cache({"img-0.jpeg": "img-0.jpeg"})
        with self.assertRaisesRegex(ValueError, "Cached image is missing"):
            rebuild.rebuild_manifest(self.manifest_path)
        (ocr / "img-0.jpeg").write_bytes(b"image bytes")
        result, _, errors = self.run_rebuild()
        self.assertEqual(result, 0, errors)
        xml = ET.parse(self.output / "class11" / "biology" / self.manifest["output_file"])
        self.assertEqual(xml.find("question/questiontext/file").get("name"), "img-0.jpeg")

    def test_refuses_ambiguous_cached_image_ids(self):
        text = '<![CDATA[<img src="@@PLUGINFILE@@/a.jpg"/><img src="@@PLUGINFILE@@/a.jpeg"/>]]>'
        (self.batch_dir / "one.txt").write_text(f"<quiz>{question('pictures', text)}</quiz>", encoding="utf-8")
        ocr = self.write_cache({"a.jpg": "a.jpg", "a.jpeg": "a.jpeg"})
        (ocr / "a.jpg").write_bytes(b"first")
        (ocr / "a.jpeg").write_bytes(b"second")
        with self.assertRaisesRegex(ValueError, "Ambiguous cached image"):
            rebuild.rebuild_manifest(self.manifest_path)

    def test_reports_failed_rebuild_and_preserves_source(self):
        (self.batch_dir / "one.txt").write_text("<quiz></quiz>", encoding="utf-8")
        result, _, errors = self.run_rebuild()
        self.assertEqual(result, 1)
        self.assertIn("checkpoint expects 1", errors)
        self.assertFalse((self.output / "class11" / "biology" / self.manifest["output_file"]).exists())
        self.assertEqual((self.folder / self.manifest["output_file"]).read_text(), "modified original")

    def test_rejects_source_or_existing_output_as_destination(self):
        for output in (self.source, self.source / "nested", self.root):
            with self.subTest(output=output), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    rebuild.main(["--source-root", str(self.source), "--output-root", str(output)])


if __name__ == "__main__":
    unittest.main()
