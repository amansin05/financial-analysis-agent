"""Phase 4: the full per-ticker pipeline, composed from the module functions.

run_full(ticker) does, for one company: ingest the latest 8-K -> summary ->
XBRL financials -> reconcile + sentiment -> embed into Chroma -> extract graph
entities. The monitor calls this across a watchlist. No CLI parsing here -- this
is the programmatic entry point the per-phase scripts also map onto.
"""
from __future__ import annotations

from financial_analysis_agent.utils import db, htmltext
from financial_analysis_agent.pipelines.ingest import (analyze, entities, filings,
                                                       store, summarize, xbrl)
from financial_analysis_agent.pipelines.retrieve import vectorstore
from financial_analysis_agent.services.edgar import EdgarClient
from financial_analysis_agent.services.finnhub import FinnhubClient


def ingest_8k(ticker: str, *, rel: dict | None = None, do_summary: bool = True) -> dict | None:
    """Store an earnings 8-K press release (latest, or a pre-fetched `rel`).

    Pass `rel` (from EdgarClient.earnings_releases) to ingest a specific historical
    quarter; omit it to fetch and ingest the latest.
    """
    edgar = EdgarClient()
    if rel is None:
        rel = edgar.earnings_release(ticker)
    if not rel:
        return None
    text = htmltext.html_to_text(rel["html"])
    paragraphs = htmltext.to_paragraphs(text, min_len=2)
    segments = filings.build_segments(paragraphs, rel["exhibit"])

    name = sector = None
    try:
        prof = FinnhubClient().company_profile(ticker)
        name, sector = prof.get("name"), prof.get("finnhubIndustry")
    except Exception:  # noqa: BLE001
        pass

    fy, fq = filings.derive_period(rel["report_date"])
    with db.connect() as conn:
        company_id = store.get_or_create_company(
            conn, ticker=ticker, name=name, sector=sector, cik=rel["cik"])
        call_id = store.upsert_call(
            conn, company_id=company_id, fiscal_year=fy, fiscal_quarter=fq,
            call_date=rel["report_date"], source="edgar_8k")
        store.replace_segments(conn, call_id, segments)
        conn.commit()

    if do_summary:
        summary, model = summarize.summarize_segments(
            segments, doc_kind="earnings press release (8-K Exhibit 99.1)")
        with db.connect() as conn:
            summarize.store_summary(conn, call_id, summary, model)
            conn.commit()

    return {"call_id": call_id, "company_id": company_id, "cik": rel["cik"],
            "name": name, "ticker": ticker.upper(), "report_date": rel["report_date"],
            "segments": len(segments)}


def _segments(call_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT seq, speaker_name, speaker_role, section, text FROM segments "
            "WHERE call_id = ? ORDER BY seq", (call_id,))]


def analyze_financials(cik: str, call_id: int, *, as_of: str | None = None) -> dict:
    """XBRL truth + reconciled spoken figures + sentiment (all stored/grounded).

    `as_of` (the call's report date) selects the right historical quarter's XBRL.
    """
    metrics = xbrl.fetch_quarter_metrics(cik, EdgarClient(), as_of=as_of)
    segs = _segments(call_id)
    figures, ex_model = analyze.extract_figures(segs)
    recon = analyze.reconcile(figures, metrics)
    verdict = analyze.headline_verdict(recon, metrics)
    sent, s_model = analyze.analyze_sentiment(segs)
    drivers = sent.get("drivers", []) if isinstance(sent, dict) else []
    with db.connect() as conn:
        xbrl.store_financials(conn, call_id, metrics)
        analyze.store_spoken_financials(conn, call_id, recon)
        analyze.store_analysis(
            conn, call_id, "reconciliation",
            {"headline_verdict": verdict, "all_figures": recon},
            ex_model, analyze.EXTRACT_PROMPT_VERSION,
            quotes=[r["source_quote"] for r in recon])
        analyze.store_analysis(
            conn, call_id, "sentiment", sent, s_model, analyze.SENTIMENT_PROMPT_VERSION,
            quotes=[d.get("source_quote", "") for d in drivers])
        conn.commit()
    return {"metrics": metrics, "verdict": verdict, "sentiment": sent}


def index(call_id: int) -> int:
    with db.connect() as conn:
        return vectorstore.index_call(conn, call_id)


def extract_entities(call_id: int, company_id: int) -> dict:
    segs = _segments(call_id)
    extracted, _ = entities.extract(segs)
    with db.connect() as conn:
        counts = entities.store(conn, call_id, company_id, extracted)
        conn.commit()
    return counts


def run_full(ticker: str, *, rel: dict | None = None) -> dict | None:
    """Ingest + analyze + index + extract for one call. Returns a summary dict."""
    info = ingest_8k(ticker, rel=rel)
    if not info:
        return None
    fin = analyze_financials(info["cik"], info["call_id"], as_of=info["report_date"])
    info["indexed"] = index(info["call_id"])
    info["entities"] = extract_entities(info["call_id"], info["company_id"])
    info["sentiment"] = fin["sentiment"]
    info["verdict"] = fin["verdict"]
    return info


def backfill(ticker: str, quarters: int = 4) -> list[dict]:
    """Ingest the last N earnings quarters for a ticker through the full pipeline."""
    releases = EdgarClient().earnings_releases(ticker, limit=quarters)
    results = []
    for rel in releases:
        info = run_full(ticker, rel=rel)
        if info:
            results.append(info)
    return results
