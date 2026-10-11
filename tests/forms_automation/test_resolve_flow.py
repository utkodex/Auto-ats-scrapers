"""Filler.resolve with fake Jev / LLM / windows: which source answers each question."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forms_automation.ai import answer_writer, head_creator, jev, llm
from forms_automation.automation import resolver
from forms_automation.automation.answers import Answers
from forms_automation.common import gui
from forms_automation.db.head_store import NO_MATCHING_HEAD
from forms_automation.db.question_store import SKIP, QuestionStore
from forms_automation.greenhouse.filler import Filler

URL = "https://job-boards.greenhouse.io/acme/jobs/1"


class Calls:
    """Records calls to the faked functions."""

    def __init__(self):
        self.classify: list[str] = []
        self.ask: list[str] = []
        self.confirm: list[str] = []
        self.review_head: list[dict] = []
        self.write: list[dict] = []


@pytest.fixture
def env(monkeypatch, qstore, hstore):
    calls = Calls()
    state = SimpleNamespace(classification=None, confirm=(gui.CONFIRM, None), review=(None, None),
                            typed="typed answer", draft=None, review_answer=(gui.UPLOAD, None),
                            llm_up=True)
    jev.clear_cache()

    def classify_head(question, kind, options, heads, chat=None):
        calls.classify.append(question)
        return state.classification

    def ask_question(label, options=None, required=False, multi=False, kind="text", job=None):
        calls.ask.append(label)
        return (gui.SAVE, state.typed) if state.typed is not None else (gui.BLANK, None)

    def confirm_head(label, job, head, confidence, reason, preview, heads):
        calls.confirm.append(label)
        action, chosen = state.confirm
        return action, chosen or head["id"]

    def review_head(draft, members, heads, flag=None, progress=""):
        calls.review_head.append(draft)
        return state.review

    def write_answer(question, kind, job, profile, prev=None, examples=None, **kw):
        calls.write.append({"question": question, "kind": kind, "prev": prev, "examples": examples})
        return {"answer": state.draft or f"written for {job['company']}", "warnings": []}

    def review_letter(heading, text, warnings, labels=None):
        action, edited = state.review_answer
        return action, edited or text

    monkeypatch.setattr(jev, "classify_head", classify_head)
    monkeypatch.setattr(gui, "ask_question", ask_question)
    monkeypatch.setattr(gui, "confirm_head", confirm_head)
    monkeypatch.setattr(gui, "review_head", review_head)
    monkeypatch.setattr(gui, "review_letter", review_letter)
    monkeypatch.setattr(answer_writer, "write_answer", write_answer)
    monkeypatch.setattr(answer_writer, "job_for", lambda url: {"company": "Acme", "role": "Intern",
                                                              "description": "", "url": url})
    monkeypatch.setattr(llm, "available", lambda *a: state.llm_up)
    monkeypatch.setattr(head_creator, "propose_head", lambda q, heads, **k: {
        "id": "visa_sponsorship", "description": "Needs visa sponsorship", "examples": [q["question"]],
        "mode": "reuse", "join": False})

    # Approved heads: veteran (with Q2), motivation (generate, with Q3).
    hstore.add({"id": "veteran", "description": "Military service", "examples": ["Are you a veteran?"]},
               status="approved")
    hstore.add({"id": "motivation", "description": "Why this internship", "mode": "generate",
                "examples": ["Why do you want this internship?"]}, status="approved")
    qstore.set_head("Are you a veteran?", "veteran", "llama")
    qstore.set_head("Why do you want this internship?", "motivation", "llama")

    def filler(interactive=True, classify=True, threshold=0.75, rules=None, use_gui=True):
        answers = Answers({"rules": rules or []}, qstore, interactive=interactive, use_gui=use_gui,
                             heads=hstore, classify=classify, threshold=threshold)
        return Filler(SimpleNamespace(url=URL), answers)

    return SimpleNamespace(calls=calls, state=state, filler=filler, q=qstore, h=hstore)


def found(head, confidence=0.9, verified=True):
    return jev.Classification(head, confidence, "same fact", verified and head != NO_MATCHING_HEAD)


def test_unverified_pick_asks_even_when_confident(env, sentinels):
    env.state.classification = found("veteran", 1.0, verified=False)
    assert env.filler().resolve("q", "Military background?", True, "select") == sentinels[1]
    assert env.calls.confirm == ["Military background?"]


def test_exact_hit_makes_no_llm_call(env, sentinels):
    assert env.filler().resolve("q", "Are you a veteran?", True, "select") == sentinels[1]
    assert env.calls.classify == [] and env.calls.ask == []


def test_rules_come_before_jev(env):
    f = env.filler(rules=[{"match": "sponsorship", "answer": "No"}])
    assert f.resolve("q", "Will you need sponsorship?", True, "select") == "No"
    assert env.calls.classify == []


def test_confident_match_fills_from_head(env, sentinels):
    env.state.classification = found("veteran", 0.9)
    f = env.filler()
    assert f.resolve("q", "Have you served in the military?", True, "select") == sentinels[1]
    entry = QuestionStore(env.q.path).get("Have you served in the military?")
    assert (entry["head"], entry["head_source"], entry["answer"]) == ("veteran", "jev", None)
    assert env.calls.confirm == [] and env.calls.ask == []
    assert f.stats["filled from a topic"] == 1
    # next time it's an exact hit on a question with a head: no LLM call
    env.calls.classify.clear()
    assert env.filler().resolve("q", "Have you served in the military?", True, "select") == sentinels[1]
    assert env.calls.classify == []


def test_low_confidence_asks_to_confirm(env, sentinels):
    env.state.classification = found("veteran", 0.5)
    assert env.filler().resolve("q", "Military background?", True, "select") == sentinels[1]
    assert env.calls.confirm == ["Military background?"]
    assert env.q.get("Military background?")["head_source"] == "user"


def test_auto_fill_off_always_confirms(env):
    env.h.update("veteran", auto_fill=False)
    env.state.classification = found("veteran", 0.99)
    env.filler().resolve("q", "Military background?", True, "select")
    assert env.calls.confirm == ["Military background?"]


def test_confirm_declined_falls_back_to_asking(env):
    env.state.classification = found("veteran", 0.5)
    env.state.confirm = (gui.NO_CLASSIFY, None)
    assert env.filler().resolve("q", "Military background?", True, "select") == "typed answer"
    assert env.calls.ask == ["Military background?"]
    assert env.q.get("Military background?")["head"] is None


def test_no_ask_leaves_low_confidence_blank(env):
    env.state.classification = found("veteran", 0.5)
    f = env.filler(interactive=False)
    assert f.resolve("q", "Military background?", True, "select") is None
    assert f.unsure == ["Military background?"]
    assert env.calls.confirm == [] and env.q.get("Military background?")["head"] is None


def test_new_topic_is_reviewed_then_answered_once(env):
    env.state.classification = found(NO_MATCHING_HEAD, 0.0)
    env.state.review = (gui.APPROVE, {"id": "visa_sponsorship", "description": "Needs a visa",
                                      "examples": ["Do you need a visa?"], "mode": "reuse",
                                      "auto_fill": True, "join": False})
    f = env.filler()
    assert f.resolve("q", "Do you need a visa?", True, "select") == "typed answer"
    assert env.h.get("visa_sponsorship")["status"] == "approved"
    entry = env.q.get("Do you need a visa?")
    assert entry["head"] == "visa_sponsorship" and entry["answer"] == "typed answer"
    assert f.stats["new topics"] == 1
    # a reworded question later reuses that answer
    env.state.classification = found("visa_sponsorship", 0.9)
    assert env.filler().resolve("q", "Will you require visa sponsorship?", True, "select") == "typed answer"


def test_new_topic_decided_later_stays_pending(env):
    env.state.classification = found(NO_MATCHING_HEAD, 0.0)
    env.state.review = (None, None)
    env.filler().resolve("q", "Do you need a visa?", True, "select")
    assert env.h.get("visa_sponsorship")["status"] == "pending"
    assert env.q.get("Do you need a visa?")["head"] == "visa_sponsorship"


def test_generate_head_never_copies_answer(env, sentinels):
    f = env.filler()
    text = f.resolve("q", "Why do you want this internship?", True, "textarea")
    assert text == "written for Acme" and text != sentinels[2]
    gen = QuestionStore(env.q.path).get("Why do you want this internship?")["generated"]
    assert [g["answer"] for g in gen] == ["written for Acme"] and gen[0]["job"] == URL
    assert env.calls.classify == []


def test_generate_rewrite_then_blank(env):
    env.state.review_answer = (gui.SKIP_LETTER, None)
    assert env.filler().resolve("q", "Why do you want this internship?", True, "textarea") is None
    assert "generated" not in env.q.get("Why do you want this internship?")
    assert env.calls.ask == []


def test_if_other_passes_previous_field(env):
    env.h.add({"id": "other_details", "description": "Details for an Other choice", "mode": "generate",
               "examples": ["If you selected other, please include more details below."]}, status="approved")
    env.q.record("If you selected other, please include more details below.", "text")
    env.q.set_head("If you selected other, please include more details below.", "other_details", "llama")
    env.q.record("How did you hear about us?", "select")
    env.q.set_answer("How did you hear about us?", "Other")
    f = env.filler()
    f.resolve("a", "How did you hear about us?", False, "select")
    f.resolve("b", "If you selected other, please include more details below.", False, "text")
    assert env.calls.write[-1]["prev"] == ("How did you hear about us?", "Other")


def test_ollama_down_gives_todays_flow(env, capsys):
    env.state.llm_up = False
    env.state.classification = found("veteran", 0.9)
    f = env.filler()
    assert f.resolve("q", "Military background?", True, "select") == "typed answer"
    f.resolve("q2", "Another new question?", True, "text")
    assert env.calls.classify == []
    assert capsys.readouterr().out.count("Ollama isn't running") == 1


def test_no_classify_ignores_heads(env):
    env.q.record("Served in the armed forces?", "select")
    env.q.set_head("Served in the armed forces?", "veteran", "user")
    f = env.filler(classify=False)
    assert f.resolve("q", "Served in the armed forces?", True, "select") == "typed answer"
    assert env.calls.classify == []
    assert f.resolve("q", "Why do you want this internship?", True, "textarea") != "written for Acme"


def test_head_answer_skip_means_blank(env):
    env.q.set_answer("Are you a veteran?", SKIP)
    env.state.classification = found("veteran", 0.9)
    assert env.filler().resolve("q", "Military background?", True, "select") is None
    assert env.calls.ask == []


def test_no_approved_heads_skips_classifier(env):
    for h in env.h.approved():
        env.h.update(h["id"], status="pending")
    env.filler().resolve("q", "Military background?", True, "select")
    assert env.calls.classify == [] and env.calls.ask == ["Military background?"]


def test_report_shows_counts_not_answers(env, capsys, sentinels):
    env.state.classification = found("veteran", 0.9)
    f = env.filler()
    f.resolve("q", "Have you served in the military?", True, "select")
    f.resolve("g", "Why do you want this internship?", True, "textarea")
    capsys.readouterr()
    f.report()
    out = capsys.readouterr().out
    assert "1 filled from a topic" in out and "1 written answers" in out
    assert not any(s in out for s in sentinels) and "written for Acme" not in out


def test_terminal_confirm(env, monkeypatch, sentinels):
    env.state.classification = found("veteran", 0.5)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert env.filler(use_gui=False).resolve("q", "Military background?", True, "select") == sentinels[1]
    assert env.q.get("Military background?")["head_source"] == "user"


# ---- option fitting (fill_native_select / fill_choice_group) ---------------------------

class FakeOptions:
    def __init__(self, texts):
        self.texts = texts

    def all_inner_texts(self):
        return list(self.texts)

    def nth(self, i):
        return SimpleNamespace(inner_text=lambda: self.texts[i])


class FakeSelect:
    def __init__(self, label, options):
        self.label, self.options, self.selected = label, FakeOptions(options), None

    def get_attribute(self, name):
        return {"id": "q_select", "aria-required": "true"}.get(name)

    def evaluate(self, js):
        return self.label

    def is_visible(self):
        return True

    def locator(self, selector):
        return self.options

    def input_value(self):
        return ""

    def select_option(self, label):
        self.selected = label


class FakeBox:
    def __init__(self, text):
        self.text, self.checked = text, False

    def evaluate(self, js):
        return self.text

    def get_attribute(self, name):
        return {"type": "checkbox", "value": self.text}.get(name)

    def is_checked(self):
        return self.checked

    def check(self, force=False):
        self.checked = True


class FakeGroup:
    def __init__(self, label, texts):
        self.label, self.boxes = label, [FakeBox(t) for t in texts]

    def locator(self, selector):
        if selector == "legend":
            return SimpleNamespace(count=lambda: 1, first=SimpleNamespace(inner_text=lambda: self.label + " *"))
        return SimpleNamespace(all=lambda: self.boxes)


VETERAN_OPTIONS = ["", "I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]


@pytest.fixture
def fits(monkeypatch):
    seen = []

    def fit_options(items, question, options, chat=None):
        seen.append(items)
        table = {"Never served": "I am not a protected veteran", "East Asian": "Asian"}
        return [jev.OptionFit(table[i], 0.9) for i in items if i in table]
    monkeypatch.setattr(jev, "fit_options", fit_options)
    return seen


def test_native_select_uses_fitted_option_and_keeps_it(env, fits):
    env.q.set_answer("Are you a veteran?", "Never served")
    el = FakeSelect("Are you a veteran?", VETERAN_OPTIONS)
    f = env.filler()
    f.fill_native_select(el)
    assert el.selected == "I am not a protected veteran"
    assert env.q.get("Are you a veteran?")["answer"] == "I am not a protected veteran"  # exact next time
    assert f.stats["option fits"] == 1 and env.calls.ask == []


def test_native_select_without_fit_asks(env, fits):
    env.q.set_answer("Are you a veteran?", "Purple")
    env.state.typed = "I don't wish to answer"
    el = FakeSelect("Are you a veteran?", VETERAN_OPTIONS)
    env.filler().fill_native_select(el)
    assert env.calls.ask == ["Are you a veteran?"] and el.selected == "I don't wish to answer"


def test_native_select_no_classify_skips_fitting(env, fits):
    env.q.set_answer("Are you a veteran?", "Never served")
    env.filler(classify=False).fill_native_select(FakeSelect("Are you a veteran?", VETERAN_OPTIONS))
    assert fits == [] and env.calls.ask == ["Are you a veteran?"]


def test_choice_group_fits_only_unmatched_items(env, fits):
    env.q.record("Ethnicity", "checkbox")
    env.q.set_answer("Ethnicity", ["White", "East Asian"])
    group = FakeGroup("Ethnicity", ["Asian", "White", "Black"])
    f = env.filler()
    f.fill_choice_group(group)
    assert [b.text for b in group.boxes if b.checked] == ["Asian", "White"]
    assert fits == [["East Asian"]]
    assert env.q.get("Ethnicity")["answer"] == ["White", "Asian"]


def test_choice_group_all_matched_makes_no_llm_call(env, fits):
    env.q.record("Ethnicity", "checkbox")
    env.q.set_answer("Ethnicity", ["White"])
    env.filler().fill_choice_group(FakeGroup("Ethnicity", ["Asian", "White"]))
    assert fits == []


def test_fit_below_threshold_is_ignored(env, monkeypatch):
    monkeypatch.setattr(jev, "fit_options", lambda items, q, options, chat=None: [jev.OptionFit("Asian", 0.4)])
    assert env.filler().fit_options("Ethnicity", "East Asian", ["Asian", "White"]) == []


@pytest.mark.parametrize("label, prev, unneeded", [
    ("If you selected Other please tell us how you learned about this job.", "Linkedin", True),
    ("If you selected Other please tell us how you learned about this job.", "Other", False),
    ("If yes, please advise what support is needed:", "No", True),
    ("If yes, please advise what support is needed:", "Yes", False),
    ("If yes, please advise what support is needed:", None, False),  # parent unknown: ask
    ("Please explain your background", "No", False),                 # not a follow-up
    ("If you answered 'yes', list the countries", ["Yes, I do"], False),
])
def test_follow_up_unneeded(label, prev, unneeded):
    assert resolver.follow_up_unneeded(label, ("parent", prev)) is unneeded


def test_optional_follow_up_left_blank_without_asking(env):
    f = env.filler()
    f.prev = ("How did you hear about us?", "Linkedin")
    assert f.resolve("q", "If you selected Other, please explain.", False, "text") is None
    assert env.calls.ask == [] and env.calls.classify == []
    assert f.stats["follow-ups not needed"] == 1


def test_required_follow_up_still_asked(env):
    f = env.filler()
    f.prev = ("How did you hear about us?", "Linkedin")
    env.state.classification = found(NO_MATCHING_HEAD)
    f.resolve("q", "If you selected Other, please explain.", True, "text")
    assert env.calls.review_head or env.calls.ask
