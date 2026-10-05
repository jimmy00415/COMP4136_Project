import pytest
from review_challenge import audited_records


def test_audit_requires_explicit_complete_ai_verdicts():
    records = [{"case_id": "x", "arm": "system", "score": {"pass": False, "outcome": "error"}}]
    pending = {
        "reviewer_type": "AI",
        "entries": [
            {"case_id": "x", "arm": "system", "pass": None, "outcome": "pending", "reason": ""}
        ],
    }
    with pytest.raises(ValueError):
        audited_records(records, pending)
    with pytest.raises(ValueError):
        audited_records(records, {"reviewer_type": "human", "entries": []})


def test_audit_overrides_are_separate_and_cannot_change_raw_scores():
    raw = {"case_id": "x", "arm": "system", "score": {"pass": False, "outcome": "error"}}
    audit = {
        "reviewer_type": "AI",
        "entries": [
            {
                "case_id": "x",
                "arm": "system",
                "pass": True,
                "outcome": "correct_answer",
                "reason": "Year stated; optional follow-up is not an ambiguity request.",
            }
        ],
    }
    reviewed, overrides = audited_records([raw], audit)
    assert raw["score"]["pass"] is False
    assert reviewed[0]["score"]["pass"] is True
    assert len(overrides) == 1


def test_duplicate_or_missing_audit_entries_are_rejected():
    records = [{"case_id": "x", "arm": "system", "score": {"pass": False, "outcome": "error"}}]
    entry = {
        "case_id": "x",
        "arm": "system",
        "pass": False,
        "outcome": "error",
        "reason": "Wrong movie.",
    }
    with pytest.raises(ValueError):
        audited_records(records, {"reviewer_type": "AI", "entries": [entry, entry]})
    with pytest.raises(ValueError):
        audited_records(records, {"reviewer_type": "AI", "entries": []})
