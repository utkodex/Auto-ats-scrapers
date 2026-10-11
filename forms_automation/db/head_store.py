"""heads.json: classification heads that group reworded questions.

A head is one topic whose questions all share ONE answer ("What is your sex?"
and "What is your gender?" -> "gender"). Heads hold no answers - those stay in
answers.json, linked by each question's "head" field - so heads.json is safe
to put in prompts and logs.

    {
      "version": 1,
      "heads": [
        {
          "id": "veteran_status",
          "description": "Whether the candidate has served in the military",
          "examples": ["Are you a veteran/have you served in the military?"],
          "mode": "reuse",          # "generate": the LLM writes a fresh answer each time
          "auto_fill": true,        # false: always confirm with the user first
          "status": "approved",     # "pending" | "approved" | "rejected"
          "created_by": "llama",
          "created_at": "2026-10-10T12:00:00",
          "updated_at": "2026-10-10T12:00:00"
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from forms_automation.common.paths import HEADS_FILE
from forms_automation.db.question_store import SKIP, QuestionStore, now

NO_MATCHING_HEAD = "NO_MATCHING_HEAD"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
MODES = ("reuse", "generate")
STATUSES = ("pending", "approved", "rejected")


def valid_id(head_id: str) -> bool:
    return bool(ID_RE.fullmatch(head_id or "")) and head_id != NO_MATCHING_HEAD


class HeadStore:
    def __init__(self, path: Path | None = None):
        self.path = path or HEADS_FILE
        data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.heads: list[dict] = data.get("heads", [])

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "heads": self.heads}, indent=2, ensure_ascii=False) + "\n",
                       "utf-8")
        os.replace(tmp, self.path)

    def approved(self) -> list[dict]:
        return [h for h in self.heads if h["status"] == "approved"]

    def pending(self) -> list[dict]:
        return [h for h in self.heads if h["status"] == "pending"]

    def get(self, head_id: str | None) -> dict | None:
        return next((h for h in self.heads if h["id"] == head_id), None)

    def _check_id(self, head_id: str, current: str | None = None) -> None:
        if not valid_id(head_id):
            raise ValueError(f"bad head id {head_id!r}: use lower_snake_case, 2-41 characters")
        if head_id != current and self.get(head_id):
            raise ValueError(f"head id {head_id!r} already exists")

    def add(self, head: dict, status: str = "pending") -> dict:
        self._check_id(head["id"])
        mode = head.get("mode", "reuse")
        stamp = now()
        entry = {
            "id": head["id"],
            "description": head.get("description", ""),
            "examples": list(head.get("examples", [])),
            "mode": mode,
            "auto_fill": head.get("auto_fill", mode == "reuse"),
            "status": status,
            "created_by": head.get("created_by", "llama"),
            "created_at": stamp,
            "updated_at": stamp,
        }
        if head.get("flag"):
            entry["flag"] = head["flag"]
        self.heads.append(entry)
        self.save()
        return entry

    def update(self, head_id: str, **fields) -> dict:
        """Change fields of a head. Renaming one doesn't touch answers.json - the caller
        runs QuestionStore.rename_head too."""
        head = self.get(head_id)
        if head is None:
            raise KeyError(head_id)
        if "id" in fields:
            self._check_id(fields["id"], current=head_id)
        if fields.get("mode", head["mode"]) not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if fields.get("status", head["status"]) not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        head.update(fields)
        head["updated_at"] = now()
        self.save()
        return head

    def merge(self, src: str, dst: str, questions: QuestionStore) -> None:
        """Move every question of `src` into `dst`; keep `src` as rejected, for history."""
        source, target = self.get(src), self.get(dst)
        if source is None or target is None:
            raise KeyError(src if source is None else dst)
        questions.rename_head(src, dst)
        for example in source["examples"]:
            if example not in target["examples"]:
                target["examples"].append(example)
        target["updated_at"] = source["updated_at"] = now()
        source["status"] = "rejected"
        source["merged_into"] = dst
        self.save()

    def answer_for_head(self, head_id: str, questions: QuestionStore):
        """The newest answer given to any question of the head: SKIP if that newest one
        was skipped, None if nothing is answered yet or the head writes fresh answers."""
        head = self.get(head_id)
        if head is None or head["mode"] == "generate":
            return None
        answered = [q for q in questions.in_head(head_id) if q.get("answer") is not None]
        if not answered:
            return None
        newest = max(answered, key=lambda q: q.get("last_seen") or "")
        return SKIP if newest["answer"] == SKIP else newest["answer"]
