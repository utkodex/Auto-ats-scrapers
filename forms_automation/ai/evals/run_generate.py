"""Live eval: written answers for the two "generate" questions against three job posts.

    python forms_automation/evals/run_generate.py

Checks each draft: length in range (textarea 80-150 words, text 1-2 sentences)
and no invented numbers or [placeholders]. Style warnings from
cover_letter.warnings_for (copied job-post text, terms not on the resume,
cliches) are counted but don't fail the run - the review window shows them.
The terminal shows numbers and warning types only; the drafts are in the
result file for you to read (it's git-ignored - drafts draw on your resume).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from harness import DATA, EVALS, check, finish, load  # puts the repo root on sys.path

# isort: split

from forms_automation.ai import answer_writer, cover_letter, llm
from forms_automation.common.paths import PROFILE_FILE

GENERATE_IDS = (16, 17)
# Synthetic earlier field for the "if you selected other" question.
PREV_FIELD = ("How did you hear about this job?", "Other")
JOBS = [
    (EVALS.parent / "Test-job-post.md", ""),
    (DATA / "jobs" / "airbnb-data-scientist.md", "Airbnb"),
    (DATA / "jobs" / "gitlab-backend-python-ai.md", "GitLab"),
]


def length_ok(text: str, kind: str) -> tuple[bool, str]:
    words = len(text.split())
    if kind == "textarea":
        return 80 <= words <= 150, f"{words} words"
    sentences = len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])
    return 1 <= sentences <= 2 and words <= 60, f"{sentences} sentences, {words} words"


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    if not llm.available():
        print("Ollama isn't running or llama3.1:8b isn't pulled.")
        return 2
    profile = json.loads(PROFILE_FILE.read_text("utf-8"))
    resume = cover_letter.resume_text(Path(profile["resume"]))
    questions = [q for q in load("db_questions.json") if q["id"] in GENERATE_IDS]
    results = {"settings": {"model": cover_letter.DEFAULT_MODEL, "temperature": 0.6,
                            "system_prompt": answer_writer.SYSTEM_PROMPT}, "drafts": []}
    for path, company in JOBS:
        job = cover_letter.file_job(path, company=company or None)
        for q in questions:
            prev = PREV_FIELD if q["id"] == 16 else None
            started = time.time()
            out = answer_writer.write_answer(q["question"], q["type"], job, profile, prev, [], resume=resume)
            ok, size = length_ok(out["answer"], q["type"])
            kinds = sorted({w.split(" - ")[0].split(":")[0] for w in out["warnings"]})
            draft = {"job": path.name, "question_id": q["id"], "seconds": round(time.time() - started, 1),
                     "length": size, "length_ok": ok, "warning_types": kinds, "warnings": out["warnings"],
                     "answer": out["answer"]}
            results["drafts"].append(draft)
            print(f"  {path.name:<30} q{q['id']}  {draft['seconds']:>5} s  {size:<24} "
                  f"length {'ok' if ok else 'BAD'}  warnings {kinds or 'none'}")
    drafts = results["drafts"]
    print()
    check(results, "length in range", f"{sum(d['length_ok'] for d in drafts)}/{len(drafts)}",
          all(d["length_ok"] for d in drafts), f"{len(drafts)}/{len(drafts)}")
    # Invented facts block; style problems (copied phrases, terms, cliches) are shown in the
    # review window for the user to edit - llama3.1:8b produces them in most essays.
    def blocking(d):
        return [w for w in d["warnings"] if w.startswith(("numbers not found", "contains a [placeholder]"))]
    check(results, "no invented numbers / placeholders", f"{sum(not blocking(d) for d in drafts)}/{len(drafts)}",
          all(not blocking(d) for d in drafts), f"{len(drafts)}/{len(drafts)}")
    clean = sum(not d["warnings"] for d in drafts)
    results["style_warnings_free"] = f"{clean}/{len(drafts)}"
    print(f"        (drafts with no style warnings: {clean}/{len(drafts)} - the rest are flagged in review)")
    code = finish("run_generate", results)
    print("Read the drafts in the result file before relying on them.")
    return code


if __name__ == "__main__":
    sys.exit(main())
