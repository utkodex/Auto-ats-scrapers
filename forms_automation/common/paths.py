"""Where forms_automation keeps its files.

Code lives in the packages (common/, db/, ai/, automation/, greenhouse/); everything
personal - profile, saved answers, topics, cover letters - lives in data/ (gitignored).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # forms_automation/
REPO = ROOT.parent
DATA = ROOT / "data"

PROFILE_FILE = DATA / "profile.json"
ANSWERS_FILE = DATA / "answers.json"
HEADS_FILE = DATA / "heads.json"
COVER_LETTERS = DATA / "cover_letters"
BROWSER_PROFILE = ROOT / ".chrome-profile"   # Chrome logins/cookies kept between runs
