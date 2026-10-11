"""Text matching between a saved answer and a form's options - no LLM, any platform."""

from __future__ import annotations

import difflib

from forms_automation.db.question_store import negated, norm

# Answers that tick a single consent checkbox.
YES = {"yes", "y", "true", "1", "agree", "i agree"}


def best_option(answer: str, options: list[str], threshold: float = 0.6) -> int | None:
    """Index of the option that best matches answer, or None.

    None also when the answer is ambiguous, e.g. "Yes" against
    "Yes, H-1B" / "Yes, EU Blue Card" - the caller then asks the user.
    """
    wanted = norm(answer)
    if not wanted:
        return None
    if sum(norm(o) == wanted for o in options) == 0:
        prefixed = [o for o in options if norm(o).startswith(wanted + " ")]
        if len(prefixed) > 1:
            return None
    best, best_score = None, 0.0
    for i, option in enumerate(options):
        have = norm(option)
        if have == wanted:
            score = 1.0
        elif have.startswith(wanted + " ") or wanted.startswith(have + " "):
            score = 0.9
        elif f" {wanted} " in f" {have} ":
            score = 0.8
        elif negated(wanted) != negated(have):
            # Similar spelling, opposite meaning: "I am not a protected
            # veteran" must never become "Other Protected Veteran".
            score = 0.0
        else:
            score = difflib.SequenceMatcher(None, wanted, have).ratio()
            if score < 0.75:  # loose look-alikes go to the user instead
                score = 0.0
        score -= len(have) / 10_000  # prefer the shorter option on ties
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= threshold else None
