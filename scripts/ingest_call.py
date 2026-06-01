"""Phase 1 end-to-end: one earnings call -> stored, speaker-segmented summary.

Usage:
    python -m scripts.ingest_call MSFT 2023 1
    python -m scripts.ingest_call AAPL 2024 2 --no-summary

Flow: resolve company (EDGAR CIK + Finnhub profile) -> fetch transcript
(API Ninjas) -> triage (text route) -> parse segments -> upsert company/call/
segments -> Groq structured summary -> store in analyses. Idempotent per call.
"""
from __future__ import annotations

import argparse
import json
import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.ingest import store, summarize, triage
from financial_analysis_agent.services.apininjas import APINinjasClient
from financial_analysis_agent.services.edgar import EdgarClient
from financial_analysis_agent.services.finnhub import FinnhubClient


def resolve_company(ticker: str) -> dict:
    """Best-effort enrich a ticker with CIK (EDGAR) + name/sector (Finnhub)."""
    info = {"ticker": ticker, "cik": None, "name": None, "sector": None}
    try:
        info["cik"] = EdgarClient().ticker_to_cik(ticker)
    except Exception as e:  # noqa: BLE001
        print(f"  ! EDGAR CIK lookup failed ({type(e).__name__}); continuing.")
    try:
        prof = FinnhubClient().company_profile(ticker)
        info["name"] = prof.get("name")
        info["sector"] = prof.get("finnhubIndustry")
    except Exception as e:  # noqa: BLE001
        print(f"  ! Finnhub profile failed ({type(e).__name__}); continuing.")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest one earnings call (Phase 1).")
    ap.add_argument("ticker")
    ap.add_argument("year", type=int)
    ap.add_argument("quarter", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--no-summary", action="store_true", help="skip the LLM summary")
    args = ap.parse_args()
    ticker = args.ticker.upper()

    db.init_db()

    print(f"== Ingesting {ticker} FY{args.year} Q{args.quarter} ==")

    # 1. Fetch transcript.
    print("[1/5] Fetching transcript (API Ninjas)...")
    data = APINinjasClient().earnings_transcript(ticker, args.year, args.quarter)
    split = data.get("transcript_split") or []
    if not split:
        print("  ! No transcript_split returned for that period. Aborting.")
        return 2
    print(f"      got {len(split)} raw speaker turns")

    # 2. Triage (text source -> 'text' route; PDFs would go through inventory_pdf).
    route = triage.route_text()
    print(f"[2/5] Triage: {route.summary}")

    # 3. Parse into segments.
    segments = store.parse_segments(split)
    n_prepared = sum(1 for s in segments if s["section"] == "prepared")
    n_qa = sum(1 for s in segments if s["section"] == "qa")
    n_analyst = sum(1 for s in segments if s["speaker_role"] == "Analyst")
    print(
        f"[3/5] Parsed {len(segments)} segments "
        f"(prepared={n_prepared}, qa={n_qa}, analyst turns={n_analyst})"
    )

    # 4. Persist company / call / segments.
    print("[4/5] Resolving company + storing...")
    info = resolve_company(ticker)
    print(f"      {info['name'] or ticker}  CIK={info['cik']}  sector={info['sector']}")
    with db.connect() as conn:
        company_id = store.get_or_create_company(
            conn,
            ticker=ticker,
            name=info["name"],
            sector=info["sector"],
            cik=info["cik"],
        )
        call_id = store.upsert_call(
            conn,
            company_id=company_id,
            fiscal_year=args.year,
            fiscal_quarter=args.quarter,
            call_date=data.get("date"),
            source="api_ninjas",
        )
        n = store.replace_segments(conn, call_id, segments)
        conn.commit()
    print(f"      company_id={company_id} call_id={call_id} segments_stored={n}")

    # 5. Summarize.
    if args.no_summary:
        print("[5/5] Skipped summary (--no-summary).")
        print("\nDone.")
        return 0

    print("[5/5] Summarizing via Groq...")
    summary, model = summarize.summarize_segments(segments)
    with db.connect() as conn:
        analysis_id = summarize.store_summary(conn, call_id, summary, model)
        conn.commit()
    print(f"      stored analysis_id={analysis_id} (model={model}, "
          f"prompt={summarize.PROMPT_VERSION})")

    print("\n--- SUMMARY ---")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
