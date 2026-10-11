from __future__ import annotations

import pytest

from forms_automation.ai import jev, llm
from forms_automation.db.head_store import NO_MATCHING_HEAD

HEADS = [{"id": "gender", "description": "Gender identity", "examples": [f"Gender {i}?" for i in range(8)]},
         {"id": "travel", "description": "Willingness to travel", "examples": ["Can you travel 20%?"]}]


class Chat:
    """Fake chat_json: `reply` for the classify call, `verify` ("yes"/"no"/Exception) for the check."""

    def __init__(self, reply, verify="yes"):
        self.reply, self.verify = reply, verify
        self.calls = []         # classify calls
        self.verify_calls = []  # strict-check calls

    def __call__(self, system, user, schema, **kw):
        if "same_answer" in schema["properties"]:
            self.verify_calls.append({"system": system, "user": user, **kw})
            if isinstance(self.verify, Exception):
                raise self.verify
            return {"reason": "r", "same_answer": self.verify}
        self.calls.append({"system": system, "user": user, "schema": schema, **kw})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def fresh_cache():
    jev.clear_cache()
    yield
    jev.clear_cache()


def test_schema_enum_is_approved_ids_plus_no_match():
    chat = Chat({"head": "gender", "confidence": 0.9, "reason": "same"})
    result = jev.classify_head("What is your sex?", "select", ["Male", "Female"], HEADS, chat)
    assert result == jev.Classification("gender", 0.9, "same", verified=True)
    enum = chat.calls[0]["schema"]["properties"]["head"]["enum"]
    assert enum == ["gender", "travel", NO_MATCHING_HEAD]
    assert chat.calls[0]["model"] == jev.CLASSIFIER_MODEL


def test_message_lists_heads_with_max_five_examples():
    chat = Chat({"head": "gender", "confidence": 0.9, "reason": ""})
    jev.classify_head("What is your sex?", "select", ["Male", "Female"], HEADS, chat)
    user = chat.calls[0]["user"]
    assert "Options: Male; Female" in user
    assert "- gender: Gender identity | examples:" in user
    assert '"Gender 4?"' in user and '"Gender 5?"' not in user


def test_out_of_set_head_becomes_no_match():
    result = jev.classify_head("Q?", None, None, HEADS, Chat({"head": "salary", "confidence": 1, "reason": ""}))
    assert result.head == NO_MATCHING_HEAD and result.confidence == 0.0


@pytest.mark.parametrize("raw, expected", [(1.7, 1.0), (-2, 0.0), ("x", 0.0), (None, 0.0), (0.4, 0.4)])
def test_confidence_clamped(raw, expected):
    result = jev.classify_head(f"Q {raw}?", None, None, HEADS,
                               Chat({"head": "travel", "confidence": raw, "reason": ""}))
    assert result.confidence == expected


def test_llm_error_means_no_match():
    result = jev.classify_head("Q?", None, None, HEADS, Chat(llm.LLMError("down")))
    assert result.head == NO_MATCHING_HEAD and result.confidence == 0.0


def test_no_heads_skips_the_model():
    chat = Chat({"head": "x", "confidence": 1, "reason": ""})
    assert jev.classify_head("Q?", None, None, [], chat).head == NO_MATCHING_HEAD
    assert chat.calls == []


def test_cache_by_normalised_question():
    chat = Chat({"head": "travel", "confidence": 0.8, "reason": ""})
    jev.classify_head("Can you travel?", None, None, HEADS, chat)
    jev.classify_head("can you TRAVEL", None, None, HEADS, chat)
    assert len(chat.calls) == 1
    jev.classify_head("can you TRAVEL", None, None, HEADS[:1], chat)  # different head list -> new call
    assert len(chat.calls) == 2


def test_reason_comes_before_head_in_schema():
    chat = Chat({"head": "travel", "confidence": 0.8, "reason": ""})
    jev.classify_head("Can you travel?", None, None, HEADS, chat)
    assert list(chat.calls[0]["schema"]["properties"])[:2] == ["reason", "head"]


def test_verified_only_when_strict_check_says_yes():
    yes = Chat({"head": "travel", "confidence": 0.9, "reason": ""}, verify="yes")
    assert jev.classify_head("Travel 25%?", None, None, HEADS, yes).verified is True
    assert "Can you travel 20%?" in yes.verify_calls[0]["user"]  # the head's examples are shown
    jev.clear_cache()
    no = Chat({"head": "travel", "confidence": 1.0, "reason": ""}, verify="no")
    found = jev.classify_head("Travel 25%?", None, None, HEADS, no)
    assert found.head == "travel" and found.verified is False
    jev.clear_cache()
    broken = Chat({"head": "travel", "confidence": 1.0, "reason": ""}, verify=llm.LLMError("x"))
    assert jev.classify_head("Travel 25%?", None, None, HEADS, broken).verified is False


def test_no_match_skips_strict_check():
    chat = Chat({"head": NO_MATCHING_HEAD, "confidence": 0.9, "reason": ""})
    assert jev.classify_head("Salary?", None, None, HEADS, chat).verified is False
    assert chat.verify_calls == []


# ---- fit_option -----------------------------------------------------------------------

OPTIONS = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]


def test_fit_option_picks_enum_option():
    chat = Chat({"option": "I am not a protected veteran", "confidence": 0.9})
    fit = jev.fit_option("Not a veteran", "Veteran status", OPTIONS, chat)
    assert fit == jev.OptionFit("I am not a protected veteran", 0.9)
    assert chat.calls[0]["schema"]["properties"]["option"]["enum"] == [*OPTIONS, jev.NO_GOOD_OPTION]


def test_fit_option_negation_guard():
    chat = Chat({"option": "I am a protected veteran", "confidence": 0.95})
    assert jev.fit_option("I am not a veteran", "Veteran status", OPTIONS, chat).option is None


def test_fit_option_no_good_option_and_errors():
    assert jev.fit_option("Purple", "Q", OPTIONS, Chat({"option": "NO_GOOD_OPTION", "confidence": 0.9})).option is None
    assert jev.fit_option("Purple", "Q", OPTIONS, Chat(llm.LLMError("x"))).option is None
    assert jev.fit_option("", "Q", OPTIONS, Chat({"option": OPTIONS[0], "confidence": 1})).option is None
    assert jev.fit_option("x", "Q", [], Chat({"option": "x", "confidence": 1})).option is None


def test_fit_options_drops_unfitted_items():
    replies = iter([{"option": "Asian", "confidence": 0.9}, {"option": "NO_GOOD_OPTION", "confidence": 0.5}])

    def chat(system, user, schema, **kw):
        return next(replies)
    fits = jev.fit_options(["East Asian", "Martian"], "Ethnicity", ["Asian", "White"], chat)
    assert [f.option for f in fits] == ["Asian"]
