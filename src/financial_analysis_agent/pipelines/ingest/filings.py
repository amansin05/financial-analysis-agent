"""Shared helpers for turning an 8-K press-release exhibit into segments.

Used by both scripts.ingest_8k (Phase 1 CLI) and src.pipeline (Phase 4 monitor)
so the parsing logic lives in exactly one place.
"""
from __future__ import annotations

# Header noise some inline-XBRL exhibits prepend (the doc filename, "EX-99.1", etc.)
_NOISE = {"ex-99.1", "ex99.1", "exhibit 99.1"}


def derive_period(report_date: str | None) -> tuple[int | None, int | None]:
    """Calendar year + quarter from an ISO report date (approx; not fiscal)."""
    if not report_date:
        return None, None
    try:
        y, m, _ = (int(x) for x in report_date.split("-"))
        return y, (m - 1) // 3 + 1
    except (ValueError, AttributeError):
        return None, None


def _clean_paragraph(p: str, skip: set[str]) -> str:
    """Drop inline-XBRL header noise lines (filename, 'EX-99.1', bare numbers)."""
    kept = []
    for line in p.splitlines():
        low = line.strip().lower()
        if not low or low in skip or low.isdigit():
            continue
        kept.append(line.strip())
    return "\n".join(kept).strip()


def build_segments(paragraphs: list[str], exhibit: str) -> list[dict]:
    """Press releases have no speakers -> store each paragraph as a segment."""
    skip = _NOISE | {exhibit.lower()}
    segs = []
    seq = 0
    for p in paragraphs:
        p = _clean_paragraph(p, skip)
        if len(p) < 2:
            continue
        segs.append({
            "seq": seq,
            "speaker_name": None,
            "speaker_role": None,
            "section": "prepared",
            "text": p,
        })
        seq += 1
    return segs
