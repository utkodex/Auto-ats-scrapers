"""Fill Greenhouse job applications in a visible Chrome window.

The script never submits. It fills every field it can, prints anything it
left blank, and waits while you review the form and click Submit yourself.

Answers come from, in order:
  1. profile.json  - your details (name, email, resume, education...)
  2. answers.json  - questions you answered before (see db/questions.py), and
                     the answer of the question's head (topic) in heads.json
  3. profile.json  - regex "rules" for common questions
  4. Jev           - a reworded question is matched to a head with the local
                     LLM; a low-confidence match or a new topic asks you first
  5. a pop-up      - anything else is recorded in answers.json and asked once
                     in a window (or in the terminal with --terminal)
Essay and "if other, please explain" questions get a fresh answer written by
the local LLM for each job, which you review before it's filled.

Usage:
    python forms_automation/greenhouse/apply.py URL [URL ...]
    python forms_automation/greenhouse/apply.py --file urls.txt
    python forms_automation/greenhouse/apply.py --no-ask URL ...   # just collect unknown questions
    python forms_automation/greenhouse/apply.py --no-classify URL  # exact wording only, no LLM matching
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):  # run as a file: make the forms_automation package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import Page, sync_playwright

from forms_automation.ai import cover_letter, jev
from forms_automation.automation.answers import Answers
from forms_automation.common import gui
from forms_automation.common.paths import BROWSER_PROFILE, PROFILE_FILE
from forms_automation.db.head_store import HeadStore
from forms_automation.db.question_store import QuestionStore
from forms_automation.greenhouse.cover_letter_task import CoverLetterTask
from forms_automation.greenhouse.filler import Filler
from forms_automation.greenhouse.security_code import SecurityCodeWatcher, wait_in_terminal


def canonical_job_url(url: str) -> str | None:
    """Map any Greenhouse job URL to its job-boards page, or None if unknown."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    host = parsed.netloc.lower()
    eu = ".eu." in host
    base = "https://job-boards.eu.greenhouse.io" if eu else "https://job-boards.greenhouse.io"

    if "greenhouse.io" in host:
        if "for" in query and "token" in query:  # .../embed/job_app?for=x&token=y
            return f"{base}/{query['for'][0]}/jobs/{query['token'][0]}"
        match = re.match(r"/([^/]+)/jobs/(\d+)", parsed.path)
        if match:
            return f"{base}/{match[1]}/jobs/{match[2]}"
    return None


def embed_url(job_url: str) -> str:
    """The form-only embed page, which is never redirected to a company site."""
    parsed = urlparse(job_url)
    board, _, job_id = parsed.path.strip("/").split("/")
    return f"{parsed.scheme}://{parsed.netloc}/embed/job_app?for={board}&token={job_id}"


def load(page: Page, url: str) -> None:
    """Open url and let the React form hydrate - filling earlier silently drops input."""
    page.goto(url, wait_until="load", timeout=60_000)
    try:  # trackers on some pages never go quiet, so cap the wait
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        page.wait_for_timeout(2_000)


def open_job(page: Page, url: str) -> None:
    target = canonical_job_url(url)
    if not target:
        # Company careers page (?gh_jid=123): Greenhouse redirects a bare job id
        # to the embed URL of the board that owns it.
        job_id = parse_qs(urlparse(url).query).get("gh_jid")
        if job_id:
            page.goto(f"https://boards.greenhouse.io/embed/job_app?token={job_id[0]}", timeout=60_000)
            target = canonical_job_url(page.url)
    if not target:
        # Some other page that embeds the form in an iframe.
        load(page, url)
        for frame in page.frames:
            if "greenhouse.io/embed/job_app" in frame.url:
                load(page, frame.url)
                return
        return
    load(page, target)
    if not page.locator("#first_name").count():
        # Boards with their own careers site redirect the job page there.
        load(page, embed_url(target))


