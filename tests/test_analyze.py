"""Reconciliation: value normalization, match/mismatch, headline verdict."""
from financial_analysis_agent.pipelines.ingest import analyze


def test_normalize_value():
    assert analyze.normalize_value({"value": 111.2, "scale": "billion"}) == 111.2e9
    assert analyze.normalize_value({"value": 2.01, "scale": "none"}) == 2.01


def test_reconcile_match_and_mismatch():
    figures = [
        {"metric": "revenue", "value": 111.2, "scale": "billion", "unit": "USD",
         "period_hint": "quarter", "source_quote": "revenue of $111.2 billion"},
        {"metric": "revenue", "value": 54.5, "scale": "billion", "unit": "USD",
         "period_hint": "quarter", "source_quote": "a segment line"},
    ]
    xbrl_metrics = {"revenue": {"val": 111_184_000_000.0, "unit": "USD"}}
    recon = analyze.reconcile(figures, xbrl_metrics)
    statuses = {round(r["extracted_value"]): r["status"] for r in recon}
    assert statuses[111_200_000_000] == "match"      # within tolerance (rounding)
    assert statuses[54_500_000_000] == "mismatch"     # segment value != total


def test_percent_metric_skipped_for_dollar_field():
    figs = [{"metric": "gross_profit", "value": 67.6, "scale": "none", "unit": "percent",
             "period_hint": "quarter", "source_quote": "gross margin 67.6%"}]
    xbrl_metrics = {"gross_profit": {"val": 56_000_000_000.0, "unit": "USD"}}
    assert analyze.reconcile(figs, xbrl_metrics) == []  # margin %, not the $ figure


def test_headline_verdict():
    figures = [{"metric": "revenue", "value": 111.18, "scale": "billion", "unit": "USD",
                "period_hint": "quarter", "source_quote": "q"}]
    xbrl_metrics = {"revenue": {"val": 111_184_000_000.0, "unit": "USD"},
                    "eps_diluted": {"val": 2.01, "unit": "USD/shares"}}
    recon = analyze.reconcile(figures, xbrl_metrics)
    verdict = analyze.headline_verdict(recon, xbrl_metrics)
    assert verdict["revenue"]["status"] == "verified"
    assert verdict["eps_diluted"]["status"] == "no_extraction"
