from __future__ import annotations

import json

from forms_automation.ai import head_creator as hc
from forms_automation.db.question_store import QuestionStore


class FakeChat:
    """Stands in for llm.chat_json: returns queued replies and records each call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, system, user, schema, **kw):
        self.calls.append({"system": system, "user": user, "schema": schema})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply(json.loads(user.split("\n\nYour previous")[0])) if callable(reply) else reply


def singletons(items):
    return {"heads": [{"id": f"topic_{q['id']}", "description": "d", "question_ids": [q["id"]]}
                      for q in items]}


# ---- validate -------------------------------------------------------------------------

def test_validate_clean_result():
    raw = {"heads": [{"id": "gender", "description": "Gender", "question_ids": [1]},
                     {"id": "veteran_status", "description": "Vet", "question_ids": [2, 3]}]}
    heads, errors = hc.validate(raw, {1, 2, 3}, set())
    assert errors == []
    assert [(h["id"], h["question_ids"], h["join"]) for h in heads] == [
        ("gender", [1], False), ("veteran_status", [2, 3], False)]


def test_validate_missing_duplicate_and_unknown_ids():
    raw = {"heads": [{"id": "a_head", "description": "", "question_ids": [1, 9]},
                     {"id": "b_head", "description": "", "question_ids": [1]}]}
    heads, errors = hc.validate(raw, {1, 2}, set())
    assert "question id 9 doesn't exist" in errors
    assert "question id 1 is in more than one head" in errors
    assert "question id 2 is missing" in errors
    assert [h["id"] for h in heads] == ["a_head"]  # b_head lost its only question


def test_validate_slugifies_and_suffixes_clashes():
    raw = {"heads": [{"id": "Start Date", "description": "", "question_ids": [1]},
                     {"id": "start-date", "description": "", "question_ids": [2]},
                     {"id": "2nd thing", "description": "", "question_ids": [3]}]}
    heads, _ = hc.validate(raw, {1, 2, 3}, set())
    assert [h["id"] for h in heads] == ["start_date", "start_date_2", "h_2nd_thing"]


def test_validate_joins_existing_head():
    raw = {"heads": [{"id": "gender", "description": "", "question_ids": [1]}]}
    heads, _ = hc.validate(raw, {1}, {"gender"})
    assert heads[0]["join"] is True


def test_validate_flags_company_names():
    raw = {"heads": [{"id": "datacor_affiliation", "description": "", "question_ids": [8]}]}
    heads, _ = hc.validate(raw, {8}, set(), companies={"datacor"})
    assert heads[0]["flag"] == "rename"


def test_company_words_from_job_and_capitals():
    words = hc.company_words([{"question": "Have you worked for Datacor or a subsidiary previously?",
                               "last_job": "https://job-boards.greenhouse.io/reddit/jobs/1"}])
    assert {"datacor", "reddit"} <= words
    assert "have" not in words


# ---- mode rules ---------------------------------------------------------------------------

def test_mode_for_rules():
    assert hc.mode_for([{"question": "Why us?", "type": "textarea"}]) == "generate"
    assert hc.mode_for([{"question": "If you selected other, please include more details below.",
                         "type": "text"}]) == "generate"
    assert hc.mode_for([{"question": "Please explain your answer", "type": "text"}]) == "generate"
    assert hc.mode_for([{"question": "If you are a referral, please include who referred you.",
                         "type": "text"}]) == "reuse"
    assert hc.mode_for([{"question": "Are you a veteran?", "type": "select"}]) == "reuse"


# ---- prompt contents ----------------------------------------------------------------------

def test_prompt_sends_no_answers(qstore, sentinels):
    items = hc.question_items(qstore.questions)
    assert all(set(i) == {"id", "question", "type"} for i in items)
    assert not any(s in json.dumps(items) for s in sentinels)


def test_existing_heads_block_only_when_given():
    assert "Existing heads" not in hc.system_prompt([])
    prompt = hc.system_prompt([{"id": "gender", "description": "Gender identity"}])
    assert "- gender: Gender identity" in prompt
    assert hc.system_prompt([], "tested").count("mode:") == 1


# ---- build / retry / batching -----------------------------------------------------------

def test_build_initial_heads_creates_pending_heads(qstore, hstore):
    chat = FakeChat(singletons)
    result = hc.build_initial_heads(qstore, hstore, chat=chat)
    assert result.created == ["topic_1", "topic_2", "topic_3"] and not result.needs_review
    assert [h["status"] for h in hstore.heads] == ["pending"] * 3
    assert hstore.get("topic_3")["mode"] == "generate"  # textarea
    assert hstore.get("topic_3")["auto_fill"] is False
    reloaded = QuestionStore(qstore.path)
    assert [(q["head"], q["head_source"]) for q in reloaded.questions] == [
        ("topic_1", "llama"), ("topic_2", "llama"), ("topic_3", "llama")]
    assert len(chat.calls) == 1


def test_retry_once_with_errors_then_success(qstore, hstore):
    broken = {"heads": [{"id": "gender", "description": "", "question_ids": [1, 2]}]}
    chat = FakeChat(broken, singletons)
    result = hc.build_initial_heads(qstore, hstore, chat=chat)
    assert len(chat.calls) == 2
    assert "question id 3 is missing" in chat.calls[1]["user"]
    assert result.errors == [] and not result.needs_review


def test_unplaced_question_gets_own_head(qstore, hstore):
    broken = {"heads": [{"id": "gender", "description": "", "question_ids": [1, 2]}]}
    result = hc.build_initial_heads(qstore, hstore, chat=FakeChat(broken))
    assert result.needs_review
    assert result.created[-1] == "why_do_you_want_this"
    assert hstore.get("why_do_you_want_this")["flag"] == "unplaced"
    assert all(q["head"] for q in QuestionStore(qstore.path).questions)


def test_batches_of_25_and_later_batch_sees_earlier_heads(tmp_path, hstore):
    store = QuestionStore(tmp_path / "answers.json")
    for i in range(30):
        store.record(f"Question number {i}?", "text")
    chat = FakeChat(singletons)
    hc.build_initial_heads(store, hstore, chat=chat)
    sizes = [len(json.loads(c["user"])) for c in chat.calls]
    assert sizes == [25, 5]
    assert "Existing heads" not in chat.calls[0]["system"]
    assert "- topic_1: d" in chat.calls[1]["system"]


def test_build_skips_already_classified(qstore, hstore):
    qstore.set_head("What is your gender?", "gender", "user")
    chat = FakeChat(singletons)
    hc.build_initial_heads(qstore, hstore, chat=chat)
    assert [i["id"] for i in json.loads(chat.calls[0]["user"])] == [2, 3]


def test_build_joins_existing_head(qstore, hstore):
    hstore.add({"id": "gender", "description": "Gender", "examples": ["Sex?"]}, status="approved")
    reply = {"heads": [{"id": "gender", "description": "", "question_ids": [1]},
                       {"id": "veteran", "description": "", "question_ids": [2]},
                       {"id": "motivation", "description": "", "question_ids": [3]}]}
    result = hc.build_initial_heads(qstore, hstore, chat=FakeChat(reply))
    assert result.joined == ["gender"]
    assert hstore.get("gender")["examples"] == ["Sex?", "What is your gender?"]


# ---- propose_head -----------------------------------------------------------------------

def test_propose_head_draft_and_join(hstore):
    q = {"question": "Do you require visa sponsorship?", "type": "select"}
    draft = hc.propose_head(q, hstore, chat=FakeChat(
        {"heads": [{"id": "visa_sponsorship", "description": "Visa", "question_ids": [1]}]}))
    assert draft == {"id": "visa_sponsorship", "description": "Visa",
                     "examples": ["Do you require visa sponsorship?"], "mode": "reuse", "join": False}
    assert hstore.heads == []  # not saved
    hstore.add({"id": "visa_sponsorship", "description": "Visa"}, status="approved")
    joined = hc.propose_head(q, hstore, chat=FakeChat(
        {"heads": [{"id": "visa_sponsorship", "description": "", "question_ids": [1]}]}))
    assert joined == {"id": "visa_sponsorship", "join": True}
