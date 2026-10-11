"""Shared helpers for the live evals: paths, result files, threshold reporting.

Result files hold settings, per-case outputs and metrics - question text and
synthetic data only, never a stored answer.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

EVALS = Path(__file__).resolve().parent
DATA = EVALS / "data"
RESULTS = EVALS / "results"
sys.path.insert(0, str(EVALS.parents[2]))  # the repo root: makes the forms_automation package importable


def load(name: str):
    return json.loads((DATA / name).read_text("utf-8"))


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def check(results: dict, name: str, value, ok: bool, target: str) -> None:
    results.setdefault("checks", []).append({"name": name, "value": value, "target": target, "passed": ok})
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} {value!s:<14} target {target}")


def finish(script: str, results: dict) -> int:
    results["passed"] = all(c["passed"] for c in results.get("checks", []))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{script}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(f"\n{'PASSED' if results['passed'] else 'FAILED'} - results in {out}")
    return 0 if results["passed"] else 1
