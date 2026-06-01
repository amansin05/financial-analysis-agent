"""Filing helpers + HTML-to-text."""
from financial_analysis_agent.pipelines.ingest import filings
from financial_analysis_agent.utils import htmltext


def test_build_segments_strips_noise():
    paras = ["EX-99.1\n2\na8-kex991.htm\nEX-99.1", "Apple reports record results",
             "Revenue grew."]
    segs = filings.build_segments(paras, "a8-kex991.htm")
    texts = [s["text"] for s in segs]
    assert "Apple reports record results" in texts
    assert all("a8-kex991.htm" not in t for t in texts)   # filename noise dropped
    assert all(s["section"] == "prepared" and s["speaker_name"] is None for s in segs)


def test_derive_period():
    assert filings.derive_period("2026-03-28") == (2026, 1)
    assert filings.derive_period("2026-04-30") == (2026, 2)
    assert filings.derive_period(None) == (None, None)


def test_html_to_text():
    html = "<html><body><h1>Hello</h1><p>World</p><script>ignore()</script></body></html>"
    txt = htmltext.html_to_text(html)
    assert "Hello" in txt and "World" in txt
    assert "ignore" not in txt
