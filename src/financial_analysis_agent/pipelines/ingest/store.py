"""Phase 1 ingest: transcript -> companies / calls / segments in SQLite.

Pure parsing + persistence; no network here (callers pass fetched data in).
Idempotent: re-ingesting the same (company, fy, quarter) replaces its segments
rather than duplicating them, leaning on the UNIQUE constraints in schema.sql.
"""
from __future__ import annotations

import re
import sqlite3

# Phrases an operator uses to hand off from prepared remarks to Q&A.
_QA_TRIGGERS = re.compile(
    r"question[-\s]and[-\s]answer|"
    r"\bq\s*&\s*a\b|"
    r"\(operator instructions\)|"
    r"first question|"
    r"take (?:our|the) first question|"
    r"begin the question",
    re.IGNORECASE,
)

_ROLE_PATTERNS = [
    ("Operator", re.compile(r"\boperator\b", re.IGNORECASE)),
    ("CEO", re.compile(r"chief executive|\bceo\b|president and ceo", re.IGNORECASE)),
    ("CFO", re.compile(r"chief financial|\bcfo\b", re.IGNORECASE)),
    ("CTO", re.compile(r"chief technology|\bcto\b", re.IGNORECASE)),
    ("COO", re.compile(r"chief operating|\bcoo\b", re.IGNORECASE)),
    ("IR", re.compile(r"investor relations|\bir\b", re.IGNORECASE)),
]


def infer_role(speaker: str, *, in_qa: bool) -> str | None:
    """Best-effort role from the speaker label. Honest about uncertainty.

    Titles are detected when present in the label. In the Q&A section, an
    otherwise-unknown speaker is most often an analyst asking a question, so we
    tag them 'Analyst' -- refined later in Phase 4's entity extraction.
    """
    s = speaker or ""
    for role, pat in _ROLE_PATTERNS:
        if pat.search(s):
            return role
    if in_qa:
        return "Analyst"
    return None


def parse_segments(transcript_split: list[dict]) -> list[dict]:
    """Turn API Ninjas' [{speaker, text}] into ordered segment rows.

    Adds seq, section ('prepared'|'qa'), and an inferred speaker_role.
    """
    segments: list[dict] = []
    in_qa = False
    for seq, item in enumerate(transcript_split):
        speaker = (item.get("speaker") or "").strip()
        text = (item.get("text") or "").strip()
        if not text:
            continue

        # Classify THIS segment with the current section, then flip the flag if
        # this turn announces Q&A -- the hand-off line itself (usually an operator
        # or an exec wrapping up) belongs to the prepared section; questions start
        # on the following turn.
        segments.append(
            {
                "seq": seq,
                "speaker_name": speaker or None,
                "speaker_role": infer_role(speaker, in_qa=in_qa),
                "section": "qa" if in_qa else "prepared",
                "text": text,
            }
        )
        if not in_qa and _QA_TRIGGERS.search(text):
            in_qa = True
    return segments


# --------------------------- persistence ---------------------------

def get_or_create_company(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str | None = None,
    sector: str | None = None,
    cik: str | None = None,
) -> int:
    cur = conn.cursor()
    row = None
    if cik:
        row = cur.execute("SELECT id FROM companies WHERE cik = ?", (cik,)).fetchone()
    if row is None and ticker:
        row = cur.execute(
            "SELECT id FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
    if row:
        cid = row["id"]
        # Backfill any fields we now know.
        cur.execute(
            "UPDATE companies SET name = COALESCE(?, name), "
            "sector = COALESCE(?, sector), cik = COALESCE(?, cik) WHERE id = ?",
            (name, sector, cik, cid),
        )
        return cid
    cur.execute(
        "INSERT INTO companies (ticker, cik, name, sector) VALUES (?, ?, ?, ?)",
        (ticker, cik, name, sector),
    )
    return cur.lastrowid


def upsert_call(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    fiscal_year: int,
    fiscal_quarter: int,
    call_date: str | None,
    source: str,
) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO calls (company_id, fiscal_year, fiscal_quarter, call_date, source) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(company_id, fiscal_year, fiscal_quarter) "
        "DO UPDATE SET call_date = COALESCE(excluded.call_date, calls.call_date), "
        "             source = excluded.source",
        (company_id, fiscal_year, fiscal_quarter, call_date, source),
    )
    row = cur.execute(
        "SELECT id FROM calls WHERE company_id = ? AND fiscal_year = ? "
        "AND fiscal_quarter = ?",
        (company_id, fiscal_year, fiscal_quarter),
    ).fetchone()
    return row["id"]


def replace_segments(
    conn: sqlite3.Connection, call_id: int, segments: list[dict]
) -> int:
    """Replace all segments for a call (idempotent re-ingest)."""
    cur = conn.cursor()
    cur.execute("DELETE FROM segments WHERE call_id = ?", (call_id,))
    cur.executemany(
        "INSERT INTO segments (call_id, seq, speaker_name, speaker_role, section, text) "
        "VALUES (:call_id, :seq, :speaker_name, :speaker_role, :section, :text)",
        [{"call_id": call_id, **s} for s in segments],
    )
    return len(segments)
