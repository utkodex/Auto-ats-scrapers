"""Where answers come from, for any platform: profile rules, answers.json, topics,
and asking the user (pop-up window or terminal)."""

from __future__ import annotations

import re

from forms_automation.ai import head_creator, jev, llm
from forms_automation.common import gui
from forms_automation.db import heads as heads_cli
from forms_automation.db.head_store import HeadStore
from forms_automation.db.question_store import SKIP, QuestionStore


class Answers:
    def __init__(self, profile: dict, store: QuestionStore, interactive: bool = True, use_gui: bool = True,
                 heads: HeadStore | None = None, classify: bool = True,
                 threshold: float = jev.CONFIDENCE_THRESHOLD):
        self.profile = profile
        self.store = store
        self.interactive = interactive
        self.use_gui = use_gui
        self.rules = [(re.compile(r["match"], re.I), r["answer"]) for r in profile.get("rules", [])]
        self.heads = heads
        self.classify = classify and heads is not None
        self.threshold = threshold
        self._llm_ok: bool | None = None  # Ollama is checked once per run

    def llm_ready(self) -> bool:
        if self._llm_ok is None:
            self._llm_ok = llm.available(jev.CLASSIFIER_MODEL)
            if not self._llm_ok:
                print(f"  ! Ollama isn't running (or {jev.CLASSIFIER_MODEL} isn't pulled) - questions are "
                      "matched by exact wording only this run")
        return self._llm_ok

    def classifier_ready(self) -> bool:
        return self.classify and bool(self.heads.approved()) and self.llm_ready()

    def head_of(self, entry: dict | None) -> dict | None:
        """The approved head of a stored question, if any (never with --no-classify)."""
        if not self.classify or not entry or not entry.get("head"):
            return None
        head = self.heads.get(entry["head"])
        return head if head and head["status"] == "approved" else None

    def rule_answer(self, label: str):
        for pattern, answer in self.rules:
            if pattern.search(label):
                return answer
        return None

    def ask(self, label: str, options: list[str] | None, required: bool, multi: bool = False,
            kind: str = "text", job: str | None = None):
        """Ask the user; the answer is stored in answers.json for next time."""
        if not self.interactive:
            return None
        if not self.use_gui:
            return self.ask_terminal(label, options, required, multi)
        action, answer = gui.ask_question(label, options, required, multi, kind, job)
        if action == gui.SKIP:
            self.store.set_answer(label, SKIP)
            return None
        if action == gui.BLANK:
            return None
        self.store.set_answer(label, answer)
        return answer

    def ask_terminal(self, label: str, options: list[str] | None, required: bool, multi: bool = False):
        print()
        print(f"  ? {label}{'  (required)' if required else ''}")
        if options:
            for i, option in enumerate(options[:60], 1):
                print(f"      {i:>2}. {option}")
            if len(options) > 60:
                print(f"      ... {len(options) - 60} more (type text to search)")
            hint = "number(s) like 1,3" if multi else "number"
            print(f"    Enter a {hint} or text.", end=" ")
        print("Blank = leave empty this time, 'skip' = never fill this question.")
        try:
            raw = input("    > ").strip()
        except EOFError:  # no terminal attached
            return None
        if not raw:
            return None
        if raw.lower() == "skip":
            self.store.set_answer(label, SKIP)
            return None
        answer: object = raw
        if options and re.fullmatch(r"\d+(\s*,\s*\d+)*", raw):
            picks = [options[int(n) - 1] for n in re.split(r"\s*,\s*", raw) if 0 < int(n) <= len(options)]
            if picks:
                answer = picks if multi else picks[0]
        self.store.set_answer(label, answer)
        return answer

    # -- classification heads (topics) ------------------------------------

    def confirm_head(self, label: str, job: str | None, head: dict, found: jev.Classification,
                     preview: str | None) -> tuple[str, str | None]:
        """Is Jev's head right? Returns (CONFIRM | OTHER_HEAD | NEW_HEAD | NO_CLASSIFY, head id)."""
        approved = self.heads.approved()
        if self.use_gui:
            return gui.confirm_head(label, job, head, found.confidence, found.reason, preview, approved)
        print()
        print(f"  ? {label}")
        print(f"    Jev thinks this is: {head['id']} (confidence {found.confidence:.2f}) - {found.reason}")
        if head["mode"] == "generate":
            print("    Will fill: a new answer written for this job")
        else:
            print(f"    Will fill: {preview if preview is not None else 'nothing saved yet - you will be asked'}")
        try:
            choice = input("    [y] use it, [o] other topic, [n] new topic, blank = no topic: ").strip().lower()
        except EOFError:
            return gui.NO_CLASSIFY, None
        if choice == "y":
            return gui.CONFIRM, head["id"]
        if choice == "n":
            return gui.NEW_HEAD, None
        if choice == "o":
            for i, h in enumerate(approved, 1):
                print(f"      {i:>2}. {h['id']} - {h['description']}")
            pick = input("    number: ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(approved):
                return gui.OTHER_HEAD, approved[int(pick) - 1]["id"]
        return gui.NO_CLASSIFY, None

    def new_head(self, label: str) -> dict | None:
        """Llama drafts a head for a question no approved head fits; the user approves, edits,
        merges or rejects it. Returns the approved head the question now belongs to, or None."""
        try:
            draft = head_creator.propose_head(self.store.get(label), self.heads)
        except llm.LLMError as exc:
            print(f"  ! couldn't draft a new topic: {exc}")
            return None
        joined = draft.get("join", False)
        if joined:  # Llama filed it under an existing head after all
            draft = {**self.heads.get(draft["id"]), "join": True}
        draft.setdefault("auto_fill", draft.get("mode", "reuse") == "reuse")
        if self.use_gui:
            action, payload = gui.review_head(draft, [label], self.heads.heads, draft.get("flag"))
        else:
            action, payload = heads_cli.review_terminal(draft, [label], self.heads.heads)
        if action == gui.APPROVE and joined:
            head = self.heads.get(draft["id"])
        elif action == gui.APPROVE:
            fields = {k: v for k, v in payload.items() if k not in ("join", "flag")}
            head = self.heads.add({**fields, "created_by": "llama"}, status="approved")
        elif action == gui.MERGE:
            head = self.heads.get(payload)
        elif action is None and not joined:  # decide later: heads.py review
            fields = {k: v for k, v in draft.items() if k != "join"}
            self.heads.add({**fields, "created_by": "llama"}, status="pending")
            self.store.set_head(label, draft["id"], "llama")
            return None
        else:
            return None
        self.store.set_head(label, head["id"], "user")
        return head

    def review_answer(self, heading: str, text: str, warnings: list[str]) -> tuple[str, str]:
        """(UPLOAD = fill, REGENERATE = write again, SKIP_LETTER = leave blank, edited text)."""
        if self.use_gui:
            return gui.review_letter(heading, text, warnings, labels=gui.ANSWER_LABELS)
        print(f"\n  --- {heading} ---\n\n{text}\n")
        for warning in warnings:
            print(f"  ! {warning}")
        try:
            choice = input("\n  Answer: [f]ill it, [w]rite again or [b]lank? ").strip().lower()[:1]
        except EOFError:
            choice = ""
        return {"f": gui.UPLOAD, "w": gui.REGENERATE}.get(choice, gui.SKIP_LETTER), text
