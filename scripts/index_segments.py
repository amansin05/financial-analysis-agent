"""Phase 3: embed stored segments into ChromaDB (idempotent upsert).

Usage:
    python -m scripts.index_segments            # index all calls
    python -m scripts.index_segments AAPL       # index one ticker's latest call

First run downloads the all-MiniLM-L6-v2 model (~90MB), then caches it.
"""
from __future__ import annotations

import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.retrieve import vectorstore


def main() -> int:
    db.init_db()
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else None

    print("Loading embedding model (first run downloads ~90MB)...")
    with db.connect() as conn:
        if ticker:
            row = conn.execute(
                "SELECT c.id FROM calls c JOIN companies co ON co.id = c.company_id "
                "WHERE co.ticker = ? ORDER BY c.call_date DESC, c.id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if not row:
                print(f"  ! No stored call for {ticker}.")
                return 2
            n = vectorstore.index_call(conn, row["id"])
            print(f"Indexed {n} segments for {ticker} (call_id={row['id']}).")
        else:
            result = vectorstore.index_all(conn)
            total = sum(result.values())
            print(f"Indexed {total} segments across {len(result)} calls:")
            for cid, cnt in result.items():
                print(f"   call_id={cid}: {cnt}")

    print(f"Chroma collection now holds {vectorstore.count()} vectors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
