"""Phase 4: build the NetworkX graph from SQLite edges and run multi-hop analysis.

Usage:
    python -m scripts.graph_query                       # full report
    python -m scripts.graph_query --theme "artificial intelligence"
    python -m scripts.graph_query --shared AAPL NVDA
"""
from __future__ import annotations

import argparse

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.retrieve import graph


def main() -> int:
    ap = argparse.ArgumentParser(description="Graph analysis over earnings edges.")
    ap.add_argument("--theme", help="trace which companies discuss a theme")
    ap.add_argument("--shared", nargs=2, metavar=("TICKER_A", "TICKER_B"),
                    help="themes two companies share (multi-hop)")
    args = ap.parse_args()

    db.init_db()
    with db.connect() as conn:
        G = graph.build_graph(conn)

    s = graph.stats(G)
    print(f"Graph built from SQLite edges: {s['nodes']} nodes, {s['edges']} edges "
          f"({s['by_kind']})")

    if args.theme:
        print(f"\n=== Theme propagation: '{args.theme}' ===")
        rows = graph.theme_propagation(G, args.theme)
        if not rows:
            print("   (theme not found)")
        for r in rows:
            print(f"   {r['company']:6} sentiment={r['sentiment']}  "
                  f"quarters={','.join(r['quarters'])}  (seg {r['segment_id']})")
        return 0

    if args.shared:
        a, b = args.shared
        print(f"\n=== Shared themes: {a.upper()} <-> {b.upper()} (multi-hop A—topic—B) ===")
        for t in graph.shared_topics(G, a, b):
            print(f"   • {t}")
        return 0

    # Default: full report.
    print("\n=== Topic pervasiveness (how many companies discuss each theme) ===")
    for r in graph.topic_pervasiveness(G):
        print(f"   {r['company_count']}  {r['topic']:24} "
              f"avg_sent={r['avg_sentiment']}  [{', '.join(r['companies'])}]")

    print("\n=== Topic centrality (degree) ===")
    for r in graph.topic_centrality(G):
        print(f"   {r['centrality']:.4f}  {r['topic']}")

    print("\n=== Communities (greedy modularity) ===")
    for i, c in enumerate(graph.communities(G), 1):
        print(f"   cluster {i}: {', '.join(c)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
