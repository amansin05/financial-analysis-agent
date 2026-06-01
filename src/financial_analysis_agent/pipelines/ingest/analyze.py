"""Phase 2 analysis: extract spoken figures, reconcile vs XBRL, score sentiment.

Grounding rules (plan principle #1):
- XBRL numbers are the only "truth" (stored source='xbrl').
- LLM-extracted figures are stored source='spoken' and every one is reconciled
  against the matching XBRL value; mismatches are flagged, never trusted.
- Each extracted figure and each sentiment driver links to the segment it came
  from via a `citations` row, so every claim is traceable to source text.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from financial_analysis_agent.services.groq import GroqClient

EXTRACT_PROMPT_VERSION = "extract-v1"
SENTIMENT_PROMPT_VERSION = "sentiment-v1"

_SCALE = {"billion": 1e9, "million": 1e6, "thousand": 1e3, "none": 1.0, "": 1.0}

# Relative tolerance: press releases round (e.g. $111.2B vs 111.184B = 0.01%).
_MATCH_TOLERANCE = 0.01  # 1%

_KNOWN_METRICS = (
    "revenue", "gross_profit", "operating_income",
    "net_income", "eps_basic", "eps_diluted",
)

# ----------------------------- extraction -----------------------------

_EXTRACT_SYS = (
    "You extract financial figures verbatim from earnings text. You never invent "
    "numbers; you only report figures explicitly present in the text. Respond with "
    "ONLY valid JSON."
)

_EXTRACT_INSTRUCTIONS = """From the text below, extract ONLY the TOTAL-COMPANY headline
figures for the CURRENT QUARTER. Do NOT include segment/product/geographic breakdowns
(e.g. "Data Center revenue"), prior-year comparatives, or year-to-date totals.
Include AT MOST ONE figure per metric -- the company-wide quarterly value.

Return JSON: {"figures": [ {
  "metric": one of ["revenue","gross_profit","operating_income","net_income","eps_basic","eps_diluted"],
  "value": numeric value as written (e.g. 111.2),
  "scale": one of ["billion","million","thousand","none"],
  "unit": one of ["USD","USD/share"],
  "period_hint": "quarter",
  "source_quote": the exact sentence fragment from the text containing this figure
} ] }
Only include figures actually stated for the total company this quarter.
Do not include any text outside the JSON.

TEXT:
"""


def _concat(segments: list[dict], max_chars: int = 110_000) -> str:
    parts = []
    for s in segments:
        spk = s.get("speaker_name")
        parts.append(f"{spk}: {s['text']}" if spk else s["text"])
    body = "\n\n".join(parts)
    return body[:max_chars]


def _parse_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1:
        return {"_raw": raw, "_parse_error": True}
    try:
        return json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw, "_parse_error": True}


def extract_figures(
    segments: list[dict], *, client: GroqClient | None = None
) -> tuple[list[dict], str]:
    """LLM-extract stated figures. Returns (figures, model)."""
    client = client or GroqClient()
    raw = client.chat(
        _EXTRACT_INSTRUCTIONS + _concat(segments),
        system=_EXTRACT_SYS,
        temperature=0.0,
        max_tokens=2000,
    )
    data = _parse_json(raw)
    if data.get("_parse_error"):
        print("  ! figure extraction returned unparseable JSON (skipping figures)")
    return data.get("figures", []), client.model


def normalize_value(fig: dict) -> float | None:
    try:
        return float(fig["value"]) * _SCALE.get(str(fig.get("scale", "none")).lower(), 1.0)
    except (KeyError, ValueError, TypeError):
        return None


# ----------------------------- reconciliation -----------------------------

_USD_METRICS = {"revenue", "gross_profit", "operating_income", "net_income"}


def reconcile(figures: list[dict], xbrl_metrics: dict[str, dict]) -> list[dict]:
    """Compare each quarter-scoped extracted figure to its XBRL counterpart."""
    results = []
    for fig in figures:
        metric = fig.get("metric")
        if metric not in _KNOWN_METRICS:
            continue
        # Only reconcile quarter figures against our quarterly XBRL facts.
        if fig.get("period_hint") not in (None, "quarter"):
            continue
        # A dollar metric reported as a percent is a margin, not the figure -- skip.
        if metric in _USD_METRICS and str(fig.get("unit", "")).lower() == "percent":
            continue
        extracted = normalize_value(fig)
        xbrl = xbrl_metrics.get(metric)
        if xbrl is None:
            status, pct = "no_xbrl", None
        elif extracted is None:
            status, pct = "unparseable", None
        else:
            xv = float(xbrl["val"])
            pct = abs(extracted - xv) / xv if xv else None
            status = "match" if (pct is not None and pct <= _MATCH_TOLERANCE) else "mismatch"
        results.append({
            "metric": metric,
            "extracted_value": extracted,
            "xbrl_value": float(xbrl["val"]) if xbrl else None,
            "unit": (xbrl or {}).get("unit") or fig.get("unit"),
            "pct_diff": round(pct, 5) if pct is not None else None,
            "status": status,
            "source_quote": fig.get("source_quote", ""),
        })
    return results


def headline_verdict(
    reconciliation: list[dict], xbrl_metrics: dict[str, dict]
) -> dict[str, dict]:
    """Per-XBRL-metric verdict: did ANY extracted figure match the headline?

    Earnings text contains segment breakdowns and prior-period figures that the
    extractor tags with the same metric name. Comparing every one to the company
    total produces false 'mismatch' noise. The trustworthy question is per metric:
    is the headline figure corroborated by the text? -> pick the closest extraction.
    """
    verdict: dict[str, dict] = {}
    for metric, xb in xbrl_metrics.items():
        cands = [r for r in reconciliation
                 if r["metric"] == metric and r["pct_diff"] is not None]
        if not cands:
            verdict[metric] = {"status": "no_extraction", "xbrl_value": float(xb["val"]),
                               "best_extracted": None, "pct_diff": None, "unit": xb.get("unit")}
            continue
        best = min(cands, key=lambda r: r["pct_diff"])
        verdict[metric] = {
            "status": "verified" if best["status"] == "match" else "unconfirmed",
            "xbrl_value": float(xb["val"]),
            "best_extracted": best["extracted_value"],
            "pct_diff": best["pct_diff"],
            "unit": xb.get("unit"),
        }
    return verdict


# ----------------------------- sentiment -----------------------------

_SENTIMENT_SYS = (
    "You are an equity-research analyst gauging management tone from earnings "
    "text. Respond with ONLY valid JSON."
)

_SENTIMENT_INSTRUCTIONS = """Assess the tone of this earnings text.
Return JSON: {
  "overall_tone": one of ["optimistic","confident","cautious","defensive","mixed"],
  "score": number from -1.0 (very negative) to 1.0 (very positive),
  "drivers": [ {"point": "what drives the tone", "source_quote": "exact fragment from text"} ],
  "summary": "2-3 sentence tone assessment"
}
Do not include any text outside the JSON.

