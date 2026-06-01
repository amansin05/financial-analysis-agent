"""FastAPI backend for the Earnings Call Agent UI.

Thin HTTP layer over the existing src/ modules -- nothing new is computed here,
it just exposes the SQLite system-of-record, the Q&A retrieval, and the NetworkX
graph to the React SPA (served at '/'). Run with:  python -m scripts.serve
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from financial_analysis_agent.utils import config, db
from financial_analysis_agent.pipelines.retrieve import graph, qa
from financial_analysis_agent.pipelines.ingest import pipeline, xbrl
from financial_analysis_agent.services.edgar import EdgarClient

app = FastAPI(title="Earnings Call Agent API")

UI_FILE = config.ROOT / "dev-ui" / "index.html"


# --------------------------- helpers ---------------------------

def _loads(s):
    try:
        return json.loads(s) if s else None
    except (json.JSONDecodeError, TypeError):
        return s


# --------------------------- companies / calls ---------------------------

@app.get("/api/health")
def health():
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) n FROM calls").fetchone()["n"]
    return {"ok": True, "calls": n}


@app.get("/api/companies")
def companies():
    with db.connect() as c:
        rows = c.execute(
            "SELECT co.ticker, co.name, co.sector, COUNT(ca.id) call_count "
            "FROM companies co LEFT JOIN calls ca ON ca.company_id = co.id "
            "GROUP BY co.id ORDER BY co.ticker"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/calls")
def calls(ticker: str | None = None):
    q = (
        "SELECT ca.id, co.ticker, co.name, ca.fiscal_year, ca.fiscal_quarter, "
        "ca.call_date, ca.source, "
        "(SELECT COUNT(*) FROM segments s WHERE s.call_id = ca.id) segments "
        "FROM calls ca JOIN companies co ON co.id = ca.company_id "
    )
    params: tuple = ()
    if ticker:
        q += "WHERE co.ticker = ? "
        params = (ticker.upper(),)
    q += "ORDER BY ca.call_date DESC, co.ticker"
    with db.connect() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/calls/{call_id}")
def call_detail(call_id: int):
    with db.connect() as c:
        call = c.execute(
            "SELECT ca.id, co.ticker, co.name, co.sector, ca.fiscal_year, "
            "ca.fiscal_quarter, ca.call_date, ca.source "
            "FROM calls ca JOIN companies co ON co.id = ca.company_id WHERE ca.id = ?",
            (call_id,),
        ).fetchone()
        if not call:
            return {"error": "not found"}
        analyses = {
            r["kind"]: {"content": _loads(r["content"]), "model": r["model"],
                        "prompt_version": r["prompt_version"], "created_at": r["created_at"]}
            for r in c.execute(
                "SELECT kind, content, model, prompt_version, created_at FROM analyses "
                "WHERE call_id = ?", (call_id,))
        }
        fins = [dict(r) for r in c.execute(
            "SELECT metric, value, unit, period, source FROM financials "
            "WHERE call_id = ? ORDER BY source, metric", (call_id,))]
        topics = [dict(r) for r in c.execute(
            "SELECT t.label, m.sentiment, m.segment_id FROM mentions m "
            "JOIN topics t ON t.id = m.target_topic_id "
            "WHERE m.call_id = ? AND m.target_type = 'topic' ORDER BY t.label", (call_id,))]
    return {
        "call": dict(call),
        "summary": analyses.get("summary"),
        "sentiment": analyses.get("sentiment"),
        "reconciliation": analyses.get("reconciliation"),
        "financials": {"xbrl": [f for f in fins if f["source"] == "xbrl"],
                       "spoken": [f for f in fins if f["source"] == "spoken"]},
        "topics": topics,
    }


# --------------------------- Q&A ---------------------------

class AskBody(BaseModel):
    question: str
    ticker: str | None = None
    k: int = 8


@app.post("/api/ask")
def ask(body: AskBody):
    res = qa.answer(body.question, ticker=body.ticker, n_results=body.k, store=False)
    sources = []
    for sid in res.get("citations", []):
        s = res["segments"].get(sid, {})
        sources.append({
            "segment_id": sid,
            "ticker": s.get("ticker"),
            "quarter": f"FY{s.get('fiscal_year')}Q{s.get('fiscal_quarter')}",
            "who": s.get("speaker_name") or s.get("speaker_role") or s.get("section"),
            "text": (s.get("text") or "").strip()[:400],
        })
    return {"question": res["question"], "answer": res["answer"],
            "grounded": res["grounded"], "sources": sources}


# --------------------------- graph ---------------------------

@app.get("/api/graph")
def graph_data():
    with db.connect() as c:
        G = graph.build_graph(c)
    nodes = [{"id": n, "kind": d.get("kind"),
              "label": d.get("label") or d.get("name") or n}
             for n, d in G.nodes(data=True)]
    edges = [{"source": u, "target": v, "kind": d.get("kind")}
             for u, v, d in G.edges(data=True)]
    return {
        "nodes": nodes, "edges": edges,
        "pervasiveness": graph.topic_pervasiveness(G, top_n=30),
        "centrality": graph.topic_centrality(G, top_n=12),
        "communities": graph.communities(G),
        "stats": graph.stats(G),
    }


@app.get("/api/theme/{label}")
def theme(label: str):
    with db.connect() as c:
        G = graph.build_graph(c)
    return {"theme": label, "propagation": graph.theme_propagation(G, label)}


@app.get("/api/shared")
def shared(a: str, b: str):
    with db.connect() as c:
        G = graph.build_graph(c)
    return {"a": a.upper(), "b": b.upper(), "shared": graph.shared_topics(G, a, b)}


# --------------------------- any-company financials lookup ---------------------------

def _is_ingested(cik: str) -> bool:
    with db.connect() as c:
        return c.execute("SELECT 1 FROM companies WHERE cik = ?", (cik,)).fetchone() is not None


@app.get("/api/lookup")
def lookup(q: str | None = None, cik: str | None = None):
    """Live financials for ANY SEC-filing company (no pre-ingestion needed).

    Resolves a ticker/name to candidates, then pulls the latest quarter's XBRL
    financials straight from SEC EDGAR + computes margins.
    """
    edgar = EdgarClient()
    matches = [] if cik else edgar.search_company(q or "", limit=8)
    sel_cik = cik or (matches[0]["cik"] if matches else None)
    if not sel_cik:
        return {"query": q, "matches": [], "selected": None,
                "metrics": [], "margins": {}, "note": "no SEC company matched"}

    # Identify the selected company's name/ticker.
    selected = next((m for m in matches if m["cik"] == sel_cik), None)
    if selected is None:
        hit = next((r for r in edgar._company_tickers()
                    if str(r["cik_str"]).zfill(10) == str(sel_cik).zfill(10)), None)
        selected = ({"ticker": hit["ticker"], "cik": sel_cik, "name": hit["title"]}
                    if hit else {"ticker": None, "cik": sel_cik, "name": None})

    metrics = xbrl.fetch_quarter_metrics(sel_cik, edgar)
    out_metrics, period = [], None
    for k, f in metrics.items():
        out_metrics.append({"metric": k, "value": f["val"], "unit": f.get("unit"),
                            "period": f.get("end")})
        period = f.get("end")
    rev = metrics.get("revenue", {}).get("val")
    margins = {}
    if rev:
        for base in ("gross_profit", "operating_income", "net_income"):
            if base in metrics:
                margins[base.replace("_", " ") + " margin"] = round(metrics[base]["val"] / rev * 100, 1)
    return {"query": q, "matches": matches, "selected": selected, "period": period,
            "metrics": out_metrics, "margins": margins,
            "ingested": _is_ingested(str(sel_cik).zfill(10))}


class IngestBody(BaseModel):
    ticker: str


@app.post("/api/ingest")
def ingest_company(body: IngestBody):
    """Pull a company into the FULL pipeline (summary + financials + Q&A + graph)."""
    try:
        info = pipeline.run_full(body.ticker)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not info:
        return {"ok": False, "error": "No earnings 8-K (item 2.02) found for this ticker."}
    v = info["verdict"]
    return {"ok": True, "ticker": info["ticker"], "call_id": info["call_id"],
            "segments": info["segments"], "indexed": info["indexed"],
            "topics": info["entities"]["topics"],
            "verified": sum(1 for x in v.values() if x["status"] == "verified"),
            "metrics": len(v),
            "tone": (info["sentiment"] or {}).get("overall_tone")}


# --------------------------- monitor ---------------------------

@app.get("/api/alerts")
def alerts():
    """Negative-topic alerts across all stored calls, with the source segment text."""
    out = []
    with db.connect() as c:
        for r in c.execute(
            "SELECT co.ticker, t.label, m.sentiment, m.segment_id, "
            "ca.fiscal_year, ca.fiscal_quarter, s.text segment_text, "
            "s.speaker_name, s.speaker_role, s.section FROM mentions m "
            "JOIN topics t ON t.id = m.target_topic_id "
            "JOIN calls ca ON ca.id = m.call_id JOIN companies co ON co.id = ca.company_id "
            "LEFT JOIN segments s ON s.id = m.segment_id "
            "WHERE m.target_type = 'topic' AND m.sentiment < 0 ORDER BY m.sentiment"):
            who = r["speaker_name"] or r["speaker_role"] or r["section"]
            out.append({"type": "neg_topic", "ticker": r["ticker"], "topic": r["label"],
                        "sentiment": r["sentiment"], "segment_id": r["segment_id"],
                        "quarter": f"FY{r['fiscal_year']}Q{r['fiscal_quarter']}",
                        "who": who, "segment_text": r["segment_text"]})
    return out


# --------------------------- static UI ---------------------------

@app.get("/")
def index():
    return FileResponse(UI_FILE)


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Console-script entry point (faa-serve)."""
    import uvicorn
    print(f"Financial Analysis Agent UI -> http://{host}:{port}")
    uvicorn.run("financial_analysis_agent.server.app:app", host=host, port=port)
