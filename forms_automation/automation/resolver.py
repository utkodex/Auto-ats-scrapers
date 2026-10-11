"""How a form question gets its answer - the same on every job platform.

A platform's filler subclasses Resolver, supplies job_url and builtin_answer(), and calls
resolve() for each field it finds. Answers come from, in order:
  1. builtin_answer() - the platform's own fields (name, email, education...)
  2. answers.json     - the same wording answered before, or the answer of its topic
  3. profile rules    - regex on the question text
  4. Jev              - a reworded question is matched to a topic with the local LLM
  5. the user         - anything else is recorded in answers.json and asked
"""

from __future__ import annotations

import re
from collections import Counter

from forms_automation.ai import answer_writer, cover_letter, head_creator, jev
from forms_automation.automation.answers import Answers
from forms_automation.common import gui
from forms_automation.db.head_store import NO_MATCHING_HEAD
from forms_automation.db.question_store import SKIP, norm

ASK = object()  # "no answer found - ask the user" (None means "leave the field blank")
WRITABLE = ("text", "textarea")  # field types an LLM-written answer can go into
MAX_REWRITES = 3
# "If yes, ..." / "If you selected Other, ..." - only needed when the previous answer says so.
FOLLOW_UP = re.compile(r"\s*if (?:you (?:selected|chose|answered) )?\W*(yes|other)\b", re.I)


def follow_up_unneeded(label: str, prev: tuple[str, object] | None) -> bool:
    """True for a follow-up question whose trigger (yes / other) the previous answer doesn't contain."""
    match = FOLLOW_UP.match(label)
    if not match or not prev or prev[1] is None:
        return False
    answer = " ".join(map(str, prev[1])) if isinstance(prev[1], list) else str(prev[1])
    return match[1].lower() not in norm(answer).split()


def once(fn):
    """fn() evaluated at most once (None stays None)."""
    if fn is None:
        return None
    cache: list = []

    def wrapper():
        if not cache:
            cache.append(fn())
        return cache[0]
    return wrapper


