"""Phase 1 summarization: full transcript -> structured summary via Groq.

Principle #1: meaning from the LLM, numbers from structured data. So the prompt
asks for qualitative interpretation and explicitly tells the model NOT to invent
precise figures -- exact metrics are reconciled against XBRL in Phase 2. The
result is stored as JSON in analyses.content, stamped with model + prompt_version.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from financial_analysis_agent.services.groq import GroqClient

PROMPT_VERSION = "summary-v1"

_SYSTEM = (
    "You are a precise equity-research analyst. You summarize earnings calls "
    "faithfully and never fabricate exact numbers. When a figure matters, refer "
    "to it qualitatively (e.g. 'revenue grew double digits') unless the speaker "
    "stated it explicitly, and even then attribute it as 'management said'. "
    "Exact verified figures are sourced separately. Respond with ONLY valid JSON."
)

_INSTRUCTIONS = """Summarize this {doc_kind}. Return JSON with exactly these keys:
{
  "headline": "one-sentence takeaway",
  "key_points": ["3-6 bullet strings covering the most important themes"],
  "guidance": "what management said about the outlook, or null if none",
  "qualitative_financials": "narrative on revenue/margin/segment trends WITHOUT inventing precise numbers",
  "risks": ["notable risks or concerns raised"],
  "tone": "one of: optimistic | confident | cautious | defensive | mixed",
  "notable_quotes": ["1-3 short verbatim quotes that capture the call"]
}
Do not include any text outside the JSON object.

DOCUMENT:
"""


def _build_prompt(
    segments: list[dict], doc_kind: str, max_chars: int = 110_000
) -> str:
    """Concatenate segments (speaker-tagged when known); truncate for context."""
    lines = []
    for s in segments:
        spk = s.get("speaker_name")
        role = s.get("speaker_role")
        if spk:
            tag = f"{spk} ({role})" if role else spk
            lines.append(f"{tag}: {s['text']}")
        else:
            lines.append(s["text"])  # speaker-less doc (e.g. press release)
    body = "\n\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n[...document truncated...]"
    # plain replace (not .format) -- the JSON schema below contains literal braces
    return _INSTRUCTIONS.replace("{doc_kind}", doc_kind) + body


def summarize_segments(
    segments: list[dict],
    *,
    doc_kind: str = "earnings call",
    client: GroqClient | None = None,
) -> tuple[dict, str]:
    """Return (parsed_summary_dict, model_name). Falls back to raw text on bad JSON."""
    client = client or GroqClient()
    prompt = _build_prompt(segments, doc_kind)
    raw = client.chat(prompt, system=_SYSTEM, temperature=0.2, max_tokens=1500)
    try:
        # Be forgiving if the model wraps JSON in prose/fences.
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start : end + 1]) if start != -1 else {"raw": raw}
    except (json.JSONDecodeError, ValueError):
        parsed = {"raw": raw, "_parse_error": True}
    return parsed, client.model


def store_summary(
    conn: sqlite3.Connection, call_id: int, summary: dict, model: str
) -> int:
    """Persist a summary analysis row (idempotent per call). Returns its id."""
    conn.execute("DELETE FROM analyses WHERE call_id = ? AND kind = 'summary'", (call_id,))
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO analyses (call_id, kind, content, model, prompt_version, created_at) "
        "VALUES (?, 'summary', ?, ?, ?, ?)",
        (call_id, json.dumps(summary, ensure_ascii=False), model, PROMPT_VERSION, now),
    )
    return cur.lastrowid
