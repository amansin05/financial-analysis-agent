"""Phase 4 entity extraction: topics, mentions, executives -- grounded to segments.

Populates the SQLite edge tables that the NetworkX graph is later built from:
  - topics / mentions      : themes discussed in a call (company -[discusses]- topic)
  - people / executive_tenure : execs named in the call (person -[exec_at]- company)

Every mention links to the segment_id it came from, so graph edges are traceable
back to source text. A controlled topic vocabulary keeps labels consistent across
companies, which is what makes cross-company theme propagation meaningful.
"""
from __future__ import annotations

import json
import sqlite3

from financial_analysis_agent.pipelines.ingest.analyze import find_segment_for_quote
from financial_analysis_agent.services.groq import GroqClient

ENTITY_PROMPT_VERSION = "entities-v1"

# Controlled vocabulary -> consistent topic nodes across companies. The LLM may
# add a few free-form topics, which we normalize (lowercased) on the way in.
SEED_TOPICS = [
    "artificial intelligence", "data center", "cloud computing",
    "capital expenditures", "services revenue", "gross margin",
    "share buybacks", "dividends", "guidance and outlook", "supply chain",
    "foreign exchange", "gaming", "advertising", "subscriptions",
    "operating leverage", "free cash flow",
]

_SYS = (
    "You extract structured entities from earnings text. You only report things "
    "explicitly present. Respond with ONLY valid JSON."
)

_INSTRUCTIONS = """From the earnings text below, extract:
1. TOPICS actually discussed. Prefer labels from this list (use the exact label):
{seed}
You may add up to 3 additional short topic labels if clearly discussed.
2. EXECUTIVES named, with their role and company.

Return JSON: {
  "topics": [ {"label": "...", "sentiment": number from -1.0 to 1.0,
               "source_quote": "exact fragment mentioning this topic"} ],
  "executives": [ {"name": "...", "role": "CEO|CFO|COO|CTO|other",
                   "source_quote": "exact fragment naming this person"} ]
}
Only include items explicitly in the text. No text outside the JSON.

TEXT:
"""


def _concat(segments: list[dict], max_chars: int = 90_000) -> str:
    out = []
    for s in segments:
        spk = s.get("speaker_name")
        out.append(f"{spk}: {s['text']}" if spk else s["text"])
    return "\n\n".join(out)[:max_chars]


