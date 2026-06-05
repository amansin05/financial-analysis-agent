"""Phase 3 Q&A: retrieve segments from Chroma, answer with grounded citations.

Flow: embed question -> Chroma similarity search (optional ticker filter) ->
fetch the AUTHORITATIVE segment text/context from SQLite (Chroma only points
back via segment_id) -> ask Groq to answer using ONLY those segments and cite
the segment ids it used. Answers are grounded; the model is told to say so when
the context doesn't contain the answer.
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.retrieve import vectorstore
from financial_analysis_agent.services.groq import GroqClient

QA_PROMPT_VERSION = "qa-v1"

# Common nicknames -> ticker (only applied if that ticker exists in the DB).
_NAME_ALIASES = {"google": "GOOGL", "alphabet": "GOOGL", "facebook": "META"}


def _company_index() -> dict[str, str]:
    """Map lowercased ticker + leading name word + aliases -> ticker."""
    idx: dict[str, str] = {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ticker, name FROM companies WHERE ticker IS NOT NULL").fetchall()
    tickers = {r["ticker"] for r in rows}
    for r in rows:
        idx[r["ticker"].lower()] = r["ticker"]
        if r["name"]:
            first = re.split(r"[ .,]", r["name"].strip())[0].lower()  # apple, microsoft, nvidia...
            if len(first) >= 3:
                idx[first] = r["ticker"]
    for alias, tk in _NAME_ALIASES.items():
        if tk in tickers:
            idx[alias] = tk
    return idx


def detect_tickers(question: str) -> list[str]:
    """Tickers/company names mentioned in the question (word-boundary, ordered, unique).

    Tolerant of typos: a question word that is a close fuzzy match to a known ticker or
    company name (e.g. "nvdia" -> NVDA, "microsft" -> MSFT) still resolves, so a single
    misspelling doesn't silently drop the company filter + leadership/financials bridge.
    """
    idx = _company_index()
    low = question.lower()
    found: list[str] = []

    # 1) Exact word-boundary matches on ticker / name-word / alias.
    for term, tk in idx.items():
        if re.search(r"\b" + re.escape(term) + r"\b", low) and tk not in found:
            found.append(tk)

    # 2) Fuzzy fallback for misspellings. Only words >=4 chars, high cutoff, to avoid
    #    matching ordinary English words to a ticker.
    keys = list(idx.keys())
    for word in re.findall(r"[a-z]{4,}", low):
        if word in idx:
            continue  # already an exact hit above
        close = difflib.get_close_matches(word, keys, n=1, cutoff=0.82)
        if close and idx[close[0]] not in found:
            found.append(idx[close[0]])
    return found

_SYS = (
    "You answer questions about earnings disclosures using ONLY the provided "
    "numbered segments. Never use outside knowledge or invent figures. If the "
    "segments don't contain the answer, say so plainly. Cite the segment ids you "
    "relied on. Respond with ONLY valid JSON."
)

_INSTRUCTIONS = """Answer the QUESTION using ONLY the context below: the numbered
segments and (when present) the [XBRL-verified financials] and [leadership] lines. The
financials line is authoritative for exact figures and margins; the leadership line is
authoritative for who holds which executive role. Prefer these lines over segment prose
for those facts. You may compute simple things (e.g. a margin, a growth rate, a
difference) from the verified financials.

Write a DETAILED, well-structured answer -- never a single bare line:
- Start with a direct one-sentence answer to the question.
- Then expand: cite concrete figures, margins, quarters, and short quotes from the
  context to support it. Explain the "so what", not just the number.
- If the context covers more than one company (or more than one quarter), COMPARE them
  explicitly: state who is higher/lower, by how much (absolute and/or %), and what it
  implies. A small markdown table or bullet list is welcome for comparisons.
- Use short paragraphs and "- " bullet lines (with real newlines) for readability.
- Keep EVERY claim grounded in the context. Never invent numbers, companies, or facts
  that are not present. If the context only partially answers, give what is supported
  and clearly state what is missing.

Return JSON: {
  "answer": "a detailed, multi-sentence answer; use \\n for line breaks and '- ' bullets",
  "citations": [integer segment ids you actually used; omit the financials/leadership lines, they have no id],
  "grounded": true if the context (segments, verified financials, OR leadership) answered it fully or partially, else false
}
Do not include any text outside the JSON.

QUESTION: {question}

CONTEXT:
{context}
"""


def _fetch_segments(seg_ids: list[int]) -> dict[int, dict]:
    if not seg_ids:
        return {}
    placeholders = ",".join("?" * len(seg_ids))
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT s.id, s.text, s.speaker_name, s.speaker_role, s.section, "
            f"       s.call_id, co.ticker, c.fiscal_year, c.fiscal_quarter "
            f"FROM segments s JOIN calls c ON c.id = s.call_id "
            f"JOIN companies co ON co.id = c.company_id "
            f"WHERE s.id IN ({placeholders})",
            seg_ids,
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _financials_block(tickers: list[str]) -> str:
    """XBRL-verified financials (+ computed margins) for each ticker's latest call."""
    blocks = []
    for ticker in tickers:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT f.metric, f.value, f.unit FROM financials f "
                "JOIN calls c ON c.id = f.call_id JOIN companies co ON co.id = c.company_id "
                "WHERE co.ticker = ? AND f.source = 'xbrl' "
                "AND c.id = (SELECT id FROM calls WHERE company_id = co.id "
                "            ORDER BY call_date DESC, id DESC LIMIT 1)",
                (ticker.upper(),),
            ).fetchall()
        if not rows:
            continue
        m = {r["metric"]: (r["value"], r["unit"]) for r in rows}
        parts = []
        for metric, (val, unit) in m.items():
            disp = f"${val/1e9:.2f}B" if unit == "USD" else (
                f"${val:.2f}/share" if unit == "USD/shares" else f"{val}")
            parts.append(f"{metric}={disp}")
        rev = m.get("revenue", (None,))[0]
        if rev:
            for base in ("gross_profit", "operating_income", "net_income"):
                if base in m and m[base][0] is not None:
                    parts.append(f"{base.replace('_', ' ')} margin={m[base][0]/rev*100:.1f}%")
        blocks.append(f"[{ticker} XBRL-verified financials, latest quarter]: " + "; ".join(parts))
    return "\n".join(blocks)


