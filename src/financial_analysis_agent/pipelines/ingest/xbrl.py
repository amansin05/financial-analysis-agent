"""Phase 2: exact financials from SEC XBRL (deterministic, never hallucinated).

Principle #1: numbers come from structured data. We pull a standard set of
income-statement concepts for the company's MOST RECENT fiscal quarter and store
them in `financials` with source='xbrl'.

The one real subtlety (proven on Apple's FQ2'26): a single fp='Q2' label maps to
TWO facts -- a 3-month quarter (111.2B) and a 6-month YTD (254.9B). The label
can't tell them apart; the period DURATION can. So we select by duration (~90
days = a quarter) and recency, not by the fp string.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from financial_analysis_agent.services.edgar import EdgarClient

# metric_key -> ordered candidate (taxonomy, tag) pairs; first with data wins.
STANDARD_METRICS: dict[str, list[tuple[str, str]]] = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ],
    "gross_profit": [("us-gaap", "GrossProfit")],
    "operating_income": [("us-gaap", "OperatingIncomeLoss")],
    "net_income": [("us-gaap", "NetIncomeLoss")],
    "eps_basic": [("us-gaap", "EarningsPerShareBasic")],
    "eps_diluted": [("us-gaap", "EarningsPerShareDiluted")],
}

# A fiscal quarter is ~13 weeks; accept a generous window, exclude YTD/annual.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100


def _parse(d: str) -> date:
    y, m, day = (int(x) for x in d.split("-"))
    return date(y, m, day)


def _duration_days(fact: dict) -> int | None:
    start, end = fact.get("start"), fact.get("end")
    if not start or not end:
        return None
    return (_parse(end) - _parse(start)).days


def is_quarterly(fact: dict) -> bool:
    d = _duration_days(fact)
    return d is not None and _QUARTER_MIN_DAYS <= d <= _QUARTER_MAX_DAYS


# An earnings 8-K is filed ~weeks after quarter end; if the nearest quarterly
# fact ends much further back than this, that quarter isn't separately tagged
# (the classic fiscal-Q4-only-in-the-10-K case) -- better to report nothing.
_MAX_REPORT_GAP_DAYS = 110


def select_latest_quarter(
    facts: list[dict], as_of: str | None = None, *, max_gap_days: int = _MAX_REPORT_GAP_DAYS
) -> dict | None:
    """Quarterly-duration fact from 10-Q/10-K (skips YTD/annual).

    Default: the most recent quarter. If `as_of` (an ISO date) is given, the most
    recent quarter whose period END is on/before that date AND within `max_gap_days`
    of it -- so a historical 8-K reconciles against the quarter it actually reported,
    and a missing standalone Q4 (reported only as an annual in the 10-K) yields None
    rather than silently matching a stale earlier quarter.
    """
    quarterly = [
        f for f in facts
        if is_quarterly(f) and f.get("form") in ("10-Q", "10-K", "10-K/A", "10-Q/A")
    ]
    if as_of:
        quarterly = [f for f in quarterly if f["end"] <= as_of]
    if not quarterly:
        return None
    best = max(quarterly, key=lambda f: f["end"])
    if as_of and (_parse(as_of) - _parse(best["end"])).days > max_gap_days:
        return None
    return best


def fetch_quarter_metrics(
    cik: str, client: EdgarClient | None = None, *, as_of: str | None = None
) -> dict[str, dict]:
    """Return {metric_key: fact} for a quarter across STANDARD_METRICS.

    `as_of` (ISO date, e.g. the 8-K report date) selects the quarter reported at
    that time; omit it for the latest quarter.
    """
    client = client or EdgarClient()
    out: dict[str, dict] = {}
    for key, candidates in STANDARD_METRICS.items():
        best: dict | None = None
        # Filers migrate tags over time (e.g. NVDA moved off
        # RevenueFromContractWithCustomer... to Revenues), leaving the old tag
        # populated with stale facts. So evaluate EVERY candidate tag and keep
        # the globally most-recent quarter, rather than the first tag with data.
        for taxonomy, tag in candidates:
            try:
                data = client.company_concept(cik, taxonomy, tag)
            except Exception:  # noqa: BLE001  (concept may not exist for this filer)
                continue
            # Pick whichever unit bucket holds the values (USD or USD/shares).
            units = data.get("units", {})
            facts = next(iter(units.values()), [])
            unit = next(iter(units.keys()), None)
            fact = select_latest_quarter(facts, as_of=as_of)
            if fact and (best is None or fact["end"] > best["end"]):
                best = {**fact, "taxonomy": taxonomy, "tag": tag, "unit": unit}
        if best:
            out[key] = best
    return out


def store_financials(
    conn: sqlite3.Connection, call_id: int, metrics: dict[str, dict]
) -> int:
    """Replace XBRL financials for a call (idempotent). Returns rows written."""
    conn.execute(
        "DELETE FROM financials WHERE call_id = ? AND source = 'xbrl'", (call_id,)
    )
    rows = [
        {
            "call_id": call_id,
            "metric": key,
            "value": f["val"],
            "unit": f.get("unit"),
            "period": f.get("end"),   # fiscal-period end date == the quarter
            "source": "xbrl",
        }
        for key, f in metrics.items()
    ]
    conn.executemany(
        "INSERT INTO financials (call_id, metric, value, unit, period, source) "
        "VALUES (:call_id, :metric, :value, :unit, :period, :source)",
        rows,
    )
    return len(rows)
