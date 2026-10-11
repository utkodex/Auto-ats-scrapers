from __future__ import annotations

import json

import pytest

from forms_automation.db.head_store import NO_MATCHING_HEAD, HeadStore
from forms_automation.db.question_store import SKIP


def head(head_id, **extra):
    return {"id": head_id, "description": f"About {head_id}", "examples": [f"{head_id}?"], **extra}


def test_add_validates_ids(hstore):
    for bad in ["Gender", "1abc", "a", "has space", NO_MATCHING_HEAD, "x" * 42]:
        with pytest.raises(ValueError):
            hstore.add(head(bad))
    hstore.add(head("gender"))
    with pytest.raises(ValueError):
        hstore.add(head("gender"))


def test_add_defaults_and_atomic_save(hstore):
    entry = hstore.add(head("motivation", mode="generate"))
    assert entry["status"] == "pending"
    assert entry["auto_fill"] is False  # generate heads always confirm
    assert hstore.add(head("travel"))["auto_fill"] is True
    data = json.loads(hstore.path.read_text("utf-8"))
    assert data["version"] == 1
    assert [h["id"] for h in data["heads"]] == ["motivation", "travel"]
    assert not hstore.path.with_suffix(".json.tmp").exists()


def test_approved_and_pending_filters(hstore):
    hstore.add(head("gender"), status="approved")
    hstore.add(head("travel"))
    assert [h["id"] for h in hstore.approved()] == ["gender"]
    assert [h["id"] for h in hstore.pending()] == ["travel"]
    assert [h["id"] for h in HeadStore(hstore.path).approved()] == ["gender"]


def test_update_rename_and_validation(hstore):
    hstore.add(head("gender"))
    hstore.add(head("travel"))
    hstore.update("gender", id="gender_identity", status="approved")
    assert hstore.get("gender_identity")["status"] == "approved"
    with pytest.raises(ValueError):
        hstore.update("travel", id="gender_identity")
    with pytest.raises(ValueError):
        hstore.update("travel", mode="other")
    with pytest.raises(KeyError):
        hstore.update("nope", status="approved")


def test_merge_moves_questions_and_marks_source(hstore, qstore):
    hstore.add(head("sex"), status="approved")
    hstore.add(head("gender"), status="approved")
    qstore.set_head("What is your gender?", "sex", "llama")
    hstore.merge("sex", "gender", qstore)
    source = hstore.get("sex")
    assert source["status"] == "rejected" and source["merged_into"] == "gender"
    assert "sex?" in hstore.get("gender")["examples"]
    assert [q["id"] for q in qstore.in_head("gender")] == [1]
    assert [h["id"] for h in hstore.approved()] == ["gender"]


def test_answer_for_head_picks_newest_answer(hstore, qstore, sentinels):
    hstore.add(head("demo"), status="approved")
    qstore.set_head("What is your gender?", "demo", "llama")   # last_seen 10-01, SENTINEL-A1
    qstore.set_head("Are you a veteran?", "demo", "llama")     # last_seen 10-02, SENTINEL-A2
    assert hstore.answer_for_head("demo", qstore) == sentinels[1]


def test_answer_for_head_ignores_unanswered(hstore, qstore, sentinels):
    hstore.add(head("demo"), status="approved")
    qstore.set_head("What is your gender?", "demo", "llama")
    qstore.record("What's your gender identity?", "select")    # newest, but unanswered
    qstore.set_head("What's your gender identity?", "demo", "jev")
    assert hstore.answer_for_head("demo", qstore) == sentinels[0]


def test_answer_for_head_skip_when_newest_skipped(hstore, qstore):
    hstore.add(head("demo"), status="approved")
    qstore.set_head("What is your gender?", "demo", "llama")
    qstore.set_head("Are you a veteran?", "demo", "llama")
    qstore.set_answer("Are you a veteran?", SKIP)
    assert hstore.answer_for_head("demo", qstore) == SKIP


def test_answer_for_head_none_cases(hstore, qstore):
    hstore.add(head("motivation", mode="generate"), status="approved")
    qstore.set_head("Why do you want this internship?", "motivation", "llama")
    assert hstore.answer_for_head("motivation", qstore) is None
    hstore.add(head("empty"), status="approved")
    assert hstore.answer_for_head("empty", qstore) is None
    assert hstore.answer_for_head("missing", qstore) is None
