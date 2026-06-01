"""Segment parsing + role inference + prepared/Q&A boundary."""
from financial_analysis_agent.pipelines.ingest import store


def test_roles_and_qa_boundary():
    split = [
        {"speaker": "Operator", "text": "Welcome to the Q1 earnings call."},
        {"speaker": "Jane Doe - Chief Executive Officer", "text": "Revenue grew this quarter."},
        {"speaker": "John Smith - Chief Financial Officer", "text": "Margins expanded."},
        {"speaker": "Operator", "text": "We will now begin the question-and-answer session. First question."},
        {"speaker": "Mark Lee", "text": "Can you talk about cloud demand?"},
        {"speaker": "Jane Doe - Chief Executive Officer", "text": "Demand remains strong."},
    ]
    segs = store.parse_segments(split)
    assert segs[0]["speaker_role"] == "Operator"
    assert segs[1]["speaker_role"] == "CEO"
    assert segs[2]["speaker_role"] == "CFO"
    # Prepared section through the operator hand-off; Q&A starts after.
    assert segs[1]["section"] == "prepared"
    assert segs[3]["section"] == "prepared"
    assert segs[4]["section"] == "qa"
    assert segs[4]["speaker_role"] == "Analyst"


def test_empty_text_skipped():
    segs = store.parse_segments([{"speaker": "X", "text": "  "}, {"speaker": "Y", "text": "real"}])
    assert len(segs) == 1
    assert segs[0]["text"] == "real"
