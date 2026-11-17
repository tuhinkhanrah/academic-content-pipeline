"""Path selection shared by mirrored content runners."""

import sys
from pathlib import Path


def resolve_subtree(root: Path, subtree: Path | None) -> Path:
    if subtree is None:
        return root
    if subtree.is_absolute():
        raise ValueError("--subtree must be a relative directory inside the root.")
    selected = (root / subtree).resolve()
    if not selected.is_relative_to(root) or not selected.is_dir():
        raise ValueError("--subtree must be an existing directory inside the root.")
    return selected


def rebuild_mirrored_combined_xml(output_root: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from academic_content_pipeline.moodle_combiner import rebuild_combined_xml

    try:
        combined = rebuild_combined_xml(output_root)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(
            f"Generation finished, but combined Moodle XML could not be rebuilt: {exc}"
        ) from exc
    print(f"Rebuilt {len(combined)} combined Moodle XML file(s).")
