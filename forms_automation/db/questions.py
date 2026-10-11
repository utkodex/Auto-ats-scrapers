"""View and answer the questions stored in answers.json.

    python forms_automation/db/questions.py list                 # everything
    python forms_automation/db/questions.py list --unanswered    # still need an answer
    python forms_automation/db/questions.py list --search visa
    python forms_automation/db/questions.py show 12
    python forms_automation/db/questions.py answer 12 2          # option number...
    python forms_automation/db/questions.py answer 12 "30 days"  # ...or text
    python forms_automation/db/questions.py answer 12 1,3        # several options
    python forms_automation/db/questions.py answer               # walk through every unanswered one
    python forms_automation/db/questions.py skip 12              # never fill this question
    python forms_automation/db/questions.py clear 12             # forget the answer (ask again)
    python forms_automation/db/questions.py delete 12
    python forms_automation/db/questions.py export questions.csv # edit in Excel...
    python forms_automation/db/questions.py import questions.csv # ...and load it back
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a file: make the forms_automation package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from forms_automation.db.question_store import SKIP, QuestionStore


def shown_answer(row: dict) -> str:
    answer = row["answer"]
    if answer is None:
        return "-- not answered --"
    if answer == SKIP:
        return "(skip)"
    return ", ".join(answer) if isinstance(answer, list) else str(answer)


def options_of(row: dict) -> list[str]:
    return row["options"] or []


def parse_answer(row: dict, raw: str):
    """Option numbers ("2", "1,3") become option texts; anything else is kept as typed."""
    options = options_of(row)
    if options and re.fullmatch(r"\d+(\s*,\s*\d+)*", raw):
        picks = [options[int(n) - 1] for n in re.split(r"\s*,\s*", raw) if 0 < int(n) <= len(options)]
        if picks:
            return picks if len(picks) > 1 or row["type"] in ("multiselect", "checkbox") else picks[0]
    return raw


def get_row(db: QuestionStore, qid: int) -> dict:
    row = db.by_id(qid)
    if not row:
        sys.exit(f"No question with id {qid}")
    return row


def print_row(row: dict, full: bool = False) -> None:
    flag = "*" if row["required"] else " "
    label = row["question"] if full else (row["question"][:90] + ("..." if len(row["question"]) > 90 else ""))
    print(f"[{row['id']:>3}]{flag} {label}")
    if full:
        print(f"       type: {row['type'] or '?'}   seen: {row['times_seen']}x   last job: {row['last_job'] or '-'}")
        for i, option in enumerate(options_of(row), 1):
            print(f"         {i:>2}. {option}")
    print(f"       -> {shown_answer(row)}")


def set_answer(db: QuestionStore, qid: int, answer) -> None:
    get_row(db, qid)["answer"] = answer
    db.save()


def cmd_list(db: QuestionStore, args) -> None:
    rows = db.rows(unanswered_only=args.unanswered, search=args.search)
    for row in rows:
        print_row(row)
    unanswered = sum(row["answer"] is None for row in rows)
    print(f"\n{len(rows)} question(s), {unanswered} not answered.  (* = required)")


def cmd_answer(db: QuestionStore, args) -> None:
    if args.id is not None:
        if args.value is None:
            sys.exit("Give the answer too, e.g.: answer 12 2   or   answer 12 \"30 days\"")
        row = get_row(db, args.id)
        set_answer(db, args.id, parse_answer(row, args.value))
        print_row(get_row(db, args.id))
        return
    # Interactive pass over everything still unanswered.
    rows = db.rows(unanswered_only=True)
    if not rows:
        print("Nothing left to answer.")
        return
    print("Enter = leave for later, 'skip' = never fill, 'q' = quit.\n")
    for row in rows:
        print_row(row, full=True)
        raw = input("       > ").strip()
        if raw.lower() == "q":
            break
        if raw:
            set_answer(db, row["id"], SKIP if raw.lower() == "skip" else parse_answer(row, raw))
        print()


def cmd_export(db: QuestionStore, args) -> None:
    with open(args.path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        writer = csv.writer(f)
        writer.writerow(["id", "required", "question", "type", "options", "answer", "times_seen", "last_job"])
        for row in db.rows():
            writer.writerow([
                row["id"], "yes" if row["required"] else "", row["question"], row["type"] or "",
                " | ".join(options_of(row)),
                "" if row["answer"] is None else shown_answer(row),
                row["times_seen"], row["last_job"] or "",
            ])
    print(f"Wrote {args.path}. Fill the 'answer' column (option text, number, or 'skip'), then import it.")


def cmd_import(db: QuestionStore, args) -> None:
    changed = 0
    with open(args.path, newline="", encoding="utf-8-sig") as f:
        for record in csv.DictReader(f):
            raw = (record.get("answer") or "").strip()
            if not raw or raw == "-- not answered --":
                continue
            row = db.by_id(int(record["id"]))
            if not row:
                continue
            answer = SKIP if raw.lower() in ("skip", "(skip)") else parse_answer(row, raw)
            if row["type"] in ("multiselect", "checkbox") and isinstance(answer, str) and ", " in answer:
                answer = [a.strip() for a in answer.split(", ")]
            set_answer(db, row["id"], answer)
            changed += 1
    print(f"Updated {changed} answer(s).")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--unanswered", action="store_true")
    p.add_argument("--search")
    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p = sub.add_parser("answer")
    p.add_argument("id", type=int, nargs="?")
    p.add_argument("value", nargs="?")
    for name in ("skip", "clear", "delete"):
        sub.add_parser(name).add_argument("id", type=int)
    sub.add_parser("export").add_argument("path", type=Path)
    sub.add_parser("import").add_argument("path", type=Path)
    args = parser.parse_args()

    db = QuestionStore()
    if args.command == "list":
        cmd_list(db, args)
    elif args.command == "show":
        print_row(get_row(db, args.id), full=True)
    elif args.command == "answer":
        cmd_answer(db, args)
    elif args.command == "skip":
        get_row(db, args.id)
        set_answer(db, args.id, SKIP)
    elif args.command == "clear":
        get_row(db, args.id)
        set_answer(db, args.id, None)
    elif args.command == "delete":
        get_row(db, args.id)
        db.delete(args.id)
    elif args.command == "export":
        cmd_export(db, args)
    elif args.command == "import":
        cmd_import(db, args)


if __name__ == "__main__":
    main()
