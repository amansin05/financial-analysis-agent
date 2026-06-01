"""Phase 4: extract topics/mentions/executives for stored calls -> edge tables.

Usage:
    python -m scripts.extract_entities          # all calls
    python -m scripts.extract_entities AAPL     # one ticker's latest call
"""
from __future__ import annotations

import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.ingest import entities


def _calls(conn, ticker: str | None):
    if ticker:
        return conn.execute(
            "SELECT c.id call_id, c.company_id, co.ticker FROM calls c "
            "JOIN companies co ON co.id = c.company_id WHERE co.ticker = ? "
            "ORDER BY c.call_date DESC, c.id DESC LIMIT 1", (ticker,),
        ).fetchall()
    return conn.execute(
        "SELECT c.id call_id, c.company_id, co.ticker FROM calls c "
        "JOIN companies co ON co.id = c.company_id ORDER BY c.id"
    ).fetchall()


def main() -> int:
    db.init_db()
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else None

    with db.connect() as conn:
        calls = _calls(conn, ticker)
    if not calls:
        print("  ! No matching calls. Run scripts.ingest_8k first.")
        return 2

    for c in calls:
        with db.connect() as conn:
            segs = [dict(r) for r in conn.execute(
                "SELECT speaker_name, speaker_role, section, text FROM segments "
                "WHERE call_id = ? ORDER BY seq", (c["call_id"],))]
        print(f"[{c['ticker']}] extracting entities from {len(segs)} segments...")
        extracted, model = entities.extract(segs)
        with db.connect() as conn:
            counts = entities.store(conn, c["call_id"], c["company_id"], extracted)
            conn.commit()
        print(f"   topics={counts['topics']} executives={counts['executives']} (model={model})")

    # Merge any topic-label variants onto canonical nodes (keeps the graph clean).
    with db.connect() as conn:
        n_merged = entities.canonicalize_topics(conn)
        conn.commit()
    if n_merged:
        print(f"canonicalized {n_merged} topic variant(s)")

    # Derive analyst coverage (empty on press-release data; works for transcripts).
    with db.connect() as conn:
        n_cov = entities.derive_analyst_coverage(conn)
        conn.commit()
    print(f"analyst_coverage edges derived: {n_cov} "
          f"{'(none -- press releases have no Analyst speakers)' if n_cov == 0 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
