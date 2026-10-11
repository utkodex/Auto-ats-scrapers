"""Check that no stored answer leaked into eval data or result files.

    python forms_automation/evals/check_privacy.py

Prints counts only - never the answers. An answer that is also part of a
question's own wording (e.g. "I agree" in a consent question) isn't a leak and
is ignored, and so are the public job posts in data/jobs/ (downloaded from
Greenhouse, they can contain a one-word answer like a city by chance).
An answer that is exactly a standard option listed in the eval data (e.g.
"Prefer not to say") is counted separately: every option is listed there, so
it doesn't reveal which one you chose. Exit 1 if any other answer is found.
"""

from __future__ import annotations

import json
import sys

from harness import DATA, RESULTS  # puts the repo root on sys.path

# isort: split

from forms_automation.db.question_store import SKIP, STORE_FILE


def main() -> int:
    questions = json.loads(STORE_FILE.read_text("utf-8"))["questions"]
    wording = " ".join(q["question"].lower() for q in questions)
    values = set()
    for q in questions:
        answer = q.get("answer")
        for g in q.get("generated") or []:
            values.add(str(g.get("answer", "")).lower())
        for v in answer if isinstance(answer, list) else [answer]:
            if isinstance(v, str) and len(v) > 3 and v != SKIP:
                values.add(v.lower())
    values = {v for v in values if len(v) > 3 and v not in wording}
    standard = set()
    for path in DATA.glob("*.json"):
        for case in json.loads(path.read_text("utf-8")):
            standard.update(str(o).lower() for o in case.get("options") or [])
            if case.get("expected_option"):
                standard.add(case["expected_option"].lower())
    files = [p for folder in (DATA, RESULTS) if folder.exists() for p in folder.rglob("*")
             if p.is_file() and (DATA / "jobs") not in p.parents]
    texts = [p.read_text("utf-8", errors="ignore").lower() for p in files]
    found = {v for v in values if any(v in t for t in texts)}
    options = found & standard
    leaks = len(found - options)
    print(f"files {len(files)}  answers checked {len(values)}  standard options {len(options)}  leaks {leaks}")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
