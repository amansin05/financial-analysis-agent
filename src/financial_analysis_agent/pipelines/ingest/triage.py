"""Document triage: classify before you process (plan principle #2).

For a plain-text transcript there's nothing to triage -- `route_text()` returns
the 'text' route directly. For a PDF filing, `inventory_pdf()` walks each page
with PyMuPDF and reports whether it has a real text layer, embedded images, and
how dense its vector drawings are, so the caller can route pages to the cheapest
correct extractor (text vs. table vs. vision) instead of paying for vision blind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PageInfo:
    page: int
    chars: int          # length of the extracted text layer
    images: int         # embedded raster images
    drawings: int       # vector draw ops (lines/curves) -- proxy for charts/tables
    route: str = "text"  # text | table | vision


@dataclass
class DocInfo:
    path: str
    kind: str           # 'text' | 'pdf'
    pages: list[PageInfo] = field(default_factory=list)
    route: str = "text"  # document-level recommended route

    @property
    def summary(self) -> str:
        if self.kind == "text":
            return f"text input -> route '{self.route}'"
        n = len(self.pages)
        routes = {}
        for p in self.pages:
            routes[p.route] = routes.get(p.route, 0) + 1
        breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(routes.items()))
        return f"{n} pages -> doc route '{self.route}' (pages {breakdown})"


# Thresholds tuned to the plan's Netflix 10-K observation: clean text layers,
# tables made of vector lines (not charts), so vector-density alone shouldn't
# trigger vision -- only a page with images AND little text should.
_MIN_TEXT_CHARS = 100        # below this, the text layer is effectively empty
_VECTOR_TABLE_HINT = 40      # many vector ops + good text -> likely a table


def route_text() -> DocInfo:
    """Triage result for an already-text source (e.g. a fetched transcript)."""
    return DocInfo(path="<text>", kind="text", route="text")


def _classify_page(chars: int, images: int, drawings: int) -> str:
    # Real text layer present -> never pay for vision.
    if chars >= _MIN_TEXT_CHARS:
        if drawings >= _VECTOR_TABLE_HINT:
            return "table"   # dense vectors + text == ruled table, use pdfplumber
        return "text"
    # No usable text layer.
    if images > 0:
        return "vision"      # scanned / image-only page genuinely needs OCR/vision
    return "text"            # blank-ish page; cheapest route, nothing to extract


def inventory_pdf(path: str | Path) -> DocInfo:
    """Inventory a PDF page-by-page and recommend a route. Requires PyMuPDF."""
    import fitz  # imported lazily so the text path needs no PDF deps

    doc = DocInfo(path=str(path), kind="pdf")
    with fitz.open(path) as pdf:
        for i, page in enumerate(pdf):
            chars = len(page.get_text("text"))
            images = len(page.get_images(full=True))
            drawings = len(page.get_drawings())
            route = _classify_page(chars, images, drawings)
            doc.pages.append(PageInfo(i, chars, images, drawings, route))

    # Document-level route = the "heaviest" route any page needs.
    order = {"text": 0, "table": 1, "vision": 2}
    doc.route = max((p.route for p in doc.pages), key=lambda r: order[r], default="text")
    return doc
