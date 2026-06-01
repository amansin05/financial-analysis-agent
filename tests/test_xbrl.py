"""XBRL quarter selection: duration filter, recency, as_of window."""
from financial_analysis_agent.pipelines.ingest import xbrl


def _fact(start, end, val, form="10-Q"):
    return {"start": start, "end": end, "val": val, "form": form}


def test_is_quarterly():
    assert xbrl.is_quarterly(_fact("2026-01-01", "2026-03-31", 1))      # ~90d
    assert not xbrl.is_quarterly(_fact("2025-09-28", "2026-03-28", 1))  # ~6mo YTD
    assert not xbrl.is_quarterly(_fact("2024-09-29", "2025-09-27", 1))  # annual


def test_selects_quarter_not_ytd():
    facts = [
        _fact("2025-09-28", "2026-03-28", 254_940_000_000),  # 6-mo YTD
        _fact("2025-12-28", "2026-03-28", 111_184_000_000),  # 3-mo quarter
    ]
    chosen = xbrl.select_latest_quarter(facts)
    assert chosen["val"] == 111_184_000_000


def test_as_of_window_blocks_stale_quarter():
    facts = [_fact("2025-03-29", "2025-06-28", 94_000_000_000)]  # Jun quarter
    # A late-October report (FY-end) has no nearby quarter -> None, not stale Jun.
    assert xbrl.select_latest_quarter(facts, as_of="2025-10-30") is None
    # The July report is within the window -> selected.
    assert xbrl.select_latest_quarter(facts, as_of="2025-07-31")["val"] == 94_000_000_000
