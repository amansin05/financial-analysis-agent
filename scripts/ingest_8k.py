"""Phase 1 (EDGAR path): latest earnings 8-K press release -> stored summary.

Usage:
    python -m scripts.ingest_8k AAPL
    python -m scripts.ingest_8k MSFT --fy 2025 --fq 3
    python -m scripts.ingest_8k NVDA --no-summary

Flow: resolve company (EDGAR CIK + Finnhub profile) -> fetch latest 8-K with
item 2.02 -> pull EX-99.1 -> HTML->text -> paragraphs as segments -> upsert
company/call/segments -> Groq structured summary. Fully free, no API key.
"""
from __future__ import annotations

import argparse
import json
import sys

from financial_analysis_agent.utils import db, htmltext
from financial_analysis_agent.pipelines.ingest import filings, store, summarize, triage
from financial_analysis_agent.services.edgar import EdgarClient
from financial_analysis_agent.services.finnhub import FinnhubClient


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest the latest earnings 8-K (Phase 1).")
    ap.add_argument("ticker")
    ap.add_argument("--fy", type=int, help="override fiscal year label")
    ap.add_argument("--fq", type=int, choices=[1, 2, 3, 4], help="override fiscal quarter")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()
    ticker = args.ticker.upper()

    db.init_db()
    print(f"== Ingesting latest earnings 8-K for {ticker} ==")

    # 1. Fetch the press release from EDGAR.
    print("[1/5] Locating latest 8-K (item 2.02) + EX-99.1 ...")
    edgar = EdgarClient()
    rel = edgar.earnings_release(ticker)
    if not rel:
        print("  ! No earnings 8-K / press-release exhibit found. Aborting.")
        return 2
    print(f"      accession={rel['accession']} filed={rel['filing_date']} "
          f"report={rel['report_date']}")
    print(f"      exhibit={rel['exhibit']} (EX-99.1={rel['is_exhibit_99']})")
    print(f"      {rel['url']}")

    # 2. Triage (HTML text source -> text route).
    route = triage.route_text()
    print(f"[2/5] Triage: {route.summary}")

    # 3. HTML -> text -> paragraph segments.
    text = htmltext.html_to_text(rel["html"])
    paragraphs = htmltext.to_paragraphs(text, min_len=2)
    segments = filings.build_segments(paragraphs, rel["exhibit"])
    print(f"[3/5] Extracted {len(text)} chars -> {len(segments)} paragraph segments")

    # 4. Resolve company + persist.
    print("[4/5] Resolving company + storing...")
    name = sector = None
    try:
        prof = FinnhubClient().company_profile(ticker)
        name, sector = prof.get("name"), prof.get("finnhubIndustry")
    except Exception as e:  # noqa: BLE001
        print(f"      ! Finnhub profile failed ({type(e).__name__}); continuing.")

    fy = args.fy or filings.derive_period(rel["report_date"])[0]
    fq = args.fq or filings.derive_period(rel["report_date"])[1]
    print(f"      {name or ticker}  CIK={rel['cik']}  period(label)=FY{fy} Q{fq}")

    with db.connect() as conn:
        company_id = store.get_or_create_company(
            conn, ticker=ticker, name=name, sector=sector, cik=rel["cik"]
        )
        call_id = store.upsert_call(
            conn, company_id=company_id, fiscal_year=fy, fiscal_quarter=fq,
            call_date=rel["report_date"], source="edgar_8k",
        )
        n = store.replace_segments(conn, call_id, segments)
        conn.commit()
    print(f"      company_id={company_id} call_id={call_id} segments_stored={n}")

    # 5. Summarize.
    if args.no_summary:
        print("[5/5] Skipped summary (--no-summary).\n\nDone.")
        return 0

    print("[5/5] Summarizing via Groq...")
    summary, model = summarize.summarize_segments(
        segments, doc_kind="earnings press release (8-K Exhibit 99.1)"
    )
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
