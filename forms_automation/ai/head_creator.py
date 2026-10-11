"""Create classification heads (heads.json) from questions in answers.json with Llama.

Llama only sees question id, text and type - never a stored answer. It groups
questions that share ONE answer into a head and names it; code then checks the
result (every question placed exactly once, valid ids, no company names) and
sets each head's mode with fixed rules. New heads start as "pending" until the
user approves them (heads.py review).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from forms_automation.ai import llm
from forms_automation.common.gui import company_of
from forms_automation.db.head_store import ID_RE, NO_MATCHING_HEAD, HeadStore
from forms_automation.db.question_store import QuestionStore

# Prompt B from my_docs/llama-heads-report.html plus two lines (company names,
# two-part questions). evals/run_head_creation.py checks it: grouping stays 18/18
# and the two-part line works (availability_dates), but llama3.1:8b still writes
# company names into ids, so validate() flags those for renaming in the review
# GUI. If grouping ever drops below the tested text, set PROMPT = "tested".
SYSTEM_PROMPT = """You organise job-application form questions into classification heads.
A head is a topic: questions with different wording but the same meaning share a head
(e.g. "What is your sex?" and "What is your gender?" -> head "gender").
Rules:
- Every question id must be in exactly one head.
- MOST IMPORTANT: a head stores ONE answer. Two questions may share a head ONLY if the
  same person would give the same answer to both. Gender, sexual orientation, ethnicity
  and transgender status have different answers, so they are separate heads.
  University, degree, graduation date, start date and location are separate heads.
  When unsure, make a separate head. One question per head is fine.
- Head ids are short snake_case.
- Never put a company name in a head id.
- If a question asks for two things, name the head after both (e.g. availability_dates).
- description: one plain sentence saying what the head is about.
{existing_heads_block}
Return JSON only."""

# The exact text that scored 18/18 grouping in 10/10 runs (mode is ignored - rules set it).
PROMPT_B_TESTED = """You organise job-application form questions into classification heads.
A head is a topic: questions with different wording but the same meaning share a head
(e.g. "What is your sex?" and "What is your gender?" -> head "gender").
Rules:
- Every question id must be in exactly one head.
- MOST IMPORTANT: a head stores ONE answer. Two questions may share a head ONLY if the
  same person would give the same answer to both. Gender, sexual orientation, ethnicity
  and transgender status have different answers, so they are separate heads.
  University, degree, graduation date, start date and location are separate heads.
  When unsure, make a separate head. One question per head is fine.
- Head ids are short snake_case.
- description: one plain sentence saying what the head is about.
- mode: "reuse" if the same answer can be copied to any company's form,
  "generate" if the answer must be written fresh per job (essays, motivation, "if other, explain" follow-ups).
{existing_heads_block}
Return JSON only."""

PROMPTS = {"new": SYSTEM_PROMPT, "tested": PROMPT_B_TESTED}
PROMPT = "new"

_HEAD_ITEM = {
    "id": {"type": "string"},
    "description": {"type": "string"},
    "question_ids": {"type": "array", "items": {"type": "integer"}},
}
SCHEMA = {"type": "object", "required": ["heads"],
          "properties": {"heads": {"type": "array", "items": {
              "type": "object", "required": ["id", "description", "question_ids"],
              "properties": _HEAD_ITEM}}}}
SCHEMAS = {
    "new": SCHEMA,
    "tested": {"type": "object", "required": ["heads"],
               "properties": {"heads": {"type": "array", "items": {
                   "type": "object", "required": ["id", "description", "mode", "question_ids"],
                   "properties": {**_HEAD_ITEM, "mode": {"type": "string", "enum": ["reuse", "generate"]}}}}}},
}

BATCH = 25
MAX_EXAMPLES = 5
IF_OTHER = re.compile(r"^\s*if (you )?(selected|chose|answered) .*other|please (explain|specify|elaborate)", re.I)


@dataclass
class BuildResult:
    created: list[str] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)
    needs_review: bool = False
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0


def question_items(entries: list[dict]) -> list[dict]:
    """What Llama may see of a question. Built key by key so an answer can't slip in."""
    return [{"id": q["id"], "question": q["question"], "type": q.get("type")} for q in entries]


def existing_block(heads: list[dict]) -> str:
    if not heads:
        return ""
    lines = [f"- {h['id']}: {h['description']}" for h in heads]
    return ("Existing heads (reuse one of these ids if a question has the same answer; "
            "otherwise create a new head):\n" + "\n".join(lines))


def system_prompt(existing: list[dict], prompt: str | None = None) -> str:
    return PROMPTS[prompt or PROMPT].format(existing_heads_block=existing_block(existing))


def mode_for(member_questions: list[dict]) -> str:
    """generate: essays and "if other, explain" follow-ups get a fresh answer per job."""
    for q in member_questions:
        if q.get("type") == "textarea" or IF_OTHER.search(q.get("question") or ""):
            return "generate"
    return "reuse"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "h_" + slug
    slug = slug[:41].rstrip("_")
    return slug if ID_RE.fullmatch(slug) else "head"


def company_words(entries: list[dict]) -> set[str]:
    """Words that are probably company names: the job board slug of each question's
    last job, and capitalised words that don't start a sentence."""
    words: set[str] = set()
    for q in entries:
        company = company_of(q.get("last_job"))
        words.update(w for w in re.split(r"[^a-z0-9]+", company.lower()) if len(w) > 2)
        tokens = re.findall(r"[\w'&-]+|[.?!:]", q.get("question") or "")
        for prev, word in zip([".", *tokens], tokens, strict=False):
            if prev not in ".?!:" and word[:1].isupper() and len(word) > 2 and not word.isupper():
                words.add(re.sub(r"[^a-z0-9]", "", word.lower()))
    return words


