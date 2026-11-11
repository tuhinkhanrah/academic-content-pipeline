import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from academic_content_pipeline.reference_paper import (
    prepare_reference,
    prepare_reference_storage,
    parse_reference_arguments,
    reference_fingerprint,
    reference_guidance,
    reference_storage_dir,
)
from academic_content_pipeline.content_processors import PaperGenerator
from academic_content_pipeline.mistral_ocr import MistralOCREngine


class ReferenceCommunicator:
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({
            "subjects": {
                "Physics": {
                    "style": "Use multi-step calculations and circuits.",
                    "chapters": {"Motion": "Use velocity graphs."},
                    "examples": [{"id": "r001", "chapter": "Motion"}],
                }
            }
        })


class ReferencePaperTests(unittest.TestCase):
    def test_parse_reference_arguments(self):
        self.assertEqual(parse_reference_arguments(["source.md"]), (Path("source.md"), {}))
        self.assertEqual(
            parse_reference_arguments(["paper1=a.pdf", "paper2=b.md"]),
            (None, {"paper1": Path("a.pdf"), "paper2": Path("b.md")}),
        )
        for values in (["paper1=a.pdf", "paper1=b.pdf"], ["a.pdf", "paper1=b.pdf"], ["paper1="]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_reference_arguments(values)

    def test_reference_storage_is_isolated_by_paper_and_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf1 = root / "one" / "same.pdf"
            pdf2 = root / "two" / "same.pdf"
            pdf1.parent.mkdir()
            pdf2.parent.mkdir()
            pdf1.write_bytes(b"first")
            pdf2.write_bytes(b"second")
            fingerprint1 = reference_fingerprint(pdf1)
            fingerprint2 = reference_fingerprint(pdf2)
            first = reference_storage_dir(root / "exam1", "paper1", fingerprint1)
            self.assertNotEqual(first, reference_storage_dir(root / "exam1", "paper1", fingerprint2))
            self.assertNotEqual(first, reference_storage_dir(root / "exam1", "paper2", fingerprint1))
            self.assertNotEqual(first, reference_storage_dir(root / "exam2", "paper1", fingerprint1))
            self.assertEqual(first.parent, root / "exam1" / "reference" / "paper1")
            seen = []
            ocr = SimpleNamespace(process_pdf=lambda path, directory: (
                seen.append(directory) or SimpleNamespace(
                    pages=[SimpleNamespace(markdown="1. Physics question", images={})]
                )
            ))
            prepare_reference(pdf1, ["Physics"], ReferenceCommunicator(), ocr, first, first / "profile.json")
            self.assertEqual(seen, [first])

    def test_pdf_ocr_cache_and_images_are_isolated_between_papers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"fixture")
            fingerprint = reference_fingerprint(pdf)
            first = prepare_reference_storage(root / "exam1", "Exam 1", "paper1", fingerprint)
            second = prepare_reference_storage(root / "exam2", "Exam 2", "paper1", fingerprint)
            invocations = []

            def process(**kwargs):
                invocations.append(1)
                data = base64.b64encode(f"image-{len(invocations)}".encode()).decode()
                return SimpleNamespace(pages=[SimpleNamespace(
                    markdown="![x](img.jpeg)",
                    images=[SimpleNamespace(id="img.jpeg", image_base64=data)],
                )])

            engine = MistralOCREngine.__new__(MistralOCREngine)
            engine.enable_cache = True
            engine.model_name = "test"
            engine.slice_pdf_pages = lambda *args, **kwargs: (pdf, False)
            engine.client = SimpleNamespace(
                files=SimpleNamespace(
                    list=lambda: SimpleNamespace(data=[]),
                    upload=lambda **kwargs: SimpleNamespace(id="file"),
                    get_signed_url=lambda **kwargs: SimpleNamespace(url="url"),
                ),
                ocr=SimpleNamespace(process=process),
            )
            result1 = engine.process_pdf(pdf, first, enhance=False)
            result2 = engine.process_pdf(pdf, second, enhance=False)
            cached = engine.process_pdf(pdf, first, enhance=False)
            self.assertEqual(len(invocations), 2)
            self.assertEqual(Path(result1.pages[0].images["img.jpeg"]).read_bytes(), b"image-1")
            self.assertEqual(Path(result2.pages[0].images["img.jpeg"]).read_bytes(), b"image-2")
            self.assertEqual(Path(cached.pages[0].images["img.jpeg"]).resolve(), (first / "img.jpeg").resolve())
            self.assertTrue(list((first / "cache").glob("*.json")))
            self.assertTrue(list((second / "cache").glob("*.json")))

    def test_reference_directory_rejects_different_exam(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fingerprint = {"format": ".pdf", "sha256": "abc"}
            first = prepare_reference_storage(root, "Exam One", "paper1", fingerprint)
            self.assertEqual(first, prepare_reference_storage(root, "Exam One", "paper1", fingerprint))
            with self.assertRaisesRegex(ValueError, "separate output directory"):
                prepare_reference_storage(root, "Exam Two", "paper1", fingerprint)

    def test_legacy_pdf_ocr_cache_is_copied_with_portable_image_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"fixture")
            fingerprint = reference_fingerprint(pdf)
            target = reference_storage_dir(root / "out", "paper1", fingerprint)
            legacy = root / "out" / "reference" / "exam_one" / "paper1" / target.name
            image_data = base64.b64encode(b"image-data").decode()
            engine = MistralOCREngine.__new__(MistralOCREngine)
            engine.enable_cache = True
            engine.model_name = "test"
            engine.slice_pdf_pages = lambda *args, **kwargs: (pdf, False)
            engine.client = SimpleNamespace(
                files=SimpleNamespace(
                    list=lambda: SimpleNamespace(data=[]),
                    upload=lambda **kwargs: SimpleNamespace(id="file"),
                    get_signed_url=lambda **kwargs: SimpleNamespace(url="url"),
                ),
                ocr=SimpleNamespace(process=lambda **kwargs: SimpleNamespace(
                    pages=[SimpleNamespace(markdown="![x](img.jpeg)", images=[
                        SimpleNamespace(id="img.jpeg", image_base64=image_data)
                    ])]
                )),
            )
            engine.process_pdf(pdf, legacy, enhance=False)
            engine.client.ocr.process = lambda **kwargs: self.fail("unexpected live OCR")
            migrated = prepare_reference_storage(root / "out", "Exam One", "paper1", fingerprint)
            cached = engine.process_pdf(pdf, migrated, enhance=False)
            self.assertEqual(Path(cached.pages[0].images["img.jpeg"]).resolve(), (target / "img.jpeg").resolve())
            self.assertEqual((target / "img.jpeg").read_bytes(), b"image-data")
            self.assertTrue((legacy / "img.jpeg").is_file())

    def test_pdf_ocr_preserves_repeated_per_page_image_ids(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"fixture")
            first = base64.b64encode(b"first").decode("ascii")
            second = base64.b64encode(b"second").decode("ascii")
            pages = [
                SimpleNamespace(markdown="![x](img-0.jpeg)", images=[
                    SimpleNamespace(id="img-0.jpeg", image_base64=data)
                ])
                for data in (first, second)
            ]
            engine = MistralOCREngine.__new__(MistralOCREngine)
            engine.enable_cache = False
            engine.model_name = "test"
            engine.slice_pdf_pages = lambda *args, **kwargs: (pdf, False)
            engine.client = SimpleNamespace(
                files=SimpleNamespace(
                    list=lambda: SimpleNamespace(data=[]),
                    upload=lambda **kwargs: SimpleNamespace(id="file"),
                    get_signed_url=lambda **kwargs: SimpleNamespace(url="url"),
                ),
                ocr=SimpleNamespace(process=lambda **kwargs: SimpleNamespace(pages=pages)),
            )
            result = engine.process_pdf(pdf, root / "images", enhance=False)
            first_path = Path(result.pages[0].images["img-0.jpeg"])
            second_path = Path(result.pages[1].images["img-0-page-2.jpeg"])
            self.assertEqual(first_path.read_bytes(), b"first")
            self.assertEqual(second_path.read_bytes(), b"second")
            self.assertIn("img-0-page-2.jpeg", result.pages[1].markdown)

    def test_paper_generator_applies_style_to_generation_request(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "reference.md"
            paper.write_text("# Physics\n1. A circuit question.", encoding="utf-8")
            syllabus = root / "syllabus.md"
            syllabus.write_text("# Motion", encoding="utf-8")
            spec = root / "spec.yaml"
            spec.write_text(
                "exam_name: Test\nsubjects:\n"
                "  - name: Physics\n    total_questions: 1\n    syllabus_file: syllabus.md\n",
                encoding="utf-8",
            )
            communicator = ReferenceCommunicator()
            original_generate = communicator.generate

            def generate(**kwargs):
                if "assessment style" in kwargs["system_instruction"]:
                    return original_generate(**kwargs)
                communicator.calls.append(kwargs)
                return "<quiz />"

            communicator.generate = generate
            generator = PaperGenerator(
                communicator=communicator, ocr_engine=object(),
                reference_paper=paper, staging_dir=root,
            )
            generator.renderer = SimpleNamespace(
                render=lambda output, directory, stem: directory / f"{stem}.xml"
            )
            with patch("academic_content_pipeline.content_processors.write_prompt_snapshot"):
                generator.process_spec(spec, root / "out")
            self.assertEqual(len(communicator.calls), 2)
            generated_prompt = communicator.calls[-1]["contents"].text
            self.assertIn("generate paper questions for Physics", communicator.calls[-1]["purpose"])
            self.assertIn("analyze reference paper style", communicator.calls[0]["purpose"])
            self.assertIn("Use multi-step calculations and circuits", generated_prompt)
            self.assertIn("Use velocity graphs", generated_prompt)
            self.assertIn("A circuit question", generated_prompt)
            self.assertIn("current syllabus, spec", generated_prompt)

    def test_prepared_paper_profile_is_used_without_shared_reference(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "syllabus.md").write_text("# Motion", encoding="utf-8")
            spec = root / "spec.yaml"
            spec.write_text(
                "exam_name: Test\nsubjects:\n"
                "  - name: Physics\n    total_questions: 1\n    syllabus_file: syllabus.md\n",
                encoding="utf-8",
            )
            communicator = ReferenceCommunicator()
            communicator.generate = lambda **kwargs: communicator.calls.append(kwargs) or "<quiz />"
            generator = PaperGenerator(communicator=communicator, ocr_engine=object())
            generator.reference_profile = {"subjects": {"Physics": {
                "style": "Paper-specific style", "chapters": {}, "examples": []
            }}}
            generator.renderer = SimpleNamespace(render=lambda output, directory, stem: directory / f"{stem}.xml")
            with patch("academic_content_pipeline.content_processors.write_prompt_snapshot"):
                generator.process_spec(spec, root / "out")
            self.assertIn("Paper-specific style", communicator.calls[0]["contents"].text)

    def test_markdown_analysis_is_cached_and_tracks_images(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image = root / "figure.png"
            Image.new("RGB", (2, 2)).save(image)
            paper = root / "paper.md"
            paper.write_text("# Physics\n1. A circuit question.\n![figure](figure.png)", encoding="utf-8")
            communicator = ReferenceCommunicator()
            cache = root / "profile.json"
            first = prepare_reference(paper, ["Physics"], communicator, None, root, cache)
            self.assertEqual(len(communicator.calls), 1)
            self.assertEqual(len(communicator.calls[0]["contents"].images), 1)
            self.assertEqual(first, prepare_reference(paper, ["Physics"], communicator, None, root, cache))
            self.assertEqual(len(communicator.calls), 1)
            self.assertIn("velocity graphs", reference_guidance(first, ["Physics"], "# Motion"))
            self.assertNotIn("velocity graphs", reference_guidance(first, ["Physics"], "# Optics"))
            self.assertNotIn("A circuit question", reference_guidance(first, ["Physics"], "# Optics"))
            Image.new("RGB", (3, 3)).save(image)
            prepare_reference(paper, ["Physics"], communicator, None, root, cache)
            self.assertEqual(len(communicator.calls), 2)

    def test_pdf_reference_inputs_and_xml_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "paper.xml"
            paper.write_text("<quiz/>", encoding="utf-8")
            communicator = ReferenceCommunicator()
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                prepare_reference(paper, ["Physics"], communicator, None, root, root / "xml.json")
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"fixture")
            ocr = SimpleNamespace(process_pdf=lambda *_: SimpleNamespace(
                pages=[SimpleNamespace(markdown="1. Physics question", images={})]
            ))
            prepare_reference(pdf, ["Physics"], communicator, ocr, root, root / "pdf.json")
            self.assertIn("Physics question", communicator.calls[-1]["contents"].text)

    def test_all_pdf_pages_and_images_batched_or_full(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"fixture")
            pages = []
            for number in range(1, 44):
                image = root / f"{number}.png"
                Image.new("RGB", (2, 2), color=(number, 0, 0)).save(image)
                pages.append(SimpleNamespace(
                    markdown=f"{number}. PAGE_MARKER_{number}",
                    images={image.name: str(image)},
                ))
            ocr = SimpleNamespace(process_pdf=lambda *_: SimpleNamespace(pages=pages))
            communicator = ReferenceCommunicator()
            prepare_reference(pdf, ["Physics"], communicator, ocr, root, root / "batch.json")
            self.assertEqual([len(c["contents"].images) for c in communicator.calls], [20, 20, 3])
            self.assertIn("PAGE_MARKER_43", communicator.calls[-1]["contents"].text)
            communicator = ReferenceCommunicator()
            prepare_reference(pdf, ["Physics"], communicator, ocr, root, root / "full.json", analysis_mode="full")
            self.assertEqual(len(communicator.calls), 1)
            self.assertEqual(len(communicator.calls[0]["contents"].images), 43)
            self.assertIn("PAGE_MARKER_43", communicator.calls[0]["contents"].text)

    def test_interrupted_markdown_batches_resume(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "reference.md"
            paper.write_text("\n".join(f"{i}. Question {i}" for i in range(1, 46)), encoding="utf-8")
            communicator = ReferenceCommunicator()
            original = communicator.generate
            def failing(**kwargs):
                if len(communicator.calls) == 1:
                    raise RuntimeError("interrupted")
                return original(**kwargs)
            communicator.generate = failing
            cache = root / "profile.json"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                prepare_reference(paper, ["Physics"], communicator, None, root, cache)
            resumed = ReferenceCommunicator()
            prepare_reference(paper, ["Physics"], resumed, None, root, cache)
            self.assertEqual(len(resumed.calls), 2)
            self.assertIn("Question 45", resumed.calls[-1]["contents"].text)

    def test_markdown_analysis_uses_fixed_twenty_question_batches_or_full(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "reference.md"
            paper.write_text(
                "\n".join(f"{i}. Question {i}" for i in range(1, 44)), encoding="utf-8"
            )
            batched = ReferenceCommunicator()
            prepare_reference(paper, ["Physics"], batched, None, root, root / "batch.json")
            for call, first, last in zip(batched.calls, (1, 21, 41), (20, 40, 43)):
                text = call["contents"].text.split("\n\nReference text:\n", 1)[1]
                self.assertEqual(len([line for line in text.splitlines() if line.strip()]), last - first + 1)
                self.assertTrue(text.startswith(f"{first}. Question {first}"))
                self.assertTrue(text.rstrip().endswith(f"{last}. Question {last}"))
            self.assertEqual(len(batched.calls), 3)
            full = ReferenceCommunicator()
            prepare_reference(
                paper, ["Physics"], full, None, root, root / "full.json", analysis_mode="full"
            )
            self.assertEqual(len(full.calls), 1)
            self.assertIn("43. Question 43", full.calls[0]["contents"].text)

    def test_invalid_reference_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaises(FileNotFoundError):
                reference_fingerprint(root / "missing.pdf")
            path = root / "unsupported.txt"
            path.write_text("test", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                reference_fingerprint(path)
            path = root / "paper.md"
            path.write_text("![diagram](missing.png)", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                reference_fingerprint(path)

    def test_invalid_model_example_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "paper.md"
            paper.write_text("1. A question", encoding="utf-8")
            communicator = SimpleNamespace(generate=lambda **kwargs: json.dumps({
                "subjects": {"Physics": {
                    "style": "Conceptual.", "chapters": {},
                    "examples": [{"id": "missing", "chapter": ""}],
                }}
            }))
            with self.assertRaisesRegex(ValueError, "Invalid reference example"):
                prepare_reference(
                    paper, ["Physics"], communicator, None, root, root / "cache.json"
                )

    def test_reference_change_during_analysis_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paper = root / "paper.md"
            paper.write_text("1. A question", encoding="utf-8")
            communicator = ReferenceCommunicator()
            generate = communicator.generate

            def change_reference(**kwargs):
                result = generate(**kwargs)
                paper.write_text("1. A different question", encoding="utf-8")
                return result

            communicator.generate = change_reference
            with self.assertRaisesRegex(ValueError, "changed during style analysis"):
                prepare_reference(
                    paper, ["Physics"], communicator, None, root, root / "cache.json"
                )
            self.assertFalse((root / "cache.json").exists())


if __name__ == "__main__":
    unittest.main()
