"""Live eval: does Llama group today's questions into the right heads, the same way every run?

    python forms_automation/evals/run_head_creation.py --runs 3 [--prompt new|tested]

Uses evals/data/db_questions.json (id, question, type, company - no answers).
Passes when every run groups 18/18 correctly, all runs agree, no question is
missing or doubled, every head id holding a company name is flagged for renaming,
and each run takes <= 120 s.
"""

from __future__ import annotations

import argparse
import sys
import time

from harness import check, finish, load, sha1  # puts the repo root on sys.path

# isort: split

from forms_automation.ai import head_creator as hc
from forms_automation.ai import llm

MAX_SECONDS = 120


def partition(heads: list[dict]) -> dict[int, frozenset]:
    """question id -> the set of question ids sharing its head."""
    return {qid: frozenset(h["question_ids"]) for h in heads for qid in h["question_ids"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prompt", choices=sorted(hc.PROMPTS), default=hc.PROMPT)
    args = parser.parse_args()
    if not llm.available():
        print("Ollama isn't running or llama3.1:8b isn't pulled.")
        return 2

    questions = load("db_questions.json")
    entries = [{"id": q["id"], "question": q["question"], "type": q["type"],
                "last_job": f"https://job-boards.greenhouse.io/{q['company']}/jobs/1" if q["company"] else None}
               for q in questions]
    expected = partition([{"question_ids": [q["id"] for q in questions if q["expected_head"] == h]}
                          for h in {q["expected_head"] for q in questions}])
    expected_mode = {q["id"]: q["expected_mode"] for q in questions}
    companies = hc.company_words(entries)
    by_id = {e["id"]: e for e in entries}

    results = {"settings": {"model": llm.DEFAULT_MODEL, "prompt": args.prompt,
                            "prompt_sha1": sha1(hc.PROMPTS[args.prompt]), "runs": args.runs,
                            "temperature": 0}, "runs": []}
    print(f"head creation x{args.runs}, prompt '{args.prompt}' ({results['settings']['prompt_sha1']})")
    for n in range(1, args.runs + 1):
        calls = []

        def chat(*a, _calls=calls, **kw):
            _calls.append(1)
            return llm.chat_json(*a, **kw)

        started = time.time()
        heads, errors = hc.group(entries, [], args.prompt, chat)
        seconds = round(time.time() - started, 1)
        got = partition(heads)
        grouped = sum(got.get(qid) == members for qid, members in expected.items())
        modes = sum(hc.mode_for([by_id[q] for q in h["question_ids"]]) == expected_mode[qid]
                    for h in heads for qid in h["question_ids"] for q in [qid])
        run = {"run": n, "seconds": seconds, "calls": len(calls), "grouping": grouped, "mode": modes,
               "errors": errors, "unplaced": [h["id"] for h in heads if h.get("flag") == "unplaced"],
               "company_ids": [h["id"] for h in heads if hc.has_company(h["id"], companies)],
               "flagged": [h["id"] for h in heads if h.get("flag") == "rename"],
               "heads": [{"id": h["id"], "description": h["description"], "question_ids": h["question_ids"]}
                         for h in heads]}
        results["runs"].append(run)
        print(f"  run {n}: {seconds:>5} s  {len(heads)} heads  grouping {grouped}/{len(expected)}  "
              f"mode {modes}/{len(expected)}  calls {len(calls)}  company ids {run['company_ids']}")

    runs = results["runs"]
    total = len(expected)
    shapes = {tuple(sorted((h["id"], tuple(sorted(h["question_ids"]))) for h in r["heads"])) for r in runs}
    renames = {q["expected_head"]: next((h["id"] for h in runs[0]["heads"] if q["id"] in h["question_ids"]), None)
               for q in questions}
    results["id_differences"] = {exp: got for exp, got in renames.items() if exp != got}
    print("\n  head ids that differ from the reviewed names:", results["id_differences"] or "none")
    print()
    check(results, "grouping 18/18 in every run", f"{sum(r['grouping'] == total for r in runs)}/{len(runs)}",
          all(r["grouping"] == total for r in runs), f"{len(runs)}/{len(runs)}")
    check(results, "identical across runs", len(shapes) == 1, len(shapes) == 1, "True")
    check(results, "missing / duplicate ids", sum(len(r["errors"]) + len(r["unplaced"]) for r in runs),
          all(not r["errors"] and not r["unplaced"] for r in runs), "0")
    # llama3.1:8b ignores "no company names"; validate() must flag every such id for renaming.
    unflagged = [h for r in runs for h in r["company_ids"] if h not in r["flagged"]]
    check(results, "company-name ids not flagged", len(unflagged), not unflagged, "0")
    print(f"        (company-name ids seen: {sum(len(r['company_ids']) for r in runs)}, renamed in review)")
    check(results, "max seconds per run", max(r["seconds"] for r in runs),
          all(r["seconds"] <= MAX_SECONDS for r in runs), f"<= {MAX_SECONDS}")
    check(results, "mode by rules (run 1)", f"{runs[0]['mode']}/{total}", runs[0]["mode"] == total, f"{total}/{total}")
    return finish("run_head_creation", results)


if __name__ == "__main__":
    sys.exit(main())