TEXT:
"""


def analyze_sentiment(
    segments: list[dict], *, client: GroqClient | None = None
) -> tuple[dict, str]:
    client = client or GroqClient()
    raw = client.chat(
        _SENTIMENT_INSTRUCTIONS + _concat(segments),
        system=_SENTIMENT_SYS,
        temperature=0.2,
        max_tokens=900,
    )
    return _parse_json(raw), client.model


# ----------------------------- grounding (citations) -----------------------------

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "")).strip().lower()


def find_segment_for_quote(
    conn: sqlite3.Connection, call_id: int, quote: str, *, probe_len: int = 40
) -> int | None:
    """Best-effort map a quote back to the segment_id it came from."""
    q = _norm(quote)
    if len(q) < 8:
        return None
    probe = q[:probe_len]
    rows = conn.execute(
        "SELECT id, text FROM segments WHERE call_id = ? ORDER BY seq", (call_id,)
    ).fetchall()
    for r in rows:
        if probe in _norm(r["text"]):
            return r["id"]
    # Looser fallback: any segment containing a long word from the quote.
    for r in rows:
        if q[:20] and q[:20] in _norm(r["text"]):
            return r["id"]
    return None


def store_analysis(
    conn: sqlite3.Connection,
    call_id: int,
    kind: str,
    content: dict,
    model: str,
    prompt_version: str,
    quotes: list[str] | None = None,
) -> int:
    """Insert an analysis row + a citation per grounding quote. Returns analysis_id.

    Idempotent per (call_id, kind): replaces any prior analysis of the same kind
    for this call (its citations cascade-delete) so re-running doesn't accumulate.
    """
    conn.execute(
        "DELETE FROM analyses WHERE call_id = ? AND kind = ?", (call_id, kind)
    )
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO analyses (call_id, kind, content, model, prompt_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (call_id, kind, json.dumps(content, ensure_ascii=False), model, prompt_version, now),
    )
    analysis_id = cur.lastrowid
    for quote in quotes or []:
        if not quote:
            continue
        seg_id = find_segment_for_quote(conn, call_id, quote)
        conn.execute(
            "INSERT INTO citations (analysis_id, segment_id, quote) VALUES (?, ?, ?)",
            (analysis_id, seg_id, quote),
        )
    return analysis_id


def store_spoken_financials(
    conn: sqlite3.Connection, call_id: int, reconciliation: list[dict]
) -> int:
    """Store LLM-extracted figures as source='spoken' (never trusted as truth)."""
    conn.execute(
        "DELETE FROM financials WHERE call_id = ? AND source = 'spoken'", (call_id,)
    )
    rows = [
        {
            "call_id": call_id,
            "metric": r["metric"],
            "value": r["extracted_value"],
            "unit": r["unit"],
            "period": r["status"],   # store reconciliation status in period slot
            "source": "spoken",
        }
        for r in reconciliation
        if r["extracted_value"] is not None
    ]
    conn.executemany(
        "INSERT INTO financials (call_id, metric, value, unit, period, source) "
        "VALUES (:call_id, :metric, :value, :unit, :period, :source)",
        rows,
    )
    return len(rows)
