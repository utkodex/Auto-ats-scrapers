# Greenhouse form automation

Fills Greenhouse job applications in a visible Chrome window. It **never
submits**: you review the form, solve any captcha, and click Submit yourself.

## Setup

```powershell
cp forms_automation/profile.example.json forms_automation/profile.json   # then edit it
```

Uses your installed Google Chrome. If Chrome isn't installed, run
`.venv\Scripts\python -m playwright install chromium` once.

## Run

```powershell
.venv\Scripts\python forms_automation\greenhouse_apply.py https://job-boards.greenhouse.io/striveworks/jobs/8012592003
.venv\Scripts\python forms_automation\greenhouse_apply.py URL1 URL2 URL3
.venv\Scripts\python forms_automation\greenhouse_apply.py --file urls.txt
```

Works with `job-boards.greenhouse.io`, `boards.greenhouse.io`, embed links
(`embed/job_app?for=...&token=...`), and company career pages that embed a
Greenhouse form (`?gh_jid=...`). Each job opens in its own tab; click **Next
job** in the review window to move on. **Close browser** on the last job closes
Chrome, so submit first.

## Where answers come from

1. `profile.json`: your details (name, email, phone, resume, education).
2. `answers.json`: questions you've answered before.
3. `profile.json` `rules`: regex on the question → answer, for common questions.
4. A pop-up window: anything else is **recorded in `answers.json`** (question,
   options, required, which job) and asked in a window that stays on top of
   Chrome. Pick an option (long lists have a search box) or type your own
   answer, then:
   - **Save answer** (Enter): stored and reused on every form that asks it.
   - **Leave blank for now** (Esc): stays unanswered and is asked again next time.
   - **Never fill this**: always skipped.

   After each job a window lists any required fields still empty. Submit in
   Chrome, then click **Next job** (or **Close browser** on the last one).
   Add `--terminal` to answer in the terminal instead.

Add a rule to `profile.json` to answer a whole family of questions, for example
`{"match": "notice period", "answer": "30 days"}`.

## The unknown-questions file (`answers.json`)

Collect questions first, answer them in one go, then apply:

```powershell
# 1. fill what's known, record the rest without stopping to ask
.venv\Scripts\python forms_automation\greenhouse_apply.py --no-ask --file urls.txt

# 2. see and answer what was collected
.venv\Scripts\python forms_automation\questions.py list --unanswered
.venv\Scripts\python forms_automation\questions.py show 12        # full text + options
.venv\Scripts\python forms_automation\questions.py answer 12 2    # option number, or text
.venv\Scripts\python forms_automation\questions.py answer         # walk through all unanswered

# ...or edit them in Excel
.venv\Scripts\python forms_automation\questions.py export questions.csv
.venv\Scripts\python forms_automation\questions.py import questions.csv

# 3. rerun - answered questions are now filled automatically
```

Other commands: `skip ID` (never fill), `clear ID` (forget the answer),
`delete ID`, `list --search visa`.

You can also edit `answers.json` directly: each question has an `"answer"`
field (`null` = not answered yet, `"__skip__"` = never fill).

`profile.json`, `answers.json` and `.chrome-profile/` hold personal data and are
gitignored.

## Cover letters (local LLM)

Cover letters are written by a local Ollama model - nothing leaves this PC.
One-time setup: `ollama pull llama3.1:8b` and keep the Ollama app running.

**In the form automation** (automatic): when a job has a Cover Letter field,
`greenhouse_apply.py` writes a letter in the background while it fills the form,
then opens a review window with the letter (editable) and the automatic checks:

- **Upload as PDF** (Ctrl+Enter): saves your version as a PDF and attaches it.
- **Regenerate**: writes a new draft.
- **Skip cover letter**: nothing is uploaded (the text is still saved).

```powershell
.venv\Scripts\python forms_automation\greenhouse_apply.py URL                          # letters on by default
.venv\Scripts\python forms_automation\greenhouse_apply.py URL --no-cover-letter        # turn off
.venv\Scripts\python forms_automation\greenhouse_apply.py URL --cover-letter-model gemma3:12b --cover-letter-words 250
```

A fixed `"cover_letter": "path/to/letter.pdf"` in `profile.json` is uploaded instead
of writing one. With `--no-ask`, letters are uploaded without review (marked
`uploaded-unreviewed` in the log) - read them before you submit.

**On its own** (any job, including non-Greenhouse):

```powershell
.venv\Scripts\python forms_automation\cover_letter.py https://job-boards.greenhouse.io/striveworks/jobs/8012592003
.venv\Scripts\python forms_automation\cover_letter.py --jd-file forms_automation\Test-job-post.md
.venv\Scripts\python forms_automation\cover_letter.py --jd-file job.txt --company "Acme" --role "ML Engineer" --resume path\to\cv.pdf
```

**Output** (in `cover_letters/`, gitignored):

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