def _parse(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1:
        return {}
    try:
        # strict=False allows literal control chars (tabs/newlines) inside strings.
        # The LLM copies source_quotes verbatim from tab-separated filing tables, so
        # quotes routinely contain raw tabs -- invalid in strict JSON, which would
        # silently drop every entity for table-heavy filings (e.g. SNDK).
        return json.loads(raw[start : end + 1], strict=False)
    except (json.JSONDecodeError, ValueError):
        return {}


def extract(segments: list[dict], *, client: GroqClient | None = None) -> tuple[dict, str]:
    client = client or GroqClient()
    prompt = _INSTRUCTIONS.replace("{seed}", ", ".join(SEED_TOPICS)) + _concat(segments)
    # json_mode guarantees valid JSON: prevents the model from emitting unescaped
    # quotes (in quoted speech) or raw tabs (from filing tables) that silently
    # dropped entities and left companies isolated in the graph.
    raw = client.chat(prompt, system=_SYS, temperature=0.0, max_tokens=1500, json_mode=True)
    return _parse(raw), client.model


# ----------------------------- persistence -----------------------------

# Map common LLM variants onto canonical seed labels so the graph doesn't
# fragment one concept across several nodes (e.g. dividend(s)/dividend payments).
_TOPIC_ALIASES = {
    "dividend": "dividends",
    "dividend payment": "dividends",
    "dividend payments": "dividends",
    "buyback": "share buybacks",
    "buybacks": "share buybacks",
    "share buyback": "share buybacks",
    "stock buyback": "share buybacks",
    "stock buybacks": "share buybacks",
    "capex": "capital expenditures",
    "capital expenditure": "capital expenditures",
    "ai": "artificial intelligence",
    "a.i.": "artificial intelligence",
    "cloud": "cloud computing",
    "cloud revenue": "cloud computing",
    "subscription": "subscriptions",
    "fx": "foreign exchange",
}


def _norm_topic(label: str) -> str:
    norm = " ".join((label or "").strip().lower().split())
    return _TOPIC_ALIASES.get(norm, norm)


def canonicalize_topics(conn: sqlite3.Connection) -> int:
    """Merge existing topic nodes onto canonical labels (remap mentions, drop dupes).

    Repairs data extracted before aliasing without re-running the LLM. Returns the
    number of topic rows merged away.
    """
    merged = 0
    rows = conn.execute("SELECT id, label FROM topics").fetchall()
    for r in rows:
        canon = _norm_topic(r["label"])
        if canon == r["label"]:
            continue
        canon_id = _get_or_create_topic(conn, canon)
        if canon_id == r["id"]:
            continue
        conn.execute(
            "UPDATE mentions SET target_topic_id = ? WHERE target_topic_id = ?",
            (canon_id, r["id"]),
        )
        conn.execute("DELETE FROM topics WHERE id = ?", (r["id"],))
        merged += 1
    return merged


def _get_or_create_topic(conn: sqlite3.Connection, label: str) -> int:
    conn.execute("INSERT OR IGNORE INTO topics (label) VALUES (?)", (label,))
    return conn.execute("SELECT id FROM topics WHERE label = ?", (label,)).fetchone()["id"]


def _get_or_create_person(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO people (full_name) VALUES (?)", (name,))
    return conn.execute(
        "SELECT id FROM people WHERE full_name = ?", (name,)
    ).fetchone()["id"]


def store(
    conn: sqlite3.Connection, call_id: int, company_id: int, extracted: dict
) -> dict:
    """Persist topics->mentions and executives->tenure. Idempotent per call."""
    # Idempotent: clear this call's mentions before re-inserting. We re-create both
    # topic AND person mentions below, so both must be cleared -- otherwise re-running
    # extraction duplicates every person mention.
    conn.execute(
        "DELETE FROM mentions WHERE call_id = ? AND target_type IN ('topic', 'person')",
        (call_id,),
    )

    n_topics = 0
    for t in extracted.get("topics", []):
        label = _norm_topic(t.get("label", ""))
        if not label:
            continue
        topic_id = _get_or_create_topic(conn, label)
        seg_id = find_segment_for_quote(conn, call_id, t.get("source_quote", ""))
        conn.execute(
            "INSERT INTO mentions (call_id, segment_id, target_type, target_topic_id, "
            "target_text, sentiment) VALUES (?, ?, 'topic', ?, ?, ?)",
            (call_id, seg_id, topic_id, label, t.get("sentiment")),
        )
        n_topics += 1

    n_execs = 0
    for e in extracted.get("executives", []):
        name = (e.get("name") or "").strip()
        if not name:
            continue
        person_id = _get_or_create_person(conn, name)
        conn.execute(
            "INSERT OR IGNORE INTO executive_tenure (person_id, company_id, role) "
            "VALUES (?, ?, ?)",
            (person_id, company_id, e.get("role")),
        )
        # Also record the exec as a person-mention grounded to a segment.
        seg_id = find_segment_for_quote(conn, call_id, e.get("source_quote", ""))
        conn.execute(
            "INSERT INTO mentions (call_id, segment_id, target_type, target_person_id, "
            "target_text) VALUES (?, ?, 'person', ?, ?)",
            (call_id, seg_id, person_id, name),
        )
        n_execs += 1

    return {"topics": n_topics, "executives": n_execs}


def derive_analyst_coverage(conn: sqlite3.Connection) -> int:
    """Free coverage edges from Analyst-role segments (transcript path only).

    Empty on the 8-K press-release path (no speakers); populated automatically
    once transcript ingestion provides speaker_role='Analyst' segments.
    """
    rows = conn.execute(
        "SELECT DISTINCT c.company_id, s.speaker_name, MIN(c.call_date) first_seen, "
        "       MAX(c.call_date) last_seen "
        "FROM segments s JOIN calls c ON c.id = s.call_id "
        "WHERE s.speaker_role = 'Analyst' AND s.speaker_name IS NOT NULL "
        "GROUP BY c.company_id, s.speaker_name"
    ).fetchall()
    n = 0
    for r in rows:
        analyst_id = _ensure_analyst(conn, r["speaker_name"])
        conn.execute(
            "INSERT INTO analyst_coverage (analyst_id, company_id, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(analyst_id, company_id) "
            "DO UPDATE SET first_seen = excluded.first_seen, last_seen = excluded.last_seen",
            (analyst_id, r["company_id"], r["first_seen"], r["last_seen"]),
        )
        n += 1
    return n


def _ensure_analyst(conn: sqlite3.Connection, full_name: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO analysts (full_name, firm_id) VALUES (?, NULL)", (full_name,)
    )
    return conn.execute(
        "SELECT id FROM analysts WHERE full_name = ? AND firm_id IS NULL", (full_name,)
    ).fetchone()["id"]
