"""Minimal HTML -> plain text, stdlib only (no bs4 dependency).

Good enough for SEC press-release exhibits: drops script/style, turns block
elements into line breaks, decodes entities, and collapses runaway whitespace.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "section", "article", "header", "footer",
}
_DROP = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag == "td" or tag == "th":
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in _DROP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    # Normalize whitespace: trim trailing spaces, collapse blank-line runs.
    text = re.sub(r"[ \t]*\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def to_paragraphs(text: str, *, min_len: int = 1) -> list[str]:
    """Split cleaned text into paragraph blocks for per-segment storage."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
    return [b for b in blocks if len(b) >= min_len]
