# Greenhouse form automation

Fills Greenhouse job applications in a visible Chrome window. It **never
submits**: you review the form, solve any captcha, and click Submit yourself.

## Layout

```
forms_automation/
  greenhouse/   Greenhouse only: apply.py (run this), filler.py (the form's fields),
                cover_letter_task.py, security_code.py
  automation/   rule-based filling for any platform: answers.py (profile, rules,
                asking you), resolver.py (the answer order below), matching.py
  ai/           local LLM: llm.py, jev.py (topic matching, option fitting),
                head_creator.py, answer_writer.py, cover_letter.py, evals/
  db/           answers.json and heads.json: question_store.py, head_store.py,
                and their commands questions.py and heads.py
  common/       gui.py (pop-up windows), email_otp.py (Gmail codes), paths.py
  data/         your files: profile.json, answers.json, heads.json, backups,
                cover_letters/ (gitignored)
```

Another job platform goes in its own folder next to `greenhouse/`: a filler that
subclasses `automation.resolver.Resolver` gets the profile, saved answers, topics,
Jev and the pop-up windows for free.

## Setup

```powershell
cp forms_automation/profile.example.json forms_automation/data/profile.json   # then edit it
```

Uses your installed Google Chrome. If Chrome isn't installed, run
`.venv\Scripts\python -m playwright install chromium` once.

## Run

```powershell
.venv\Scripts\python forms_automation\greenhouse\apply.py https://job-boards.greenhouse.io/striveworks/jobs/8012592003
.venv\Scripts\python forms_automation\greenhouse\apply.py URL1 URL2 URL3
.venv\Scripts\python forms_automation\greenhouse\apply.py --file urls.txt
```

Works with `job-boards.greenhouse.io`, `boards.greenhouse.io`, embed links
(`embed/job_app?for=...&token=...`), and company career pages that embed a
Greenhouse form (`?gh_jid=...`). Each job opens in its own tab; click **Next
job** in the review window to move on. **Close browser** on the last job closes
Chrome, so submit first.

## Where answers come from

1. `profile.json`: your details (name, email, phone, resume, education).
2. `answers.json`: questions you've answered before, and the answer of the
   question's **topic** (head) - see [Topics](#topics-reworded-questions).
3. `profile.json` `rules`: regex on the question → answer, for common questions.
4. **Jev** (local LLM): a reworded question ("What is your sex?" when you've
   answered "What is your gender?") is matched to a topic and gets that topic's
   answer. Jev double-checks every match with a second, strict question; unless
   that check agrees (or when the topic is set to always ask), a window asks you
   to confirm, with Jev's pick pre-selected. A question that fits no topic gets a new topic drafted for
   you to approve.
5. A pop-up window: anything else is **recorded in `answers.json`** (question,
   options, required, which job) and asked in a window that stays on top of
   Chrome. Pick an option (long lists have a search box) or type your own
   answer, then:
   - **Save answer** (Enter): stored and reused on every form that asks it.
   - **Leave blank for now** (Esc): stays unanswered and is asked again next time.
   - **Never fill this**: always skipped.

   An optional follow-up ("If yes, ...", "If you selected Other, ...") isn't
   asked when the answer just before it isn't Yes / Other.

   After each job a window lists any required fields still empty. Submit in
   Chrome, then click **Next job** (or **Close browser** on the last one).
   Add `--terminal` to answer in the terminal instead.

Add a rule to `profile.json` to answer a whole family of questions, for example
`{"match": "notice period", "answer": "30 days"}`.

## The unknown-questions file (`answers.json`)

Collect questions first, answer them in one go, then apply:

```powershell
# 1. fill what's known, record the rest without stopping to ask
.venv\Scripts\python forms_automation\greenhouse\apply.py --no-ask --file urls.txt

# 2. see and answer what was collected
.venv\Scripts\python forms_automation\db\questions.py list --unanswered
.venv\Scripts\python forms_automation\db\questions.py show 12        # full text + options
.venv\Scripts\python forms_automation\db\questions.py answer 12 2    # option number, or text
.venv\Scripts\python forms_automation\db\questions.py answer         # walk through all unanswered

# ...or edit them in Excel
.venv\Scripts\python forms_automation\db\questions.py export questions.csv
.venv\Scripts\python forms_automation\db\questions.py import questions.csv

# 3. rerun - answered questions are now filled automatically
```

Other commands: `skip ID` (never fill), `clear ID` (forget the answer),
`delete ID`, `list --search visa`.

You can also edit `answers.json` directly: each question has an `"answer"`
field (`null` = not answered yet, `"__skip__"` = never fill).

`data/` (profile, saved answers, topics, cover letters) and `.chrome-profile/` hold
personal data and are gitignored.

## Topics (reworded questions)

