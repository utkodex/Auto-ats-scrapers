"""Jev: the type-safe classifier.

Every answer Jev gives is one value from a closed list that code builds for
the call - an approved head id or NO_MATCHING_HEAD, a form option or
NO_GOOD_OPTION - so a reply can never name a head or option that doesn't
exist. It runs on llama3.1:8b through Ollama structured output (an `enum` in
the JSON schema); swapping the backend must keep these two functions.

classify_head() works in two steps: Jev picks the closest head (reasoning
first, then the head), then a strict check asks whether the saved answer of
that head could be copied unchanged. Only a verified pick may fill a field
without asking; llama3.1:8b's own confidence isn't reliable (it reports 1.0 for
most wrong picks - see evals/run_classification.py).

classify_head() sees question text, type and options only - never an answer.
fit_option() is the one call that sends a stored answer, and only to the
local Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass

from forms_automation.ai import llm
from forms_automation.db.head_store import NO_MATCHING_HEAD
from forms_automation.db.question_store import negated, norm

CONFIDENCE_THRESHOLD = 0.75  # below this the user confirms the head (tune with evals/run_classification.py)
CLASSIFIER_MODEL = "llama3.1:8b"
NO_GOOD_OPTION = "NO_GOOD_OPTION"
MAX_EXAMPLES = 5

CLASSIFY_PROMPT = """You classify one job-application question into one of the given heads.
A head stores ONE answer. Pick a head only if the same person would give the same
answer to this question as to the head's examples. Company names and small wording
differences (e.g. "20%" vs "25%" travel) don't matter; a different fact does.
If no head stores the answer to this question, return NO_MATCHING_HEAD.
First write reason: which fact the question asks for, and which head (if any) stores exactly that fact.
Then head and confidence (0 to 1).
Return JSON only."""

# Second step: strict on purpose. In tests it rejected every wrong pick (0/40) but
# also many right ones - those are shown to the user with the pick pre-selected.
VERIFY_PROMPT = """A job-application question and a saved topic. Answer whether the SAME person's saved
answer for the topic can be copied unchanged as the answer to this question.
Different facts (e.g. gender vs sexual orientation, graduation date vs enrollment, current employer
vs a referrer's name) mean "no". Return JSON only."""
VERIFY_SCHEMA = {"type": "object", "required": ["reason", "same_answer"],
                 "properties": {"reason": {"type": "string"},
                                "same_answer": {"type": "string", "enum": ["yes", "no"]}}}

FIT_PROMPT = """You match a saved answer to the options of a job-application question.
Pick the option that means the same as the stored answer for this question;
NO_GOOD_OPTION if none does. Never pick an option with the opposite meaning.
confidence: how sure you are, 0 to 1.
Return JSON only."""


@dataclass(frozen=True)
class Classification:
    head: str            # an approved head id, or NO_MATCHING_HEAD
    confidence: float    # 0.0-1.0
    reason: str          # one line, shown in the confirm dialog
    verified: bool = False  # the strict check agreed: may fill without asking


@dataclass(frozen=True)
class OptionFit:
    option: str | None   # one of the form's options, or None for NO_GOOD_OPTION
    confidence: float


_cache: dict[tuple, Classification] = {}


def clear_cache() -> None:
    _cache.clear()


def _clamp(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def classify_schema(head_ids: list[str]) -> dict:
    # Ollama generates keys in this order: the reason comes before the choice.
    return {"type": "object", "required": ["reason", "head", "confidence"],
            "properties": {
                "reason": {"type": "string"},
                "head": {"type": "string", "enum": [*_unique(head_ids), NO_MATCHING_HEAD]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}


def classify_message(question: str, kind: str | None, options: list[str] | None, heads: list[dict]) -> str:
    lines = [f"Question: {question}", f"Type: {kind or 'unknown'}"]
    if options:
        lines.append("Options: " + "; ".join(options))
    lines.append("\nHeads:")
    for h in heads:
        examples = "; ".join(f'"{e}"' for e in (h.get("examples") or [])[:MAX_EXAMPLES])
        lines.append(f"- {h['id']}: {h.get('description', '')} | examples: {examples}")
    return "\n".join(lines)


def classify_head(question: str, kind: str | None, options: list[str] | None,
                  heads: list[dict], chat=None) -> Classification:
    """Which approved head stores the answer to this question (NO_MATCHING_HEAD if none)."""
    if not heads:
        return Classification(NO_MATCHING_HEAD, 0.0, "no approved heads yet")
    ids = [h["id"] for h in heads]
    key = (norm(question), tuple(sorted(ids)))
    if key in _cache:
        return _cache[key]
    try:
        data = (chat or llm.chat_json)(CLASSIFY_PROMPT, classify_message(question, kind, options, heads),
                                       classify_schema(ids), model=CLASSIFIER_MODEL)
    except llm.LLMError as exc:
        return Classification(NO_MATCHING_HEAD, 0.0, f"classifier error: {exc}")
    head = data.get("head")
    if head not in ids and head != NO_MATCHING_HEAD:  # backend ignored the enum
        result = Classification(NO_MATCHING_HEAD, 0.0, f"invalid head {head!r}")
    else:
        reason = str(data.get("reason", "")).strip()
        verified = head != NO_MATCHING_HEAD and verify_head(question, next(h for h in heads if h["id"] == head),
                                                            chat)
        result = Classification(head, _clamp(data.get("confidence")), reason, verified)
    _cache[key] = result
    return result


def verify_head(question: str, head: dict, chat=None) -> bool:
    """Strict check: could the head's saved answer be copied unchanged? False on any error."""
    examples = "\n".join(f"- {e}" for e in (head.get("examples") or [])[:MAX_EXAMPLES])
    message = (f"Question: {question}\nTopic: {head['id']} - {head.get('description', '')}\n"
               f"Example questions of the topic:\n{examples}")
    try:
        data = (chat or llm.chat_json)(VERIFY_PROMPT, message, VERIFY_SCHEMA, model=CLASSIFIER_MODEL)
    except llm.LLMError:
        return False
    return data.get("same_answer") == "yes"


def fit_schema(options: list[str]) -> dict:
    return {"type": "object", "required": ["option", "confidence"],
            "properties": {
                "option": {"type": "string", "enum": [*_unique(options), NO_GOOD_OPTION]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}


def fit_option(stored_answer: str, question: str, options: list[str], chat=None) -> OptionFit:
    """The form option that means the same as a stored answer, or OptionFit(None, ...)."""
    if not options or not str(stored_answer or "").strip():
        return OptionFit(None, 0.0)
    message = (f"Question: {question}\nStored answer: {stored_answer}\nOptions:\n"
               + "\n".join(f"- {o}" for o in options))
    try:
        data = (chat or llm.chat_json)(FIT_PROMPT, message, fit_schema(options), model=CLASSIFIER_MODEL)
    except llm.LLMError:
        return OptionFit(None, 0.0)
    option, confidence = data.get("option"), _clamp(data.get("confidence"))
    if option not in options:
        return OptionFit(None, confidence)
    if negated(str(stored_answer)) != negated(option):  # opposite meaning, e.g. "not a veteran"
        return OptionFit(None, 0.0)
    return OptionFit(option, confidence)


def fit_options(stored_answers: list[str], question: str, options: list[str], chat=None) -> list[OptionFit]:
    """Multiselect: fit each stored item on its own; items with no good option are dropped."""
    fits = [fit_option(a, question, options, chat) for a in stored_answers]
    return [f for f in fits if f.option is not None]
