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
