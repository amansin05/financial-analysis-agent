"""Phase 3 Q&A CLI: ask a grounded question over indexed earnings segments.

Usage:
    python -m scripts.ask "what did Apple say about Services revenue?" --ticker AAPL
    python -m scripts.ask "who returned the most cash to shareholders?"
    python -m scripts.ask "how did margins trend?" --ticker MSFT --no-store

Run scripts.index_segments first so the Chroma collection is populated.
"""
from __future__ import annotations

import argparse
import sys

from financial_analysis_agent.pipelines.retrieve import qa


def main() -> int:
    ap = argparse.ArgumentParser(description="Grounded Q&A over earnings segments.")
    ap.add_argument("question")
    ap.add_argument("--ticker", help="restrict retrieval to one company")
    ap.add_argument("-k", type=int, default=8, help="segments to retrieve (default 8)")
    ap.add_argument("--no-store", action="store_true", help="don't persist the answer")
    args = ap.parse_args()

    res = qa.answer(
        args.question, ticker=args.ticker, n_results=args.k, store=not args.no_store
    )

    print(f"\nQ: {res['question']}")
    print(f"\nA: {res['answer']}")
    print(f"\ngrounded={res['grounded']}  cited segments: {res['citations']}")
    if res["citations"]:
        print("\n--- sources ---")
        for sid in res["citations"]:
            s = res["segments"].get(sid, {})
            tag = f"{s.get('ticker')} FY{s.get('fiscal_year')}Q{s.get('fiscal_quarter')}"
            who = s.get("speaker_name") or s.get("speaker_role") or s.get("section") or ""
            text = (s.get("text") or "").strip().replace("\n", " ")
            print(f"  [seg {sid}] ({tag}{', ' + who if who else ''}): {text[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
