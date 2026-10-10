"""answers.json: application questions that profile.json can't answer.

Every such question is recorded the first time a form asks it, with its
options and the job it came from. Its "answer" stays null until you give one
(in the terminal during a run, with questions.py, or by editing the file),
and is reused on every form that asks the same question.

    {
      "questions": [
        {
          "id": 4,
          "question": "Are you a veteran/have you served in the military?",
          "answer": "No military service",
          "type": "select",
          "options": ["Active Reserve", "...", "No military service"],
          "required": true,
          "times_seen": 2,
          "first_seen": "2026-10-09T12:00:00",
          "last_seen": "2026-10-09T12:30:00",
          "last_job": "https://job-boards.greenhouse.io/reddit/jobs/8250389"
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE_FILE = HERE / "answers.json"

# Answer meaning "never fill this question".
SKIP = "__skip__"


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class QuestionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or STORE_FILE
        data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
            # Older flat format: {"normalised question": answer}
            data = {"questions": [
                {"id": i, "question": key, "answer": answer, "type": None, "options": None,
                 "required": False, "times_seen": 0, "first_seen": now(), "last_seen": now(),
                 "last_job": None}
                for i, (key, answer) in enumerate((data or {}).items(), 1)
            ]}
        self.questions: list[dict] = data["questions"]

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"questions": self.questions}, indent=2, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, self.path)  # never leave a half-written file behind

    def get(self, label: str) -> dict | None:
        key = norm(label)
        return next((q for q in self.questions if norm(q["question"]) == key), None)

    def by_id(self, qid: int) -> dict | None:
        return next((q for q in self.questions if q["id"] == qid), None)

    def answer_for(self, label: str):
        """The stored answer, or None when the question is new or unanswered."""
        entry = self.get(label)
        return entry["answer"] if entry else None

    def record(self, label: str, kind: str | None = None, options: list[str] | None = None,
               required: bool = False, job: str | None = None) -> None:
        """Note that a form asked this question (adding it if new)."""
        entry = self.get(label)
        if entry is None:
            entry = {
                "id": max((q["id"] for q in self.questions), default=0) + 1,
                "question": label, "answer": None, "type": None, "options": None,
                "required": False, "times_seen": 0, "first_seen": now(),
                "last_seen": now(), "last_job": None,
            }
            self.questions.append(entry)
        entry["question"] = label
        entry["type"] = kind or entry["type"]
        entry["options"] = options or entry["options"]
        entry["required"] = entry["required"] or required
        entry["times_seen"] += 1
        entry["last_seen"] = now()
        entry["last_job"] = job or entry["last_job"]
        self.save()

    def update_options(self, label: str, options: list[str]) -> None:
        entry = self.get(label)
        if entry:
            entry["options"] = options
            self.save()

    def set_answer(self, label: str, answer) -> None:
        entry = self.get(label)
        if entry is None:
            self.record(label)
            entry = self.get(label)
            entry["times_seen"] = 0
        entry["answer"] = answer
        self.save()

    def delete(self, qid: int) -> None:
        self.questions = [q for q in self.questions if q["id"] != qid]
        self.save()

    def rows(self, unanswered_only: bool = False, search: str | None = None) -> list[dict]:
        rows = [
            q for q in self.questions
            if (not unanswered_only or q["answer"] is None)
            and (not search or search.lower() in q["question"].lower())
        ]
        return sorted(rows, key=lambda q: (not q["required"], -q["times_seen"], q["id"]))
