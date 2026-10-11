"""Live eval: does Jev put reworded questions under the right head, and stay out of wrong ones?

    python forms_automation/evals/run_classification.py [--threshold 0.75] [--sweep]

Data (no answers): heads_fixture.json (the 18 reviewed heads), paraphrases.json
(new wordings, expected head), out_of_scope.json (no head fits),
near_misses.json (look-alikes that must not join `wrong_head`).

Jev picks a head, then a strict check verifies it; only a verified pick on an
auto_fill head with confidence >= threshold fills without asking - anything
else opens the confirm window with the pick pre-selected.

Measures pick accuracy (Jev's choice on paraphrases), how many paraphrases fill
without asking (coverage), off-topic questions and near-misses that would be
auto-filled into a wrong head, the overall wrong auto-fill rate, the raw
NO_MATCHING_HEAD pick rate, a calibration table and latency. --sweep prints wrong
auto-fill and coverage per threshold.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from harness import check, finish, load  # puts the repo root on sys.path

# isort: split

from forms_automation.ai import jev, llm
from forms_automation.db.head_store import NO_MATCHING_HEAD

BUCKETS = [(0.0, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 1.01)]
SWEEP = [round(0.5 + 0.05 * i, 2) for i in range(10)]


def cases() -> list[dict]:
    out = [{**c, "set": "paraphrase"} for c in load("paraphrases.json")]
    out += [{**c, "set": "out_of_scope", "expected_head": NO_MATCHING_HEAD} for c in load("out_of_scope.json")]
    out += [{**c, "set": "near_miss"} for c in load("near_misses.json")]
    return out


def autofills(r: dict, auto: dict[str, bool], threshold: float) -> bool:
    """Would the form filler use this pick without asking?"""
    return (r["head"] != NO_MATCHING_HEAD and r["verified"] and auto.get(r["head"], False)
            and r["confidence"] >= threshold)


def wrong_autofill(results: list[dict], auto: dict[str, bool], threshold: float) -> int:
    return sum(1 for r in results if r["head"] != r["expected_head"] and autofills(r, auto, threshold))


def coverage(results: list[dict], auto: dict[str, bool], threshold: float) -> int:
    """Paraphrases filled correctly without asking."""
    return sum(1 for r in results if r["set"] == "paraphrase" and r["head"] == r["expected_head"]
               and autofills(r, auto, threshold))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--threshold", type=float, default=jev.CONFIDENCE_THRESHOLD)
    parser.add_argument("--sweep", action="store_true", help="print wrong auto-fill / coverage per threshold")
    args = parser.parse_args()
    if not llm.available():
        print("Ollama isn't running or llama3.1:8b isn't pulled.")
        return 2

    heads = load("heads_fixture.json")
    auto = {h["id"]: h["auto_fill"] for h in heads}
    rows = []
    all_cases = cases()
    print(f"classifying {len(all_cases)} questions against {len(heads)} heads...")
    for i, case in enumerate(all_cases, 1):
        jev.clear_cache()
        started = time.time()
        found = jev.classify_head(case["question"], case.get("type"), case.get("options"), heads)
        rows.append({"set": case["set"], "question": case["question"], "expected_head": case["expected_head"],
                     "wrong_head": case.get("wrong_head"), "head": found.head, "confidence": found.confidence,
                     "verified": found.verified, "reason": found.reason, "seconds": round(time.time() - started, 2)})
        if i % 20 == 0:
            print(f"  {i}/{len(all_cases)}")

    para = [r for r in rows if r["set"] == "paraphrase"]
    oos = [r for r in rows if r["set"] == "out_of_scope"]
    near = [r for r in rows if r["set"] == "near_miss"]
    times = sorted(r["seconds"] for r in rows)
    p95 = times[min(len(times) - 1, int(0.95 * len(times)))]
    t = args.threshold
    results = {"settings": {"model": jev.CLASSIFIER_MODEL, "threshold": t, "cases": len(rows)}, "cases": rows}

    print("\n  calibration (all cases):")
    calib = []
    for lo, hi in BUCKETS:
        inside = [r for r in rows if lo <= r["confidence"] < hi]
        right = sum(r["head"] == r["expected_head"] for r in inside)
        calib.append({"bucket": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(inside), "correct": right})
        print(f"    {lo:.2f}-{min(hi, 1):.2f}: {len(inside):>3} cases, "
              f"{(right / len(inside) if inside else 0):.0%} correct")
    results["calibration"] = calib

    sweep = [{"threshold": s, "wrong_autofill": wrong_autofill(rows, auto, s),
              "coverage": coverage(rows, auto, s)} for s in SWEEP]
    results["sweep"] = sweep
    if args.sweep:
        print("\n  threshold  wrong auto-fill        paraphrases filled without asking")
        for s in sweep:
            print(f"    {s['threshold']:.2f}     {s['wrong_autofill']:>3} ({s['wrong_autofill'] / len(rows):.1%})"
                  f"           {s['coverage']:>3}/{len(para)}")
        ok = [s for s in sweep if s["wrong_autofill"] / len(rows) <= 0.02]
        results["recommended_threshold"] = ok[0]["threshold"] if ok else None
        print(f"\n  lowest threshold with wrong auto-fill <= 2%: {results['recommended_threshold']}")

    wrong = [r for r in rows if r["head"] != r["expected_head"]]
    print(f"\n  misses ({len(wrong)}):")
    for r in wrong:
        print(f"    [{r['set']}] {r['question'][:60]:<60} expected {r['expected_head']:<20} "
              f"got {r['head']} ({r['confidence']:.2f}, {'VERIFIED' if r['verified'] else 'asks'})")

    print()
    acc = sum(r["head"] == r["expected_head"] for r in para) / len(para)
    kept_out = sum(not autofills(r, auto, t) for r in oos) / len(oos)
    merges = sum(r["head"] == r["wrong_head"] and autofills(r, auto, t) for r in near) / len(near)
    wrong_rate = wrong_autofill(rows, auto, t) / len(rows)
    covered = coverage(rows, auto, t)
    results["raw_no_match_recall"] = sum(r["head"] == NO_MATCHING_HEAD for r in oos) / len(oos)
    results["coverage"] = f"{covered}/{len(para)}"
    print(f"  paraphrases filled without asking: {covered}/{len(para)} (the rest ask, pick pre-selected)")
    print(f"  raw NO_MATCHING_HEAD picks on off-topic questions: {results['raw_no_match_recall']:.0%}\n")
    check(results, "paraphrase pick accuracy", f"{acc:.1%}", acc >= 0.90, ">= 90%")
    check(results, "off-topic never auto-filled", f"{kept_out:.1%}", kept_out >= 0.80, ">= 80%")
    check(results, "near-miss auto-filled wrong", f"{merges:.1%}", merges <= 0.10, "<= 10%")
    check(results, f"wrong auto-fill at {t}", f"{wrong_rate:.1%}", wrong_rate <= 0.02, "<= 2%")
    check(results, "p95 latency (s)", p95, p95 <= 15, "<= 15")
    results["p50_seconds"] = statistics.median(times)
    return finish("run_classification", results)


if __name__ == "__main__":
    sys.exit(main())
