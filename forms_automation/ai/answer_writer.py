"""Write a fresh answer to an essay or "if other, please explain" question.

Used for questions in a "generate" head: the answer depends on the job, so it
is written for each application from the job post and your resume - never
copied from an earlier form. Earlier written answers of the same head are
passed as style examples only. Everything runs on the local Ollama.
"""

from __future__ import annotations

from pathlib import Path

from forms_automation.ai import cover_letter
from forms_automation.ai.cover_letter import BANNED_PHRASES, DEFAULT_MODEL, MAX_JD_CHARS

# Target length per field type; warnings_for flags answers far outside it.
WORDS = {"textarea": 115, "text": 30}
LENGTH = {"textarea": "one paragraph of 80-150 words", "text": "1-2 sentences"}

SYSTEM_PROMPT = f"""You write the candidate's answer to one question on a job application. Rules:
- Use ONLY facts from the candidate's resume. Never invent employers, job titles, degrees,
  certifications, years of experience, metrics or numbers.
- Only name tools, languages and technologies that appear in the resume.
- The job description lists what the company WANTS - it is not the candidate's experience.
  Never copy its sentences.
- Answer the question directly, in the first person, tied to this role and company.
- Plain, specific language. Never use any of these words or phrases:
  {", ".join(f'"{p}"' for p in BANNED_PHRASES)}.
- Plain text only: no greeting, no sign-off, no markdown, no placeholders like [Company].
  Output only the answer."""

_jobs: dict[str, dict] = {}


def job_for(url: str) -> dict:
    """The job post behind a form, fetched once per URL (shared with the cover letter)."""
    if url not in _jobs:
        _jobs[url] = cover_letter.greenhouse_job(url)
    return _jobs[url]


def build_prompt(question: str, kind: str, job: dict, resume: str,
                 prev_question: tuple[str, object] | None, examples: list[str]) -> str:
    kind = kind if kind in WORDS else "text"
    parts = [f"Question: {question}", f"Length: {LENGTH[kind]}.",
             f"Company: {job.get('company') or '(not named in the job post)'}", f"Role: {job.get('role', '')}"]
    if prev_question:
        label, value = prev_question
        shown = ", ".join(value) if isinstance(value, list) else value
        parts.append(f'This question follows "{label}", where the candidate chose: {shown}. '
                     "Explain that choice.")
    if examples:
        parts.append("Earlier answers by the candidate to similar questions (match their tone, "
                     "don't copy them):\n" + "\n---\n".join(examples[:2]))
    parts.append(f"\n=== CANDIDATE RESUME ===\n{resume}")
    parts.append(f"\n=== JOB DESCRIPTION ===\n{(job.get('description') or '')[:MAX_JD_CHARS]}")
    return "\n".join(parts)


def write_answer(question: str, kind: str, job: dict, profile: dict,
                 prev_question: tuple[str, object] | None = None, examples: list[str] | None = None,
                 model: str = DEFAULT_MODEL, resume: str | None = None, generate=None) -> dict:
    """{"answer": str, "warnings": list[str]}."""
    if resume is None:
        resume = cover_letter.resume_text(Path(profile["resume"]))
    prompt = build_prompt(question, kind, job, resume, prev_question, examples or [])
    text = (generate or cover_letter.generate)(model, SYSTEM_PROMPT, prompt, 0.6, echo=False).strip()
    words = WORDS.get(kind, WORDS["text"])
    job = {"company": "", "role": "", "description": "", **job}
    warnings = [w for w in cover_letter.warnings_for(text, resume, job, words, question)
                if not w.startswith("doesn't mention")  # an answer needn't name the company
                and not (kind != "textarea" and w.startswith("length is"))]  # short answers: 1-2 sentences
    return {"answer": text, "warnings": warnings}
