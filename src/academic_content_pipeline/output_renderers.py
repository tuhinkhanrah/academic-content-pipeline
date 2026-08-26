"""Output rendering strategies for generated academic content."""

from pathlib import Path
from typing import Dict, Optional

try:
    from .pipeline_utils import (
        compile_html_to_pdf,
        compile_tex_to_pdf,
        fix_and_inject_moodle_xml,
    )
except ImportError:  # pragma: no cover - fallback for direct script execution
    from pipeline_utils import (
        compile_html_to_pdf,
        compile_tex_to_pdf,
        fix_and_inject_moodle_xml,
    )


class OutputRenderer:
    """Renders generated content as Moodle XML or a compiled PDF."""

    def __init__(self, output_format: str, pdf_engine: str = "html"):
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if self.output_format not in {"xml", "pdf"}:
            raise ValueError(f"Unsupported output format: {self.output_format}")
        if self.output_format == "pdf" and self.pdf_engine not in {"html", "tex"}:
            raise ValueError(f"Unsupported PDF engine: {self.pdf_engine}")

    def render(
        self,
        raw_output: str,
        output_dir: Path,
        output_stem: str,
        image_map: Optional[Dict[str, str]] = None,
    ) -> Path:
        """Render raw AI output and return the final artifact path."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        image_map = image_map or {}

        if self.output_format == "xml":
            final_path = output_dir / f"{output_stem}.xml"
            final_xml = fix_and_inject_moodle_xml(raw_output, image_map)
            final_path.write_text(final_xml, encoding="utf-8")
            return final_path

        final_path = output_dir / f"{output_stem}.pdf"
        if self.pdf_engine == "tex":
            compile_tex_to_pdf(raw_output, final_path, image_map=image_map)
        else:
            compile_html_to_pdf(raw_output, final_path, image_map=image_map)
        return final_path