def wait_for_review(n: int, total: int, url: str, filler: Filler, error: str | None,
                    notes: list[str] | None = None, watcher: SecurityCodeWatcher | None = None) -> None:
    """Pop-up shown after each job while you review and submit in Chrome."""
    last = n == total
    lines = [url, ""]
    if error:
        lines.append(f"Stopped early: {error}")
    lines += notes or []
    if filler.blank_required:
        lines.append("Required fields still empty - fill these in the browser:")
        lines += [f"   \u2022 {label}" for label in filler.blank_required]
    elif not error:
        lines.append("All required fields I found are filled.")
    lines += ["", "Check the form (and any captcha), then click Submit in Chrome."]
    if watcher:
        lines.append("If Greenhouse then asks for the emailed security code, it's fetched from "
                     "Gmail and typed in for you - then click Submit again.")
    if last:
        lines.append("Closing this window closes the browser - submit first!")
    button = "Close browser" if last else "Next job \u2192"
    gui.notice(f"Job {n} of {total}", f"Job {n} of {total} filled - review and submit", lines, [button],
               poll=watcher.poll if watcher else None)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="Greenhouse job URLs")
    parser.add_argument("--file", type=Path, help="text file with one job URL per line")
    parser.add_argument("--profile", type=Path, default=PROFILE_FILE)
    parser.add_argument("--no-ask", action="store_true",
                        help="don't prompt; just record unknown questions in answers.json")
    parser.add_argument("--terminal", action="store_true",
                        help="ask questions in the terminal instead of pop-up windows")
    parser.add_argument("--no-cover-letter", action="store_true",
                        help="don't write cover letters with the local LLM")
    parser.add_argument("--cover-letter-model", default=cover_letter.DEFAULT_MODEL,
                        help=f"Ollama model for cover letters (default {cover_letter.DEFAULT_MODEL})")
    parser.add_argument("--cover-letter-words", type=int, default=cover_letter.DEFAULT_WORDS)
    parser.add_argument("--no-otp", action="store_true",
                        help="don't fetch Greenhouse's emailed security code from Gmail")
    parser.add_argument("--no-classify", action="store_true",
                        help="match questions by exact wording only (no heads, no LLM matching)")
    parser.add_argument("--threshold", type=float, default=jev.CONFIDENCE_THRESHOLD,
                        help=f"Jev confidence needed to fill without asking (default {jev.CONFIDENCE_THRESHOLD})")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [line.strip() for line in args.file.read_text("utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
    if not urls:
        parser.error("give at least one job URL (or --file)")

    profile = json.loads(args.profile.read_text("utf-8"))
    answers = Answers(profile, QuestionStore(), interactive=not args.no_ask, use_gui=not args.terminal,
                      heads=HeadStore(), classify=not args.no_classify, threshold=args.threshold)
    if answers.classify and not answers.heads.approved():
        print("  ! No topics set up yet, so only questions with the exact same wording are filled from "
              "answers.json.\n    Run once: python forms_automation/db/heads.py build")
    # A fixed cover_letter file in profile.json wins; otherwise write one per job.
    write_letters = not args.no_cover_letter and not profile.get("cover_letter")

    with sync_playwright() as p:
        options = {"headless": False, "no_viewport": True, "args": ["--start-maximized"]}
        try:
            context = p.chromium.launch_persistent_context(str(BROWSER_PROFILE), channel="chrome", **options)
        except Exception:  # Chrome not installed - use Playwright's Chromium
            context = p.chromium.launch_persistent_context(str(BROWSER_PROFILE), **options)

        headless: list = []  # created on first use - renders cover-letter PDFs

        def pdf_browser():
            if not headless:
                try:
                    headless.append(p.chromium.launch(channel="chrome"))
                except Exception:
                    headless.append(p.chromium.launch())
            return headless[0]

        for n, url in enumerate(urls, 1):
            print(f"\n[{n}/{len(urls)}] {url}")
            page = context.new_page()
            filler = Filler(page, answers)
            error = None
            notes: list[str] = []
            task = None
            try:
                open_job(page, url)
                if write_letters and page.locator("input[type=file]#cover_letter").count():
                    filler.done.add("cover_letter")
                    task = CoverLetterTask(page.url, profile, args.cover_letter_model,
                                           args.cover_letter_words, pdf_browser,
                                           interactive=not args.no_ask, use_gui=not args.terminal)
                filler.run()
            except Exception as exc:  # keep going with the next job
                error = str(exc).splitlines()[0]
                print(f"  ! stopped early: {error}")
            if task:
                try:
                    notes.append(task.finish(page))
                except Exception as exc:
                    notes.append(f"Cover letter failed: {str(exc).splitlines()[0]}")
                    print(f"  ! {notes[-1]}")
            watcher = None
            if not args.no_otp and not page.is_closed():
                company = task.job["company"] if task and task.job else gui.company_of(page.url)
                watcher = SecurityCodeWatcher(page, company)
            if args.terminal:
                last = n == len(urls)
                wait_in_terminal("\n  Press Enter " + ("to close the browser (submit first!)..." if last
                                                        else "for the next job (this tab stays open)..."),
                                 watcher)
            else:
                wait_for_review(n, len(urls), url, filler, error, notes, watcher)
        for browser in headless:
            browser.close()
        context.close()


if __name__ == "__main__":
    main()
