"""Fixtures for forms_automation tests.

Every store here lives in tmp_path and holds synthetic sentinel answers only -
never the real data/answers.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SENTINELS = ["SENTINEL-A1", "SENTINEL-A2", "SENTINEL-A3"]

QUESTIONS = [
    {"id": 1, "question": "What is your gender?", "answer": SENTINELS[0], "type": "select",
     "options": ["Male", "Female", "Decline"], "required": True, "times_seen": 1,
     "first_seen": "2026-10-01T10:00:00", "last_seen": "2026-10-01T10:00:00",
     "last_job": "https://job-boards.greenhouse.io/acme/jobs/1"},
    {"id": 2, "question": "Are you a veteran?", "answer": SENTINELS[1], "type": "select",
     "options": ["Yes", "No"], "required": False, "times_seen": 2,
     "first_seen": "2026-10-01T10:00:00", "last_seen": "2026-10-02T10:00:00",
     "last_job": "https://job-boards.greenhouse.io/acme/jobs/1"},
    {"id": 3, "question": "Why do you want this internship?", "answer": SENTINELS[2],
     "type": "textarea", "options": None, "required": True, "times_seen": 1,
     "first_seen": "2026-10-01T10:00:00", "last_seen": "2026-10-01T10:00:00",
     "last_job": "https://job-boards.greenhouse.io/acme/jobs/1"},
]


@pytest.fixture
def sentinels() -> list[str]:
    return list(SENTINELS)


@pytest.fixture
def answers_file(tmp_path) -> Path:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"questions": json.loads(json.dumps(QUESTIONS))}, indent=2), "utf-8")
    return path


@pytest.fixture
def qstore(answers_file):
    from forms_automation.db.question_store import QuestionStore

    return QuestionStore(answers_file)


@pytest.fixture
def hstore(tmp_path):
    from forms_automation.db.head_store import HeadStore

    return HeadStore(tmp_path / "heads.json")
