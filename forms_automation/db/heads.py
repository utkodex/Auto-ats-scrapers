"""Build and review the classification heads (topics) in heads.json.

    python forms_automation/db/heads.py build              # Llama groups unclassified questions, then review
    python forms_automation/db/heads.py build --no-gui     # ...and review later
    python forms_automation/db/heads.py review             # approve / edit / merge / reject pending heads
    python forms_automation/db/heads.py review --terminal
    python forms_automation/db/heads.py list               # heads and their question ids (never answers)

Only approved heads are used when filling forms.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # run as a file: make the forms_automation package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forms_automation.ai import head_creator, llm
from forms_automation.common import gui
from forms_automation.db.head_store import HeadStore, valid_id
from forms_automation.db.question_store import QuestionStore


def backup(questions: QuestionStore) -> Path | None:
    if not questions.path.exists():
        return None
    target = questions.path.with_name(f"{questions.path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(questions.path, target)
    return target


def apply_review(heads: HeadStore, questions: QuestionStore, head_id: str, action: str | None,
                 payload=None) -> str:
    """Apply a review decision; returns "approved", "merged", "rejected" or "pending"."""
    if action == gui.APPROVE:
        new_id = payload["id"]
        if new_id != head_id:
            heads.update(head_id, id=new_id)
            questions.rename_head(head_id, new_id)
        heads.get(new_id).pop("flag", None)
        heads.update(new_id, description=payload["description"], examples=payload["examples"],
                     mode=payload["mode"], auto_fill=payload["auto_fill"], status="approved")
        return "approved"
    if action == gui.MERGE:
        heads.merge(head_id, payload, questions)
        return "merged"
    if action == gui.REJECT:
        heads.update(head_id, status="rejected")
        questions.rename_head(head_id, None)
        return "rejected"
    return "pending"


def members_of(questions: QuestionStore, head_id: str) -> list[str]:
    return [q["question"] for q in questions.in_head(head_id)]


def review_terminal(head: dict, members: list[str], heads: list[dict], progress: str = ""):
    """Terminal twin of gui.review_head: same return values."""
    print(f"\n[{progress}] {head['id']}  ({head['mode']}, auto_fill={head['auto_fill']})")
    print(f"  {head['description']}")
    if head.get("flag") == "rename":
        print("  ! the name contains a company name - rename it to the fact it stores")
    for question in members:
        print(f"    - {question}")
    while True:
        choice = input("  [a]pprove  [e]dit  [m]erge  [r]eject  (blank = decide later): ").strip().lower()
        if choice == "":
            return None, None
        if choice == "a":
            return gui.APPROVE, dict(head)
        if choice == "r":
            return gui.REJECT, None
        if choice == "m":
            targets = [h for h in heads if h["id"] != head["id"] and h["status"] == "approved"]
            for i, h in enumerate(targets, 1):
                print(f"    {i}. {h['id']} - {h['description']}")
            pick = input("  merge into number (blank = back): ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(targets):
                return gui.MERGE, targets[int(pick) - 1]["id"]
            continue
        if choice == "e":
            edited = dict(head)
            taken = {h["id"] for h in heads} - {head["id"]}
            while True:
                new_id = input(f"  id [{head['id']}]: ").strip() or head["id"]
                if valid_id(new_id) and new_id not in taken:
                    break
                print("  use lower_snake_case, 2-41 characters, not an existing head")
            edited["id"] = new_id
            edited["description"] = input(f"  description [{head['description']}]: ").strip() or head["description"]
            mode = input(f"  mode reuse/generate [{head['mode']}]: ").strip() or head["mode"]
            edited["mode"] = mode if mode in ("reuse", "generate") else head["mode"]
            auto = input(f"  auto_fill y/n [{'y' if head['auto_fill'] else 'n'}]: ").strip().lower()
            edited["auto_fill"] = head["auto_fill"] if auto == "" else auto.startswith("y")
            return gui.APPROVE, edited


def review(heads: HeadStore, questions: QuestionStore, terminal: bool = False) -> dict:
    pending = list(heads.pending())
    counts = {"approved": 0, "merged": 0, "rejected": 0, "pending": 0}
    for i, head in enumerate(pending, 1):
        progress = f"{i} / {len(pending)}"
        members = members_of(questions, head["id"])
        if terminal:
            action, payload = review_terminal(head, members, heads.heads, progress)
        else:
            action, payload = gui.review_head(head, members, heads.heads, head.get("flag"), progress)
        counts[apply_review(heads, questions, head["id"], action, payload)] += 1
    summary = ", ".join(f"{n} {k}" for k, n in counts.items())
    if terminal or not pending:
        print(f"Review done: {summary}" if pending else
              "No pending heads." if heads.heads else "No topics yet - run first: python forms_automation/db/heads.py build")
    else:
        gui.notice("Heads reviewed", "Review done", [summary], ["Close"])
    return counts


def cmd_build(heads: HeadStore, questions: QuestionStore, args) -> int:
    if not llm.available():
        print("Ollama isn't running or llama3.1:8b isn't pulled (ollama pull llama3.1:8b).")
        return 1
    todo = sum(1 for q in questions.questions if not q.get("head"))
    if not todo:
        print("Every question already has a head.")
    else:
        saved = backup(questions)
        print(f"Backed up answers to {saved.name}" if saved else "No answers file yet.")
        print(f"Grouping {todo} questions with Llama (about a minute per 20)...")
        result = head_creator.build_initial_heads(questions, heads, batch=args.batch, prompt=args.prompt)
        print(f"{len(result.created)} new heads, {len(result.joined)} joined existing heads, "
              f"{result.seconds} s.")
        flagged = [h["id"] for h in heads.pending() if h.get("flag")]
        if flagged:
            print(f"Check the names of: {', '.join(flagged)}")
        if result.needs_review:
            print("Llama's grouping needed fixing - review carefully:", "; ".join(result.errors))
    if args.no_gui:
        print("Review with:  python forms_automation/db/heads.py review")
        return 0
    review(heads, questions)
    return 0


def cmd_list(heads: HeadStore, questions: QuestionStore, args) -> int:
    for head in sorted(heads.heads, key=lambda h: (h["status"], h["id"])):
        ids = [q["id"] for q in questions.in_head(head["id"])]
        extra = f" -> {head['merged_into']}" if head.get("merged_into") else ""
        print(f"{head['id']:<28} {head['status']:<9} {head['mode']:<9} "
              f"auto_fill={'yes' if head['auto_fill'] else 'no ':<4} questions {ids}{extra}")
    unclassified = [q["id"] for q in questions.questions if not q.get("head")]
    counts = {s: sum(h["status"] == s for h in heads.heads) for s in ("approved", "pending", "rejected")}
    print(f"\n{counts['approved']} approved, {counts['pending']} pending, {counts['rejected']} rejected; "
          f"questions without a head: {unclassified or 'none'}")
    if not heads.heads:
        print("No topics yet - run first: python forms_automation/db/heads.py build")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and review classification heads.")
    parser.add_argument("--answers", type=Path, help=argparse.SUPPRESS)  # tests use temp files
    parser.add_argument("--heads", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="group questions without a head (Llama), then review")
    p.add_argument("--no-gui", action="store_true", help="don't open the review windows")
    p.add_argument("--batch", type=int, default=head_creator.BATCH)
    p.add_argument("--prompt", choices=sorted(head_creator.PROMPTS), default=head_creator.PROMPT)
    p = sub.add_parser("review", help="approve, edit, merge or reject pending heads")
    p.add_argument("--terminal", action="store_true")
    sub.add_parser("list", help="heads with their question ids")
    args = parser.parse_args(argv)

    questions = QuestionStore(args.answers)
    heads = HeadStore(args.heads)
    if args.command == "build":
        return cmd_build(heads, questions, args)
    if args.command == "review":
        review(heads, questions, terminal=args.terminal)
        return 0
    return cmd_list(heads, questions, args)


if __name__ == "__main__":
    sys.exit(main())
