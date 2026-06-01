"""Phase 4 graph: build a NetworkX graph FROM SQLite edges, on demand.

The graph is derived, never stored: we read the edge tables, construct an
in-memory networkx.Graph, run multi-hop / centrality / community analysis, and
discard it. SQLite remains the single source of truth (results can be written
back if desired). This is what makes questions like "which themes are most
pervasive across companies" or "trace a theme's spread" natural -- they're graph
traversals, awkward as SQL.

Node kinds: 'company' (id=ticker), 'topic' (id='topic:<label>'), 'person'.
Edges:  company —discusses— topic   (attrs: sentiment, quarter, segment_id)
        person  —exec_at—  company   (attrs: role)
"""
from __future__ import annotations

import sqlite3

import networkx as nx


def _topic_node(label: str) -> str:
    return f"topic:{label}"


def build_graph(conn: sqlite3.Connection) -> nx.Graph:
    """Construct the in-memory graph from SQLite edge tables."""
    G = nx.Graph()

    # Company nodes.
    for r in conn.execute("SELECT ticker, name, sector FROM companies"):
        if r["ticker"]:
            G.add_node(r["ticker"], kind="company", name=r["name"], sector=r["sector"])

    # company —discusses— topic  (from topic mentions, with quarter + grounding).
    for r in conn.execute(
        "SELECT co.ticker, t.label, m.sentiment, m.segment_id, "
        "       c.fiscal_year, c.fiscal_quarter "
        "FROM mentions m JOIN topics t ON t.id = m.target_topic_id "
        "JOIN calls c ON c.id = m.call_id JOIN companies co ON co.id = c.company_id "
        "WHERE m.target_type = 'topic' AND co.ticker IS NOT NULL"
    ):
        tnode = _topic_node(r["label"])
        if not G.has_node(tnode):
            G.add_node(tnode, kind="topic", label=r["label"])
        quarter = f"FY{r['fiscal_year']}Q{r['fiscal_quarter']}"
        # A company may touch a topic in multiple quarters -> collect them.
        if G.has_edge(r["ticker"], tnode):
            G[r["ticker"]][tnode]["quarters"].add(quarter)
        else:
            G.add_edge(r["ticker"], tnode, kind="discusses",
                       sentiment=r["sentiment"], segment_id=r["segment_id"],
                       quarters={quarter})

    # person —exec_at— company.
    for r in conn.execute(
        "SELECT p.full_name, et.role, co.ticker FROM executive_tenure et "
        "JOIN people p ON p.id = et.person_id JOIN companies co ON co.id = et.company_id "
        "WHERE co.ticker IS NOT NULL"
    ):
        G.add_node(r["full_name"], kind="person")
        G.add_edge(r["full_name"], r["ticker"], kind="exec_at", role=r["role"])

    return G


# ----------------------------- queries -----------------------------

def _companies(G: nx.Graph) -> list[str]:
    return [n for n, d in G.nodes(data=True) if d.get("kind") == "company"]


def _topics(G: nx.Graph) -> list[str]:
    return [n for n, d in G.nodes(data=True) if d.get("kind") == "topic"]


def theme_propagation(G: nx.Graph, topic_label: str) -> list[dict]:
    """Which companies discuss a theme, with sentiment + quarters (the 'spread')."""
    tnode = _topic_node(topic_label.strip().lower())
    if not G.has_node(tnode):
        return []
    out = []
    for company in G.neighbors(tnode):
        if G.nodes[company].get("kind") != "company":
            continue
        e = G[company][tnode]
        out.append({
            "company": company,
            "sentiment": e.get("sentiment"),
            "quarters": sorted(e.get("quarters", [])),
            "segment_id": e.get("segment_id"),
        })
    return sorted(out, key=lambda x: (x["sentiment"] is None, -(x["sentiment"] or 0)))


def topic_pervasiveness(G: nx.Graph, top_n: int = 10) -> list[dict]:
    """Rank topics by how many distinct companies discuss them (cross-company reach)."""
    rows = []
    for t in _topics(G):
        companies = [n for n in G.neighbors(t) if G.nodes[n].get("kind") == "company"]
        sents = [G[c][t].get("sentiment") for c in companies
                 if G[c][t].get("sentiment") is not None]
        rows.append({
            "topic": G.nodes[t]["label"],
            "company_count": len(companies),
            "companies": sorted(companies),
            "avg_sentiment": round(sum(sents) / len(sents), 2) if sents else None,
        })
    rows.sort(key=lambda r: (-r["company_count"], r["topic"]))
    return rows[:top_n]


def shared_topics(G: nx.Graph, ticker_a: str, ticker_b: str) -> list[str]:
    """Multi-hop A —topic— B: themes both companies discuss."""
    a, b = ticker_a.upper(), ticker_b.upper()
    if not (G.has_node(a) and G.has_node(b)):
        return []
    ta = {n for n in G.neighbors(a) if G.nodes[n].get("kind") == "topic"}
    tb = {n for n in G.neighbors(b) if G.nodes[n].get("kind") == "topic"}
    return sorted(G.nodes[t]["label"] for t in (ta & tb))


def topic_centrality(G: nx.Graph, top_n: int = 8) -> list[dict]:
    """Degree centrality over the full graph, reported for topic nodes."""
    cent = nx.degree_centrality(G)
    topics = [(G.nodes[t]["label"], round(cent[t], 4)) for t in _topics(G)]
    topics.sort(key=lambda x: -x[1])
    return [{"topic": t, "centrality": c} for t, c in topics[:top_n]]


def communities(G: nx.Graph) -> list[list[str]]:
    """Greedy-modularity communities -> clusters of companies + their shared themes."""
    comms = nx.algorithms.community.greedy_modularity_communities(G)
    result = []
    for c in comms:
        labelled = []
        for n in c:
            kind = G.nodes[n].get("kind")
            labelled.append(G.nodes[n]["label"] if kind == "topic" else n)
        result.append(sorted(labelled))
    return result


def stats(G: nx.Graph) -> dict:
    kinds: dict[str, int] = {}
    for _, d in G.nodes(data=True):
        kinds[d.get("kind", "?")] = kinds.get(d.get("kind", "?"), 0) + 1
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(), "by_kind": kinds}
