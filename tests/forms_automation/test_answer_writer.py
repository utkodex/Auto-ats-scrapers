from __future__ import annotations

from forms_automation.ai import answer_writer as aw

JOB = {"company": "Acme", "role": "ML Intern", "description": "We want Python and SQL skills. " * 5}
RESUME = "Built a search tool in Python at Example University. Used SQL for analytics."


class Gen:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, model, system, prompt, temperature, echo=True):
        self.calls.append({"model": model, "system": system, "prompt": prompt,
                           "temperature": temperature, "echo": echo})
        return self.text


def test_prompt_has_question_job_resume_and_length():
    gen = Gen("I built a search tool in Python and want to grow it into production ML work.")
    out = aw.write_answer("What do you want to gain?", "text", JOB, {}, resume=RESUME, generate=gen)
    call = gen.calls[0]
    assert call["temperature"] == 0.6 and call["echo"] is False
    assert "Question: What do you want to gain?" in call["prompt"]
    assert "1-2 sentences" in call["prompt"]
    assert RESUME in call["prompt"] and "Company: Acme" in call["prompt"]
    assert out["answer"].startswith("I built")


def test_if_other_includes_previous_choice_and_examples():
    gen = Gen("x")
    aw.write_answer("If you selected other, please include more details below.", "text", JOB, {},
                    prev_question=("How did you hear about us?", "Other"),
                    examples=["first essay", "second essay", "third essay"], resume=RESUME, generate=gen)
    prompt = gen.calls[0]["prompt"]
    assert 'follows "How did you hear about us?", where the candidate chose: Other' in prompt
    assert "first essay" in prompt and "second essay" in prompt and "third essay" not in prompt


def test_warnings_flag_problems_but_not_missing_company():
    long_text = ("I am passionate about this. " * 40).strip()
    out = aw.write_answer("Why?", "textarea", JOB, {}, resume=RESUME, generate=Gen(long_text))
    assert any("length" in w for w in out["warnings"])
    assert any("passionate" in w for w in out["warnings"])
    assert not any(w.startswith("doesn't mention") for w in out["warnings"])


def test_job_for_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(aw.cover_letter, "greenhouse_job", lambda url: calls.append(url) or {"url": url})
    aw._jobs.clear()
    aw.job_for("https://job-boards.greenhouse.io/acme/jobs/1")
    aw.job_for("https://job-boards.greenhouse.io/acme/jobs/1")
    assert len(calls) == 1
    aw._jobs.clear()
