"""Pdf text/image extraction helpers shared by all paper sources."""

import os
import tempfile
from pathlib import Path

import pymupdf
from pymupdf.mupdf import FzErrorBase

from papers_agent.errors import PaperSourceError


class TempPdf:
    """Temporary file for a downloaded pdf, removed when the context exits."""

    def __init__(self) -> None:
        fd, name = tempfile.mkstemp(suffix=".pdf", prefix="papers-agent-")
        os.close(fd)
        self.path = Path(name)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        self.path.unlink(missing_ok=True)


def extract_pdf_text(path: Path) -> str:
    try:
        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text() for page in doc).strip()
    except Exception as exc:
        raise PaperSourceError("failed to extract text from pdf") from exc


def extract_pdf_images(path: Path, images_dir: Path, base: str) -> list[Path]:
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        doc = pymupdf.open(path)
    except FzErrorBase as exc:
        raise PaperSourceError("failed to open pdf") from exc
    with doc:
        for page_number, page in enumerate(doc, start=1):
            seen_xrefs: set[int] = set()
            for image_index, image in enumerate(page.get_images(full=True)):
                xref = image[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    pixmap = pymupdf.Pixmap(doc, xref)
                except FzErrorBase:
                    continue
                try:
                    if pixmap.n - pixmap.alpha > 3:
                        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
                    file = images_dir / f"{base}_image_{len(saved) + 1}.png"
                    pixmap.save(str(file))
                    saved.append(file)
                finally:
                    pixmap = None
    return saved
