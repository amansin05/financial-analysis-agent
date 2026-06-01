"""Phase 2 end-to-end: XBRL truth + reconciled spoken figures + sentiment.

Usage:
    python -m scripts.analyze_financials AAPL
    python -m scripts.analyze_financials MSFT --no-llm   # XBRL only

Requires the call to already be ingested (run scripts.ingest_8k first). Operates
on the company's most recent stored call.
"""
from __future__ import annotations

import argparse
import json
import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.ingest import analyze, xbrl
from financial_analysis_agent.services.edgar import EdgarClient


def _latest_call(conn, ticker: str):
    return conn.execute(
        "SELECT c.id call_id, c.fiscal_year, c.fiscal_quarter, co.cik, co.name "
        "FROM calls c JOIN companies co ON co.id = c.company_id "
        "WHERE co.ticker = ? ORDER BY c.call_date DESC, c.id DESC LIMIT 1",
        (ticker,),
    ).fetchone()


def _segments(conn, call_id: int) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT seq, speaker_name, speaker_role, section, text FROM segments "
            "WHERE call_id = ? ORDER BY seq", (call_id,)
        )
    ]


def _fmt(v, unit) -> str:
    if v is None:
        return "—"
    if unit == "USD":
        return f"${v/1e9:.2f}B"
    if unit == "USD/shares":
        return f"${v:.2f}/sh"
    return f"{v}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 2 financial analysis.")
    ap.add_argument("ticker")
    ap.add_argument("--no-llm", action="store_true", help="XBRL only; skip extraction+sentiment")
    args = ap.parse_args()
    ticker = args.ticker.upper()

    db.init_db()
    with db.connect() as conn:
        call = _latest_call(conn, ticker)
        if not call:
            print(f"  ! No stored call for {ticker}. Run: python -m scripts.ingest_8k {ticker}")
            return 2
        call_id = call["call_id"]
        segments = _segments(conn, call_id)
    print(f"== Phase 2 analysis: {call['name'] or ticker} "
          f"(call_id={call_id}, FY{call['fiscal_year']} Q{call['fiscal_quarter']}, "
          f"{len(segments)} segments) ==")

    # 1. XBRL truth.
    print("[1/4] Fetching XBRL financials (source='xbrl')...")
    metrics = xbrl.fetch_quarter_metrics(call["cik"], EdgarClient())
    with db.connect() as conn:
        n = xbrl.store_financials(conn, call_id, metrics)
        conn.commit()
    period = next(iter(metrics.values()), {}).get("end", "?") if metrics else "?"
    print(f"      stored {n} XBRL metrics (period end {period}):")
    for k, f in metrics.items():
        print(f"        {k:18} {_fmt(f['val'], f['unit'])}")

    if args.no_llm:
        print("[--] Skipped LLM extraction + sentiment (--no-llm).\n\nDone.")
        return 0

    # 2. Extract spoken figures + reconcile against XBRL.
    print("[2/4] Extracting stated figures + reconciling vs XBRL...")
    figures, ex_model = analyze.extract_figures(segments)
    recon = analyze.reconcile(figures, metrics)
    verdict = analyze.headline_verdict(recon, metrics)
    with db.connect() as conn:
        analyze.store_spoken_financials(conn, call_id, recon)
        analyze.store_analysis(
            conn, call_id, "reconciliation",
            {"headline_verdict": verdict, "all_figures": recon},
            ex_model, analyze.EXTRACT_PROMPT_VERSION,
            quotes=[r["source_quote"] for r in recon],
        )
        conn.commit()
    # Primary signal: per-metric headline verdict (is the XBRL figure corroborated?)
    n_verified = sum(1 for v in verdict.values() if v["status"] == "verified")
    print(f"      headline verdict ({n_verified}/{len(verdict)} corroborated by text):")
    for metric, v in verdict.items():
        mark = {"verified": "OK ", "unconfirmed": "!! ", "no_extraction": "—  "}[v["status"]]
        pct = f"({v['pct_diff']*100:.2f}%)" if v["pct_diff"] is not None else ""
        print(f"        [{mark}] {metric:16} xbrl={_fmt(v['xbrl_value'], v['unit']):>10}"
              f"  best_text={_fmt(v['best_extracted'], v['unit']):>10} {pct}")
    extra = len(recon) - len(verdict)
    print(f"      ({len(figures)} figures extracted; {max(extra,0)} additional "
          f"segment/period figures stored as source='spoken')")

    # 3. Sentiment.
    print("[3/4] Sentiment / tone analysis...")
    sent, s_model = analyze.analyze_sentiment(segments)
    drivers = sent.get("drivers", []) if isinstance(sent, dict) else []
    with db.connect() as conn:
        analyze.store_analysis(
            conn, call_id, "sentiment", sent, s_model, analyze.SENTIMENT_PROMPT_VERSION,
            quotes=[d.get("source_quote", "") for d in drivers],
        )
        conn.commit()
    print(f"      tone={sent.get('overall_tone')} score={sent.get('score')} "
          f"({len(drivers)} drivers cited)")

    # 4. Grounding report.
    print("[4/4] Verifying citations grounded to segments...")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT a.kind, COUNT(ci.id) total, "
            "SUM(CASE WHEN ci.segment_id IS NOT NULL THEN 1 ELSE 0 END) grounded "
            "FROM analyses a JOIN citations ci ON ci.analysis_id = a.id "
            "WHERE a.call_id = ? GROUP BY a.kind", (call_id,)
        ).fetchall()
    for r in rows:
        print(f"      {r['kind']:16} citations grounded {r['grounded']}/{r['total']}")

    print("\n--- SENTIMENT ---")
    print(json.dumps(sent, indent=2, ensure_ascii=False))
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
