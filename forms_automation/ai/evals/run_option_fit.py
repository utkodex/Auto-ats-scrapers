"""Live eval: does Jev map a stored answer to the right form option, and never the opposite?

    python forms_automation/evals/run_option_fit.py

Data: option_fit.json - synthetic stored answers (not yours), the form's
options, the expected option (null = none fits) and, where one exists, the
option with the opposite meaning. Passes at >= 90% accuracy and 0 opposite picks.
Uses jev.fit_option as the form filler does, including the negation guard.
"""

from __future__ import annotations

import argparse
import sys

from harness import check, finish, load  # puts the repo root on sys.path

# isort: split

from forms_automation.ai import jev, llm


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    if not llm.available():
        print("Ollama isn't running or llama3.1:8b isn't pulled.")
        return 2
    rows = []
    for case in load("option_fit.json"):
        fit = jev.fit_option(case["stored_answer"], case["question"], case["options"])
        rows.append({**case, "got": fit.option, "confidence": fit.confidence,
                     "correct": fit.option == case["expected_option"],
                     "opposite_pick": bool(case.get("opposite")) and fit.option == case["opposite"]})
    results = {"settings": {"model": jev.CLASSIFIER_MODEL, "cases": len(rows)}, "cases": rows}
    misses = [r for r in rows if not r["correct"]]
    print(f"  misses ({len(misses)}):")
    for r in misses:
        print(f"    {r['stored_answer']!r:<30} -> {r['got']!r} (expected {r['expected_option']!r}, "
              f"confidence {r['confidence']:.2f})")
    print()
    acc = sum(r["correct"] for r in rows) / len(rows)
    opposite = sum(r["opposite_pick"] for r in rows)
    check(results, "accuracy", f"{acc:.1%}", acc >= 0.90, ">= 90%")
    check(results, "opposite-meaning picks", opposite, opposite == 0, "0")
    return finish("run_option_fit", results)


if __name__ == "__main__":
    sys.exit(main())
