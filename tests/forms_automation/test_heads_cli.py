from __future__ import annotations

from forms_automation.ai import head_creator, llm
from forms_automation.common import gui
from forms_automation.db import heads as cli
from forms_automation.db.head_store import HeadStore
from forms_automation.db.question_store import QuestionStore


def fake_chat(system, user, schema, **kw):
    import json
    items = json.loads(user)
    return {"heads": [{"id": f"topic_{q['id']}", "description": "d", "question_ids": [q["id"]]} for q in items]}


def build(monkeypatch, answers_file, tmp_path):
    monkeypatch.setattr(llm, "available", lambda *a: True)
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    heads_file = tmp_path / "heads.json"
    code = cli.main(["--answers", str(answers_file), "--heads", str(heads_file), "build", "--no-gui"])
    return code, heads_file


def test_build_no_gui_creates_pending_heads_and_backup(monkeypatch, answers_file, tmp_path):
    code, heads_file = build(monkeypatch, answers_file, tmp_path)
    assert code == 0
    store = HeadStore(heads_file)
    assert [h["status"] for h in store.heads] == ["pending"] * 3
    assert all(q["head"] for q in QuestionStore(answers_file).questions)
    assert list(tmp_path.glob("answers.json.bak-*"))


def test_build_needs_ollama(monkeypatch, answers_file, tmp_path, capsys):
    monkeypatch.setattr(llm, "available", lambda *a: False)
    assert cli.main(["--answers", str(answers_file), "--heads", str(tmp_path / "h.json"), "build"]) == 1
    assert "Ollama" in capsys.readouterr().out


def test_list_never_prints_answers(monkeypatch, answers_file, tmp_path, capsys, sentinels):
    _, heads_file = build(monkeypatch, answers_file, tmp_path)
    capsys.readouterr()
    cli.main(["--answers", str(answers_file), "--heads", str(heads_file), "list"])
    out = capsys.readouterr().out
    assert "topic_1" in out and "0 approved, 3 pending" in out
    assert not any(s in out for s in sentinels)


def test_review_applies_each_decision(monkeypatch, answers_file, tmp_path):
    _, heads_file = build(monkeypatch, answers_file, tmp_path)
    decisions = iter([
        (gui.APPROVE, {"id": "gender", "description": "Gender", "examples": ["What is your gender?"],
                       "mode": "reuse", "auto_fill": True}),
        (gui.REJECT, None),
        (None, None),
    ])
    monkeypatch.setattr(gui, "review_head", lambda *a, **k: next(decisions))
    monkeypatch.setattr(gui, "notice", lambda *a, **k: "Close")
    heads, questions = HeadStore(heads_file), QuestionStore(answers_file)
    counts = cli.review(heads, questions)
    assert counts == {"approved": 1, "merged": 0, "rejected": 1, "pending": 1}
    heads, questions = HeadStore(heads_file), QuestionStore(answers_file)
    assert heads.get("gender")["status"] == "approved"
    assert questions.get("What is your gender?")["head"] == "gender"   # renamed with the head
    assert heads.get("topic_2")["status"] == "rejected"
    assert questions.get("Are you a veteran?")["head"] is None
    assert heads.get("topic_3")["status"] == "pending"


def test_review_merge(monkeypatch, answers_file, tmp_path):
    _, heads_file = build(monkeypatch, answers_file, tmp_path)
    heads, questions = HeadStore(heads_file), QuestionStore(answers_file)
    heads.update("topic_1", status="approved")
    assert cli.apply_review(heads, questions, "topic_2", gui.MERGE, "topic_1") == "merged"
    assert {q["id"] for q in questions.in_head("topic_1")} == {1, 2}
    assert heads.get("topic_2")["merged_into"] == "topic_1"


def test_review_terminal_edit(monkeypatch, answers_file, tmp_path):
    _, heads_file = build(monkeypatch, answers_file, tmp_path)
    answers = iter(["e", "Bad Id", "veteran_status", "", "", "n",   # first head: edit
                    "r",                                             # second: reject
                    ""])                                             # third: later
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    heads, questions = HeadStore(heads_file), QuestionStore(answers_file)
    counts = cli.review(heads, questions, terminal=True)
    assert counts["approved"] == 1 and counts["rejected"] == 1 and counts["pending"] == 1
    edited = HeadStore(heads_file).get("veteran_status")
    assert edited["auto_fill"] is False and edited["status"] == "approved"


def test_prompt_choice_reaches_head_creator(monkeypatch, answers_file, tmp_path):
    seen = {}
    real = head_creator.build_initial_heads

    def spy(*a, **k):
        seen["prompt"] = k.get("prompt")
        return real(*a, **k)
    monkeypatch.setattr(head_creator, "build_initial_heads", spy)
    monkeypatch.setattr(llm, "available", lambda *a: True)
    monkeypatch.setattr(llm, "chat_json", fake_chat)
    cli.main(["--answers", str(answers_file), "--heads", str(tmp_path / "h.json"),
              "build", "--no-gui", "--prompt", "tested"])
    assert seen["prompt"] == "tested"
