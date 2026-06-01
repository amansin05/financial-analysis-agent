"""Phase 4+: backfill the last N earnings quarters per ticker (full pipeline each).

Usage:
    python -m scripts.backfill AAPL MSFT NVDA --quarters 4

Ingests each quarter's 8-K -> summary -> XBRL (period-matched) -> reconcile +
sentiment -> Chroma index -> graph entities. Idempotent per (company, quarter).
This is what makes theme-propagation-over-time visible in scripts.graph_query.
"""
from __future__ import annotations

import argparse
import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.ingest import pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill historical earnings quarters.")
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--quarters", type=int, default=4, help="quarters per ticker (default 4)")
    args = ap.parse_args()

    db.init_db()
    for tk in (t.upper() for t in args.tickers):
        print(f"== Backfilling {tk} (last {args.quarters} quarters) ==")
        infos = pipeline.backfill(tk, quarters=args.quarters)
        if not infos:
            print(f"   ! no earnings 8-Ks found for {tk}")
            continue
        for info in infos:
            tone = (info["sentiment"] or {}).get("overall_tone")
            v = info["verdict"]
            ok = sum(1 for x in v.values() if x["status"] == "verified")
            print(f"   call_id={info['call_id']:>3} report={info['report_date']} "
                  f"FY-label via report | segments={info['segments']:>3} "
                  f"indexed={info['indexed']:>3} topics={info['entities']['topics']} "
                  f"verified={ok}/{len(v)} tone={tone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