Companies word the same question differently. `heads.json` groups questions
that share **one answer** into a topic ("head"): `veteran_status`, `ethnicity`,
... Each question in `answers.json` names its topic in `"head"`, so a new
wording reuses the newest answer given to any question of the topic.

```powershell
# once: Llama groups your saved questions into topics, then you review them
.venv\Scripts\python forms_automation\db\heads.py build
.venv\Scripts\python forms_automation\db\heads.py review     # pending topics (or --terminal)
.venv\Scripts\python forms_automation\db\heads.py list       # topics and their question ids
```

In review you can **approve**, **edit** (name, description, examples, answer
mode, auto-fill), **merge** into another topic, or **reject**. Only approved
topics are used. Llama only ever sees question text - never your answers.

- **Answer mode** `reuse` copies the topic's answer. `generate` (essays and
  "if you selected other, please explain") has the local LLM write a fresh
  answer from the job post and your resume for every job; you review and edit
  it before it's filled, and every version is kept under `"generated"`.
- **Auto-fill** off: always confirm the topic first, even when Jev is sure.
- When a saved answer matches none of a form's options ("Never served" vs
  "I am not a protected veteran"), Jev picks the option with the same meaning,
  never one with the opposite meaning; otherwise you're asked.

Flags: `--no-classify` matches by exact wording only (today's behaviour without
topics); `--threshold 0.9` asks more often (Jev's confidence needed to fill
without asking, default 0.75). Without Ollama running, topics still fill
exact-wording matches and everything else is asked as before.

Checks for the LLM steps live in `forms_automation/ai/evals/` (they need Ollama):
`run_head_creation.py --runs 3`, `run_classification.py --sweep`,
`run_option_fit.py`, `run_generate.py`, and `check_privacy.py` (no saved answer
in eval files). Results go to `ai/evals/results/` (gitignored).

## Cover letters (local LLM)

Cover letters are written by a local Ollama model - nothing leaves this PC.
One-time setup: `ollama pull llama3.1:8b` and keep the Ollama app running.

**In the form automation** (automatic): when a job has a Cover Letter field,
`greenhouse/apply.py` writes a letter in the background while it fills the form,
then opens a review window with the letter (editable) and the automatic checks:

- **Upload as PDF** (Ctrl+Enter): saves your version as a PDF and attaches it.
- **Regenerate**: writes a new draft.
- **Skip cover letter**: nothing is uploaded (the text is still saved).

```powershell
.venv\Scripts\python forms_automation\greenhouse\apply.py URL                          # letters on by default
.venv\Scripts\python forms_automation\greenhouse\apply.py URL --no-cover-letter        # turn off
.venv\Scripts\python forms_automation\greenhouse\apply.py URL --cover-letter-model gemma3:12b --cover-letter-words 250
```

A fixed `"cover_letter": "path/to/letter.pdf"` in `profile.json` is uploaded instead
of writing one. With `--no-ask`, letters are uploaded without review (marked
`uploaded-unreviewed` in the log) - read them before you submit.

**On its own** (any job, including non-Greenhouse):

```powershell
.venv\Scripts\python forms_automation\ai\cover_letter.py https://job-boards.greenhouse.io/striveworks/jobs/8012592003
.venv\Scripts\python forms_automation\ai\cover_letter.py --jd-file forms_automation\ai\Test-job-post.md
.venv\Scripts\python forms_automation\ai\cover_letter.py --jd-file job.txt --company "Acme" --role "ML Engineer" --resume path\to\cv.pdf
```

**Output** (in `data/cover_letters/`, gitignored):

- `<company>-<job>.txt` and `<Your_Name>_Cover_Letter_<company>-<job>.pdf`
- `cover_letters.json` - a record of every letter: job link, company, role, model,
  status (`generated`, `uploaded`, `uploaded-unreviewed`, `skipped`, `upload-failed`),
  whether you edited it, how many times it was regenerated, the warnings, and the
  final text.

The checks flag numbers and tool names not on your resume, sentences copied from
the job post (small models restate requirements as your experience), an invented
company name, and clichés. Always read the letter before uploading.

## Email security code after Submit

Greenhouse sometimes asks for an 8-character code it emails you ("Security code
for your application to <Company>") after you click Submit. While the review
window is open, the script watches for the code boxes, reads the newest code
email from Gmail and types the code in - then you click **Submit** again. If
Greenhouse rejects it (e.g. expired), it waits for a newer email and retries.

Gmail is read over IMAP, read-only: nothing is marked read, moved or deleted.
Credentials come from the repo's `.env` (gitignored):

```
SENDER_EMAIL=you@gmail.com
SENDER_PASS=xxxx xxxx xxxx xxxx    # a Google app password, not your normal password
```

Add `--no-otp` to turn this off.
