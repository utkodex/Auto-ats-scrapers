from __future__ import annotations

import json

import pytest

from forms_automation.db.question_store import QuestionStore


def test_old_file_without_head_fields_loads_and_saves_unchanged(answers_file):
    before = json.loads(answers_file.read_text("utf-8"))
    store = QuestionStore(answers_file)
    store.save()
    assert json.loads(answers_file.read_text("utf-8")) == before


def test_new_question_starts_without_head(qstore):
    qstore.record("Do you need sponsorship?", "select", ["Yes", "No"], job="https://x/jobs/9")
    entry = qstore.get("Do you need sponsorship?")
    assert entry["head"] is None
    assert entry["answer"] is None


def test_record_keeps_head_and_generated(qstore):
    qstore.set_head("Why do you want this internship?", "motivation", "llama")
    qstore.add_generated("Why do you want this internship?", "https://x/jobs/1", "text", "llama3.1:8b")
    qstore.record("Why do you want this internship?", "textarea", job="https://x/jobs/2")
    entry = QuestionStore(qstore.path).get("Why do you want this internship?")
    assert entry["head"] == "motivation"
    assert entry["head_source"] == "llama"
    assert len(entry["generated"]) == 1
    assert entry["times_seen"] == 2


def test_set_head_and_in_head(qstore):
    qstore.set_head("What is your gender?", "gender", "jev")
    qstore.set_head("what is your GENDER", "gender", "user")  # same question after norm()
    members = qstore.in_head("gender")
    assert [q["id"] for q in members] == [1]
    assert members[0]["head_source"] == "user"


def test_set_head_none_clears_source(qstore):
    qstore.set_head("What is your gender?", "gender", "jev")
    qstore.set_head("What is your gender?", None, "user")
    entry = qstore.get("What is your gender?")
    assert entry["head"] is None and entry["head_source"] is None


def test_set_head_unknown_label_raises(qstore):
    with pytest.raises(KeyError):
        qstore.set_head("Never asked before?", "x", "jev")


def test_add_generated_leaves_answer_alone(qstore, sentinels):
    qstore.add_generated("Why do you want this internship?", "https://x/jobs/1", "essay", "llama3.1:8b")
    entry = QuestionStore(qstore.path).get("Why do you want this internship?")
    assert entry["answer"] == sentinels[2]
    gen = entry["generated"][0]
    assert gen["job"] == "https://x/jobs/1" and gen["answer"] == "essay" and gen["model"] == "llama3.1:8b"
    assert gen["at"]


def test_rename_head(qstore):
    qstore.set_head("What is your gender?", "sex", "llama")
    qstore.set_head("Are you a veteran?", "sex", "llama")
    qstore.rename_head("sex", "gender")
    reloaded = QuestionStore(qstore.path)
    assert reloaded.in_head("sex") == []
    assert {q["id"] for q in reloaded.in_head("gender")} == {1, 2}


def test_rename_head_to_none_clears_source(qstore):
    qstore.set_head("What is your gender?", "sex", "llama")
    qstore.rename_head("sex", None)
    entry = qstore.get("What is your gender?")
    assert entry["head"] is None and entry["head_source"] is None
