#!/usr/bin/env python3
"""Check NCERT Exemplar Moodle XML structure and language question counts."""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from academic_content_pipeline.content_processors import (
    validate_ncert_exemplar_files,
    validate_ncert_exemplar_questions,
)


LANGUAGES = {
    "english_bengali": "_english_bengali.xml",
    "english_hindi": "_english_hindi.xml",
    "english": "_english.xml",
}


def check_outputs(root: Path) -> list[str]:
    groups: dict[tuple[Path, str], dict[str, Path]] = {}
    for path in sorted(root.rglob("*.xml")):
        for language, suffix in LANGUAGES.items():
            if path.name.casefold().endswith(suffix):
                base = path.name[:-len(suffix)]
                groups.setdefault((path.parent, base), {})[language] = path
                break

    if not groups:
        return [f"No NCERT Exemplar language XML files found under {root}."]

    errors = []
    for (folder, base), files in sorted(groups.items()):
        counts: dict[str, int] = {}
        for language in LANGUAGES:
            path = files.get(language)
            if path is None:
                errors.append(f"{folder / base}: missing {language} XML.")
                continue
            try:
                quiz = ET.parse(path).getroot()
                if quiz.tag != "quiz":
                    raise ValueError("Root element must be quiz.")
                nodes = [
                    ET.tostring(question, encoding="unicode")
                    for question in quiz
                    if question.get("type") != "category"
                ]
                validate_ncert_exemplar_questions(nodes)
                validate_ncert_exemplar_files(quiz)
                counts[language] = len(nodes)
            except (OSError, ET.ParseError, ValueError) as exc:
                errors.append(f"{path}: {exc}")

        english = counts.get("english")
        if english is None:
            continue
        for language in ("english_bengali", "english_hindi"):
            translated = counts.get(language)
            if translated is None:
                continue
            if english != translated:
                errors.append(
                    f"{files[language]}: question count mismatch; "
                    f"English={english}, {language}={translated}."
                )
        print(f"{folder / base}: {english} English questions checked.")
        if not english:
            print(f"WARNING: {files['english']} contains no questions; review the source.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Root of extracted language XML files.")
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"Not a directory: {args.root}")
    errors = check_outputs(args.root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Language question counts and Moodle question structure are consistent.")
    print("Equal counts do not prove that the language variants contain the same questions.")
    print("Review source coverage, answers, distractors, translations and visuals separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