class Resolver:
    """Platform-independent answer lookup for one job's form."""

    def __init__(self, answers: Answers):
        self.answers = answers
        self.profile = answers.profile
        self.unknown: list[str] = []  # questions asked/recorded in answers.json this run
        self.unsure: list[str] = []   # --no-ask: Jev wasn't sure of the topic, left blank
        self.stats: Counter = Counter()  # how answers were found, for the report (counts only)
        self.prev: tuple[str, object] | None = None  # previous field, for "if other, explain"

    @property
    def job_url(self) -> str | None:
        """The job being filled (recorded with each question)."""
        return None

    def builtin_answer(self, field_id: str):
        """Answers tied to the platform's own field ids rather than question text."""
        return None

    def resolve(self, field_id: str, label: str, required: bool, kind: str, options_fn=None, multi=False):
        answer = self._resolve(field_id, label, required, kind, options_fn, multi)
        self.prev = (label, answer)
        return answer

    def _resolve(self, field_id, label, required, kind, options_fn, multi):
        answer = self.builtin_answer(field_id)
        if answer is not None:
            return answer
        options_fn = once(options_fn)  # opening a dropdown to read its options has side effects
        answers, store = self.answers, self.answers.store
        entry = store.get(label)
        known = entry is not None
        head = answers.head_of(entry)
        if known:  # a question we've met before - count this sighting
            store.record(label, kind, required=required, job=self.job_url)
            if head and head["mode"] == "generate" and kind in WRITABLE:
                answer = self.generate(label, kind, head)
                if answer is not ASK:
                    return answer
                answer = None
            else:
                answer = store.answer_for(label)
                if answer is None and head:
                    answer = answers.heads.answer_for_head(head["id"], store)
                    if answer is not None:
                        self.stats["filled from a topic"] += 1
        if answer is None:
            answer = answers.rule_answer(label)
        if answer is None and not required and follow_up_unneeded(label, self.prev):
            self.stats["follow-ups not needed"] += 1
            return None
        if answer is None and not (entry and entry.get("head")) and answers.classifier_ready():
            answer = self.classify(label, required, kind, options_fn, known)
            if answer is ASK:
                answer = None
            else:
                return None if answer == SKIP else answer
        if answer is None:
            options = options_fn() if options_fn else None
            answer = self.ask_user(label, required, kind, options, multi,
                                   recorded=store.get(label) is not None)
        return None if answer == SKIP else answer

    def classify(self, label: str, required: bool, kind: str, options_fn, known: bool):
        """Find the head of a question that isn't stored with one. Returns the answer, None
        to leave the field blank, or ASK to fall back to asking the user."""
        answers, store = self.answers, self.answers.store
        options = options_fn() if options_fn else None
        found = jev.classify_head(label, kind, options, answers.heads.approved())
        if not known:
            store.record(label, kind, options, required, job=self.job_url)
        head, source = None, "jev"
        if found.head == NO_MATCHING_HEAD:
            self.stats["no topic matched"] += 1
            if not answers.interactive:
                return ASK
            head = answers.new_head(label)
            if head is None:
                return ASK
            self.stats["new topics"] += 1
            source = "user"
        else:
            head = answers.heads.get(found.head)
            if not found.verified or found.confidence < answers.threshold or not head["auto_fill"]:
                if not answers.interactive:
                    self.unsure.append(label)
                    return ASK
                preview = answers.heads.answer_for_head(head["id"], store)
                preview = "(never filled)" if preview == SKIP else (
                    ", ".join(preview) if isinstance(preview, list) else preview)
                action, chosen = answers.confirm_head(label, self.job_url, head, found, preview)
                if action == gui.NO_CLASSIFY:
                    return ASK
                if action == gui.NEW_HEAD:
                    head = answers.new_head(label)
                    if head is None:
                        return ASK
                    self.stats["new topics"] += 1
                elif action == gui.OTHER_HEAD:
                    head = answers.heads.get(chosen)
                source = "user"
                self.stats["topic confirmed by you"] += 1
        store.set_head(label, head["id"], source)
        if head["mode"] == "generate" and kind in WRITABLE:
            return self.generate(label, kind, head)
        answer = answers.heads.answer_for_head(head["id"], store)
        if answer is None:
            return ASK  # the user answers once; it's saved on this question, inside the head
        self.stats["filled from a topic"] += 1
        return answer

    def generate(self, label: str, kind: str, head: dict):
        """Have the LLM write a fresh answer, let the user review it, and keep it in
        answers.json under "generated". Returns the text, None (leave blank) or ASK."""
        answers, store = self.answers, self.answers.store
        if not answers.interactive and not head["auto_fill"]:
            self.stats["written answers left for review"] += 1
            return None
        if not answers.llm_ready():
            return ASK
        try:
            job = answer_writer.job_for(self.job_url)
            examples = [g["answer"] for q in store.in_head(head["id"]) for g in q.get("generated") or []][-2:]
            prev = self.prev if head_creator.IF_OTHER.search(label) else None
            print(f"  ~ writing an answer for '{label[:60]}' (local LLM)...")
            for _ in range(MAX_REWRITES + 1):
                draft = answer_writer.write_answer(label, kind, job, answers.profile, prev, examples)
                if not answers.interactive:
                    action, text = gui.UPLOAD, draft["answer"]
                    break
                at = f" ({job['company']})" if job.get("company") else ""
                action, text = answers.review_answer(f"Answer - {label}{at}", draft["answer"], draft["warnings"])
                if action != gui.REGENERATE:
                    break
        except Exception as exc:  # never stop the form over a written answer
            print(f"  ! couldn't write an answer: {str(exc).splitlines()[0]}")
            return ASK
        if action == gui.SKIP_LETTER or not text.strip():
            return None
        store.add_generated(label, self.job_url, text.strip(), cover_letter.DEFAULT_MODEL)
        self.stats["written answers"] += 1
        return text.strip()

    def ask_user(self, label: str, required: bool, kind: str, options: list[str] | None,
                 multi=False, recorded=False):
        """Record an unknown question in answers.json, then ask for its answer."""
        if not recorded:
            self.answers.store.record(label, kind, options, required, job=self.job_url)
        elif options:  # already counted this run - just refresh its options
            self.answers.store.update_options(label, options)
        self.unknown.append(label)
        return self.answers.ask(label, options, required, multi, kind, job=self.job_url)

    def fit_options(self, label: str, answer, options: list[str]) -> list[str]:
        """Options that mean the same as a stored answer whose wording matched none of them
        (Jev, local LLM). [] when nothing fits well enough or classification is off."""
        answers = self.answers
        options = [o for o in options if o]
        items = [] if answer is None else [str(i) for i in (answer if isinstance(answer, list) else [answer])]
        if not items or not options or not answers.classify or not answers.llm_ready():
            return []
        fits = jev.fit_options(items, label, options)
        return list(dict.fromkeys(f.option for f in fits if f.confidence >= answers.threshold))

    def keep_fit(self, label: str, chosen: list[str], multi: bool) -> None:
        """Save the fitted option(s) as this question's own answer: an exact match next time."""
        self.stats["option fits"] += 1
        if self.answers.store.get(label) is not None:
            self.answers.store.set_answer(label, chosen if multi else chosen[0])
