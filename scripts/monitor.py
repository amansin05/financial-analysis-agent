"""Phase 4: multi-company monitoring + earnings-calendar trigger + alerting.

Usage:
    python -m scripts.monitor AAPL MSFT NVDA                 # scan + alert (uses stored data)
    python -m scripts.monitor AAPL MSFT --refresh            # re-run full pipeline first
    python -m scripts.monitor AAPL MSFT NVDA --calendar 30   # upcoming earnings (next 30 days)
    python -m scripts.monitor NVDA --watch "ai capex,guidance cut,weak demand"

Alerts fire on: a watched phrase appearing in the call, a call-level sentiment
score below the floor, or any topic mention with negative sentiment. Every alert
is grounded to the segment it came from.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from financial_analysis_agent.utils import db
from financial_analysis_agent.pipelines.ingest import pipeline
from financial_analysis_agent.services.finnhub import FinnhubClient

DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA"]
DEFAULT_WATCH_TERMS = ["ai capex", "guidance cut", "layoffs", "weak demand",
                       "headwind", "decline", "miss", "restructuring"]
SENTIMENT_FLOOR = 0.0


def _latest_call(conn, ticker: str):
    return conn.execute(
        "SELECT c.id call_id, co.id company_id, co.name FROM calls c "
        "JOIN companies co ON co.id = c.company_id WHERE co.ticker = ? "
        "ORDER BY c.call_date DESC, c.id DESC LIMIT 1", (ticker,)).fetchone()


def show_calendar(tickers: list[str], days: int) -> None:
    """Earnings-calendar trigger: upcoming report dates for the watchlist."""
    # Date math is intentionally avoided here (no clock dependency); the user
    # passes a window via Finnhub's from/to. We query a generous forward window.
    from datetime import date, timedelta
    today = date.today()
    frm, to = today.isoformat(), (today + timedelta(days=days)).isoformat()
    print(f"=== Earnings calendar {frm} .. {to} (watchlist) ===")
    try:
        data = FinnhubClient().earnings_calendar(frm, to)
    except Exception as e:  # noqa: BLE001
        print(f"   ! calendar fetch failed: {type(e).__name__}: {e}")
        return
    want = {t.upper() for t in tickers}
    rows = [e for e in data.get("earningsCalendar", []) if e.get("symbol") in want]
    if not rows:
        print("   (no scheduled earnings for the watchlist in this window)")
    for e in sorted(rows, key=lambda x: x.get("date", "")):
        print(f"   {e['date']}  {e['symbol']:6} epsEstimate={e.get('epsEstimate')} "
              f"revenueEstimate={e.get('revenueEstimate')}")


def scan_alerts(tickers: list[str], watch_terms: list[str]) -> list[dict]:
    alerts: list[dict] = []
    with db.connect() as conn:
        for tk in tickers:
            call = _latest_call(conn, tk)
            if not call:
                print(f"   ! {tk}: no stored call (use --refresh to ingest)")
                continue
            cid = call["call_id"]

            # 1. Watched-phrase scan with WORD BOUNDARIES (so "miss" doesn't match
            # "Commission"/"permission"). Fetch once, regex per term.
            seg_rows = conn.execute(
                "SELECT id, text FROM segments WHERE call_id = ?", (cid,)).fetchall()
            patterns = [(t, re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE))
                        for t in watch_terms]
            for r in seg_rows:
                text = r["text"] or ""
                for term, pat in patterns:
                    m = pat.search(text)
                    if m:
                        start = max(0, m.start() - 40)
                        alerts.append({"ticker": tk, "type": "phrase", "term": term,
                                       "segment_id": r["id"],
                                       "context": text[start:start + 120].replace("\n", " ")})

            # 2. Call-level sentiment floor.
            row = conn.execute(
                "SELECT content FROM analyses WHERE call_id = ? AND kind = 'sentiment' "
                "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            if row:
                try:
                    score = json.loads(row["content"]).get("score")
                    if score is not None and score < SENTIMENT_FLOOR:
                        alerts.append({"ticker": tk, "type": "sentiment", "score": score})
                except (json.JSONDecodeError, AttributeError):
                    pass

            # 3. Negative topic sentiment (grounded mention).
            for r in conn.execute(
                "SELECT t.label, m.sentiment, m.segment_id FROM mentions m "
                "JOIN topics t ON t.id = m.target_topic_id "
                "WHERE m.call_id = ? AND m.target_type = 'topic' AND m.sentiment < 0",
                (cid,)):
                alerts.append({"ticker": tk, "type": "neg_topic", "topic": r["label"],
                               "sentiment": r["sentiment"], "segment_id": r["segment_id"]})
    return alerts


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-company earnings monitor.")
    ap.add_argument("tickers", nargs="*", default=DEFAULT_WATCHLIST,
                    help="watchlist (default AAPL MSFT NVDA)")
    ap.add_argument("--refresh", action="store_true", help="run full pipeline per ticker first")
    ap.add_argument("--calendar", type=int, metavar="DAYS",
                    help="show upcoming earnings within N days")
    ap.add_argument("--watch", help="comma-separated watch phrases (overrides default)")
    args = ap.parse_args()

    tickers = [t.upper() for t in (args.tickers or DEFAULT_WATCHLIST)]
    watch_terms = ([w.strip() for w in args.watch.split(",")] if args.watch
                   else DEFAULT_WATCH_TERMS)

    db.init_db()
    print(f"Monitoring watchlist: {', '.join(tickers)}")

    if args.calendar:
        show_calendar(tickers, args.calendar)
        print()

    if args.refresh:
        print("=== Refreshing (full pipeline per ticker) ===")
        for tk in tickers:
            info = pipeline.run_full(tk)
            if info:
                tone = (info["sentiment"] or {}).get("overall_tone")
                v = info["verdict"]
                ok = sum(1 for x in v.values() if x["status"] == "verified")
                print(f"   {tk}: call_id={info['call_id']} segments={info['segments']} "
                      f"indexed={info['indexed']} topics={info['entities']['topics']} "
                      f"verified={ok}/{len(v)} tone={tone}")
            else:
                print(f"   {tk}: no earnings 8-K found")
        print()

    print(f"=== Alerts (watch terms: {', '.join(watch_terms)}) ===")
    alerts = scan_alerts(tickers, watch_terms)
    if not alerts:
        print("   no alerts.")
    for a in alerts:
        if a["type"] == "phrase":
            print(f"   [PHRASE]   {a['ticker']:5} '{a['term']}' (seg {a['segment_id']}): {a['context']}")
        elif a["type"] == "sentiment":
            print(f"   [SENTIMENT]{a['ticker']:5} call score {a['score']} below floor {SENTIMENT_FLOOR}")
        elif a["type"] == "neg_topic":
            print(f"   [NEG TOPIC]{a['ticker']:5} {a['topic']} sentiment={a['sentiment']} (seg {a['segment_id']})")
    print(f"\n{len(alerts)} alert(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