def has_company(head_id: str, companies: set[str]) -> bool:
    return any(part in companies for part in head_id.split("_"))


def validate(raw: dict, input_ids: set[int], existing_ids: set[str],
             companies: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Clean Llama's heads. Returns (heads, errors); errors are fed back on a retry.
    Each head: {id, description, question_ids, join (an existing id), flag?}."""
    errors: list[str] = []
    placed: set[int] = set()
    heads: list[dict] = []
    used: set[str] = set()
    for item in raw.get("heads") or []:
        if not isinstance(item, dict):
            continue
        qids = []
        for qid in item.get("question_ids") or []:
            if qid not in input_ids:
                errors.append(f"question id {qid} doesn't exist")
            elif qid in placed:
                errors.append(f"question id {qid} is in more than one head")
            else:
                placed.add(qid)
                qids.append(qid)
        if not qids:
            continue
        head_id = slugify(str(item.get("id", "")))
        join = head_id in existing_ids
        if not join:
            base, n = head_id, 2
            while head_id in used or head_id in existing_ids or head_id == NO_MATCHING_HEAD:
                head_id = f"{base[:38]}_{n}"
                n += 1
            used.add(head_id)
        head = {"id": head_id, "description": str(item.get("description", "")).strip(),
                "question_ids": qids, "join": join}
        if not join and companies and has_company(head_id, companies):
            head["flag"] = "rename"
        heads.append(head)
    for qid in sorted(input_ids - placed):
        errors.append(f"question id {qid} is missing")
    return heads, errors


def _ask(items: list[dict], existing: list[dict], errors: list[str] | None, prompt: str | None,
         chat=None) -> dict:
    chat = chat or llm.chat_json
    user = json.dumps(items, ensure_ascii=False, indent=1)
    if errors:
        user += ("\n\nYour previous answer had these problems; fix them:\n- " + "\n- ".join(errors))
    return chat(system_prompt(existing, prompt), user, SCHEMAS[prompt or PROMPT])


def group(entries: list[dict], existing: list[dict], prompt: str | None = None,
          chat=None) -> tuple[list[dict], list[str]]:
    """Ask Llama to place `entries` in heads, retrying once if the result is broken.
    Questions still unplaced after the retry get a head each (from their own text)."""
    items = question_items(entries)
    input_ids = {q["id"] for q in entries}
    existing_ids = {h["id"] for h in existing}
    companies = company_words(entries)
    heads, errors = validate(_ask(items, existing, None, prompt, chat), input_ids, existing_ids, companies)
    if errors:
        retry, retry_errors = validate(_ask(items, existing, errors, prompt, chat),
                                       input_ids, existing_ids, companies)
        if len(retry_errors) <= len(errors):
            heads, errors = retry, retry_errors
    placed = {qid for h in heads for qid in h["question_ids"]}
    taken = existing_ids | {h["id"] for h in heads}
    for q in entries:
        if q["id"] in placed:
            continue
        base = head_id = slugify(" ".join((q["question"] or "").split()[:5]))
        n = 2
        while head_id in taken:
            head_id = f"{base[:38]}_{n}"
            n += 1
        taken.add(head_id)
        heads.append({"id": head_id, "description": q["question"], "question_ids": [q["id"]],
                      "join": False, "flag": "unplaced"})
    return heads, errors


def build_initial_heads(questions: QuestionStore, heads: HeadStore, batch: int = BATCH,
                        prompt: str | None = None, chat=None) -> BuildResult:
    """Put every question without a head into a (pending) head."""
    started = time.time()
    result = BuildResult()
    todo = [q for q in questions.questions if not q.get("head")]
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        by_id = {q["id"]: q for q in chunk}
        existing = [h for h in heads.heads if h["status"] != "rejected"]
        grouped, errors = group(chunk, existing, prompt, chat)
        if errors:
            result.needs_review = True
            result.errors += errors
        for h in grouped:
            members = [by_id[qid] for qid in h["question_ids"]]
            if h["join"]:
                target = heads.get(h["id"])
                examples = target["examples"] + [m["question"] for m in members
                                                  if m["question"] not in target["examples"]]
                heads.update(h["id"], examples=examples)
                result.joined.append(h["id"])
            else:
                if h.get("flag") == "unplaced":
                    result.needs_review = True
                heads.add({"id": h["id"], "description": h["description"],
                           "examples": [m["question"] for m in members], "mode": mode_for(members),
                           "flag": h.get("flag"), "created_by": "llama"})
                result.created.append(h["id"])
            for m in members:
                questions.set_head(m["question"], h["id"], "llama")
    result.seconds = round(time.time() - started, 1)
    return result


def propose_head(question: dict, heads: HeadStore, prompt: str | None = None, chat=None) -> dict:
    """A draft head for one question no approved head fits (not saved).
    {"join": True, "id": ...} when Llama files it under an existing head instead."""
    entry = {"id": question.get("id") or 1, "question": question["question"],
             "type": question.get("type"), "last_job": question.get("last_job")}
    grouped, _ = group([entry], heads.approved(), prompt, chat)
    h = grouped[0]
    if h["join"]:
        return {"id": h["id"], "join": True}
    draft = {"id": h["id"], "description": h["description"], "examples": [question["question"]],
             "mode": mode_for([entry]), "join": False}
    if h.get("flag"):
        draft["flag"] = h["flag"]
    return draft