def _executives_block(tickers: list[str]) -> str:
    """Extracted leadership (executive_tenure) for each ticker's company.

    Authoritative for who-holds-which-role questions. The 8-K press-release path has
    no speaker segmentation, so a CEO/CFO name lives only as an attribution tail inside
    a long quote segment ("...said Dr. Lisa Su, AMD chair and CEO") -- which semantic
    search ranks poorly against "who is the CEO". This structured line answers it
    regardless of phrasing. Mirrors _financials_block: a no-id authoritative preamble.
    """
    blocks = []
    for ticker in tickers:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT p.full_name, et.role FROM executive_tenure et "
                "JOIN people p ON p.id = et.person_id "
                "JOIN companies co ON co.id = et.company_id "
                "WHERE co.ticker = ? AND COALESCE(et.role, '') <> 'other' "
                "ORDER BY et.id",
                (ticker.upper(),),
            ).fetchall()
        if not rows:
            continue
        people = "; ".join(f"{r['full_name']} - {r['role']}" for r in rows)
        blocks.append(f"[{ticker} leadership, extracted from filings]: {people}")
    return "\n".join(blocks)


def _format_context(hits: list[dict], segs: dict[int, dict]) -> str:
    lines = []
    for h in hits:
        sid = h["segment_id"]
        s = segs.get(sid)
        if not s:
            continue
        who = s["speaker_name"] or s["speaker_role"] or s["section"] or ""
        tag = f"{s['ticker']} FY{s['fiscal_year']}Q{s['fiscal_quarter']}"
        prefix = f"[seg {sid}] ({tag}{', ' + who if who else ''}): "
        lines.append(prefix + (s["text"] or "").strip())
    return "\n\n".join(lines)


def answer(
    question: str,
    *,
    ticker: str | None = None,
    n_results: int = 8,
    client: GroqClient | None = None,
    store: bool = True,
) -> dict:
    """Answer a question with grounded citations. Returns a result dict.

    If no ticker is passed, company names/tickers are auto-detected from the
    question: a single detected company filters retrieval AND injects its verified
    financials; multiple companies inject all their financials without filtering.
    """
    detected = [ticker.upper()] if ticker else detect_tickers(question)
    # Filter retrieval to a single named company; leave cross-company questions open.
    where = {"ticker": detected[0]} if len(detected) == 1 else None
    hits = vectorstore.query(question, n_results=n_results, where=where)
    if not hits:
        return {"answer": "No indexed segments matched the question.",
                "citations": [], "grounded": False, "hits": []}

    segs = _fetch_segments([h["segment_id"] for h in hits])
    context = _format_context(hits, segs)
    preamble = "\n".join(b for b in (_executives_block(detected), _financials_block(detected)) if b)
    if preamble:
        context = preamble + "\n\n" + context

    client = client or GroqClient()
    prompt = _INSTRUCTIONS.replace("{question}", question).replace("{context}", context)
    # json_mode guarantees valid JSON even when the (now detailed, multi-paragraph)
    # answer contains quotes/newlines; the larger budget gives room for comparisons.
    raw = client.chat(prompt, system=_SYS, temperature=0.2, max_tokens=1800, json_mode=True)
    start, end = raw.find("{"), raw.rfind("}")
    try:
        # strict=False: tolerate literal tabs/newlines the model may copy from filing tables.
        parsed = json.loads(raw[start : end + 1], strict=False) if start != -1 else {"answer": raw}
    except (json.JSONDecodeError, ValueError):
        parsed = {"answer": raw, "citations": [], "grounded": False}

    # Keep only citations that were actually in the retrieved set (no hallucinated ids).
    valid_ids = set(segs.keys())
    cited = [int(c) for c in parsed.get("citations", []) if int(c) in valid_ids] \
        if isinstance(parsed.get("citations"), list) else []

    result = {
        "question": question,
        "answer": parsed.get("answer", ""),
        "grounded": bool(parsed.get("grounded", bool(cited))),
        "citations": cited,
        "model": client.model,
        "hits": hits,
        "segments": segs,
    }
    if store and hits:
        _store(result)
    return result


def _store(result: dict) -> None:
    """Persist the Q&A as analyses(kind='qa_answer') + citations to cited segments."""
    hits = result["hits"]
    call_id = result["segments"].get(hits[0]["segment_id"], {}).get("call_id")
    if call_id is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO analyses (call_id, kind, content, model, prompt_version, created_at) "
            "VALUES (?, 'qa_answer', ?, ?, ?, ?)",
            (call_id, json.dumps({"question": result["question"], "answer": result["answer"],
                                  "grounded": result["grounded"]}, ensure_ascii=False),
             result["model"], QA_PROMPT_VERSION, now),
        )
        aid = cur.lastrowid
        for sid in result["citations"]:
            seg = result["segments"].get(sid, {})
            conn.execute(
                "INSERT INTO citations (analysis_id, segment_id, quote) VALUES (?, ?, ?)",
                (aid, sid, (seg.get("text") or "")[:300]),
            )
        conn.commit()
