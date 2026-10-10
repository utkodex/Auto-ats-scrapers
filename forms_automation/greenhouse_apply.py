"""Fill Greenhouse job applications in a visible Chrome window.

The script never submits. It fills every field it can, prints anything it
left blank, and waits while you review the form and click Submit yourself.

Answers come from, in order:
  1. profile.json  - your details (name, email, resume, education...)
  2. answers.json  - questions you answered before (see questions.py)
  3. profile.json  - regex "rules" for common questions
  4. a pop-up      - anything else is recorded in answers.json and asked once
                     in a window (or in the terminal with --terminal)

Usage:
    python forms_automation/greenhouse_apply.py URL [URL ...]
    python forms_automation/greenhouse_apply.py --file urls.txt
    python forms_automation/greenhouse_apply.py --no-ask URL ...   # just collect unknown questions
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import gui
from playwright.sync_api import Locator, Page, sync_playwright
from question_store import SKIP, QuestionStore, norm

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BROWSER_PROFILE = HERE / ".chrome-profile"

# Inputs that belong to widgets, not to the application itself.
IGNORED_IDS = re.compile(r"^(iti-|g-recaptcha|recaptcha)")
YES = {"yes", "y", "true", "1", "agree", "i agree"}
NEGATIONS = {"no", "not", "none", "never", "don", "dont", "non", "nor", "without"}


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------

class Answers:
    def __init__(self, profile: dict, store: QuestionStore, interactive: bool = True, use_gui: bool = True):
        self.profile = profile
        self.store = store
        self.interactive = interactive
        self.use_gui = use_gui
        self.rules = [(re.compile(r["match"], re.I), r["answer"]) for r in profile.get("rules", [])]

    def rule_answer(self, label: str):
        for pattern, answer in self.rules:
            if pattern.search(label):
                return answer
        return None

    def ask(self, label: str, options: list[str] | None, required: bool, multi: bool = False,
            kind: str = "text", job: str | None = None):
        """Ask the user; the answer is stored in answers.json for next time."""
        if not self.interactive:
            return None
        if not self.use_gui:
            return self.ask_terminal(label, options, required, multi)
        action, answer = gui.ask_question(label, options, required, multi, kind, job)
        if action == gui.SKIP:
            self.store.set_answer(label, SKIP)
            return None
        if action == gui.BLANK:
            return None
        self.store.set_answer(label, answer)
        return answer

    def ask_terminal(self, label: str, options: list[str] | None, required: bool, multi: bool = False):
        print()
        print(f"  ? {label}{'  (required)' if required else ''}")
        if options:
            for i, option in enumerate(options[:60], 1):
                print(f"      {i:>2}. {option}")
            if len(options) > 60:
                print(f"      ... {len(options) - 60} more (type text to search)")
            hint = "number(s) like 1,3" if multi else "number"
            print(f"    Enter a {hint} or text.", end=" ")
        print("Blank = leave empty this time, 'skip' = never fill this question.")
        try:
            raw = input("    > ").strip()
        except EOFError:  # no terminal attached
            return None
        if not raw:
            return None
        if raw.lower() == "skip":
            self.store.set_answer(label, SKIP)
            return None
        answer: object = raw
        if options and re.fullmatch(r"\d+(\s*,\s*\d+)*", raw):
            picks = [options[int(n) - 1] for n in re.split(r"\s*,\s*", raw) if 0 < int(n) <= len(options)]
            if picks:
                answer = picks if multi else picks[0]
        self.store.set_answer(label, answer)
        return answer


def negated(text: str) -> bool:
    return bool(NEGATIONS & set(norm(text).split()))


def best_option(answer: str, options: list[str], threshold: float = 0.6) -> int | None:
    """Index of the option that best matches answer, or None.

    None also when the answer is ambiguous, e.g. "Yes" against
    "Yes, H-1B" / "Yes, EU Blue Card" - the caller then asks the user.
    """
    wanted = norm(answer)
    if not wanted:
        return None
    if sum(norm(o) == wanted for o in options) == 0:
        prefixed = [o for o in options if norm(o).startswith(wanted + " ")]
        if len(prefixed) > 1:
            return None
    best, best_score = None, 0.0
    for i, option in enumerate(options):
        have = norm(option)
        if have == wanted:
            score = 1.0
        elif have.startswith(wanted + " ") or wanted.startswith(have + " "):
            score = 0.9
        elif f" {wanted} " in f" {have} ":
            score = 0.8
        elif negated(wanted) != negated(have):
            # Similar spelling, opposite meaning: "I am not a protected
            # veteran" must never become "Other Protected Veteran".
            score = 0.0
        else:
            score = difflib.SequenceMatcher(None, wanted, have).ratio()
            if score < 0.75:  # loose look-alikes go to the user instead
                score = 0.0
        score -= len(have) / 10_000  # prefer the shorter option on ties
        if score > best_score:
            best, best_score = i, score
    return best if best_score >= threshold else None


# --------------------------------------------------------------------------
# Page helpers
# --------------------------------------------------------------------------

LABEL_JS = """el => {
  const id = el.id;
  let l = id && (document.getElementById(id + '-label') ||
                 document.querySelector(`label[for="${CSS.escape(id)}"]`));
  if (!l && el.getAttribute('aria-labelledby'))
    l = document.getElementById(el.getAttribute('aria-labelledby').split(' ')[0]);
  const text = l ? l.innerText : (el.getAttribute('aria-label') || el.placeholder || '');
  return text.replace(/\\s*\\*\\s*$/, '').trim();
}"""

COMBO_STATE_JS = """el => {
  const control = el.closest('.select__control') || el.closest('[class*="control"]');
  return {
    filled: !!(control && control.querySelector('.select__single-value, .select__multi-value')),
    multi: !!(control && control.querySelector('.select__value-container--is-multi')),
  };
}"""

MENU_OPTIONS = ".select__menu [role=option]"


def label_of(el: Locator) -> str:
    return el.evaluate(LABEL_JS)


def is_required(el: Locator) -> bool:
    return el.get_attribute("aria-required") == "true" or el.get_attribute("required") is not None


def menu_options(page: Page, wait_ms: int = 4000) -> list[str]:
    """Texts of the open react-select menu, waiting for async options to load."""
    waited = 0
    while True:
        options = page.locator(MENU_OPTIONS).all_inner_texts()
        if options or waited >= wait_ms:
            return [o.strip() for o in options]
        page.wait_for_timeout(250)
        waited += 250


def pick_in_combo(page: Page, el: Locator, answer: str) -> str | None:
    """Select answer in a react-select; returns the chosen option text."""
    el.scroll_into_view_if_needed()
    el.click()
    options = menu_options(page, wait_ms=800)
    index = best_option(answer, options)
    if index is None:
        # Async list (location, school) or long list: search for it.
        for term in dict.fromkeys([answer, answer.split(",")[0]]):
            el.fill(term)
            page.wait_for_timeout(600)
            options = menu_options(page)
            index = best_option(answer, options)
            if index is not None:
                break
    if index is None:
        el.fill("Other")
        page.wait_for_timeout(600)
        options = menu_options(page, wait_ms=2000)
        index = best_option("Other", options, threshold=0.95)
    if index is None:
        el.fill("")
        page.keyboard.press("Escape")
        return None
    chosen = options[index]
    page.locator(MENU_OPTIONS).nth(index).click()
    page.wait_for_timeout(200)
    return chosen


def combo_options_for_prompt(page: Page, el: Locator) -> list[str]:
    el.scroll_into_view_if_needed()
    el.click()
    options = menu_options(page, wait_ms=1500)
    page.keyboard.press("Escape")
    return options


# --------------------------------------------------------------------------
# Form filling
# --------------------------------------------------------------------------

class Filler:
    def __init__(self, page: Page, answers: Answers):
        self.page = page
        self.answers = answers
        self.profile = answers.profile
        self.done: set[str] = set()
        self.blank_required: list[str] = []
        self.unknown: list[str] = []  # questions asked/recorded in answers.json this run

    def log(self, label: str, value) -> None:
        shown = ", ".join(value) if isinstance(value, list) else str(value)
        print(f"  + {label[:70]:<70} -> {shown[:60]}")

    # Answers tied to Greenhouse's built-in field ids rather than question text.
    def builtin_answer(self, field_id: str):
        p = self.profile
        fixed = {
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "preferred_name": p.get("preferred_name"),
            "email": p.get("email"),
            "phone": p.get("phone"),
            "country": p.get("phone_country"),
            "candidate-location": p.get("location"),
        }
        if field_id in fixed:
            return fixed[field_id]
        match = re.fullmatch(r"(school|degree|discipline)--(\d+)", field_id)
        if match:
            schools = p.get("education") or []
            index = int(match[2])
            if index < len(schools):
                return schools[index].get(match[1])
        return None

    def resolve(self, field_id: str, label: str, required: bool, kind: str, options_fn=None, multi=False):
        answer = self.builtin_answer(field_id)
        if answer is not None:
            return answer
        store = self.answers.store
        known = store.get(label) is not None
        if known:  # a question we've met before - count this sighting
            store.record(label, kind, required=required, job=self.page.url)
            answer = store.answer_for(label)
        if answer is None:
            answer = self.answers.rule_answer(label)
        if answer is None:
            options = options_fn() if options_fn else None
            answer = self.ask_user(label, required, kind, options, multi, recorded=known)
        return None if answer == SKIP else answer

    def ask_user(self, label: str, required: bool, kind: str, options: list[str] | None,
                 multi=False, recorded=False):
        """Record an unknown question in answers.json, then ask for its answer."""
        if not recorded:
            self.answers.store.record(label, kind, options, required, job=self.page.url)
        elif options:  # already counted this run - just refresh its options
            self.answers.store.update_options(label, options)
        self.unknown.append(label)
        return self.answers.ask(label, options, required, multi, kind, job=self.page.url)

    def run(self) -> None:
        self.page.wait_for_selector("#first_name, form input", timeout=30_000)
        apply_button = self.page.get_by_role("button", name=re.compile(r"^\s*apply", re.I))
        if not self.page.locator("#first_name").is_visible() and apply_button.count():
            apply_button.first.click()
            self.page.wait_for_selector("#first_name", timeout=15_000)

        self.upload_files()
        # Answering some questions reveals others (e.g. race after ethnicity),
        # so keep sweeping until a pass finds nothing new.
        for _ in range(5):
            progressed = self.fill_pass()
            if not progressed:
                break
        self.report()

    def upload_files(self) -> None:
        for field_id, key in (("resume", "resume"), ("cover_letter", "cover_letter")):
            path = self.profile.get(key)
            el = self.page.locator(f"input[type=file]#{field_id}")
            if not path or not el.count():
                continue
            file = Path(path) if Path(path).is_absolute() else REPO / path
            if not file.exists():
                print(f"  ! {key} file not found: {file}")
                continue
            el.set_input_files(str(file))
            self.done.add(field_id)
            try:  # the upload is done when the file name replaces the progress bar
                self.page.get_by_text(file.name).first.wait_for(timeout=30_000)
                self.log(key.replace("_", " ").title(), file.name)
            except Exception:
                print(f"  ! {key} upload did not finish - attach {file.name} in the browser")

    def fill_pass(self) -> bool:
        progressed = False
        form = self.page.locator("form").filter(has=self.page.locator("input")).first

        for el in form.locator("input.select__input[id]").all():
            progressed |= self.fill_combo(el)
        for el in form.locator(
            "input[id]:not([type=hidden]):not([type=file]):not([type=checkbox])"
            ":not([type=radio]):not([type=submit]):not(.select__input), textarea[id]"
        ).all():
            progressed |= self.fill_text(el)
        for el in form.locator("select[id]").all():
            progressed |= self.fill_native_select(el)
        for group in form.locator("fieldset").filter(
            has=self.page.locator("input[type=checkbox], input[type=radio]")
        ).all():
            progressed |= self.fill_choice_group(group)
        for el in form.locator("input[type=file][id]").all():
            field_id = el.get_attribute("id")
            if field_id not in self.done and field_id not in ("resume", "cover_letter"):
                self.done.add(field_id)
                print(f"  ! file upload '{label_of(el) or field_id}' left for you")
        return progressed

    def claim(self, el: Locator) -> tuple[str, str] | None:
        field_id = el.get_attribute("id") or ""
        if not field_id or field_id in self.done or IGNORED_IDS.match(field_id):
            return None
        self.done.add(field_id)
        return field_id, label_of(el) or field_id

    def fill_text(self, el: Locator) -> bool:
        claimed = self.claim(el)
        if not claimed or not el.is_visible():
            return bool(claimed)
        field_id, label = claimed
        if el.input_value().strip():
            return True
        required = is_required(el)
        kind = "textarea" if el.evaluate("e => e.tagName") == "TEXTAREA" else "text"
        answer = self.resolve(field_id, label, required, kind)
        if answer is None:
            if required:
                self.blank_required.append(label)
            return True
        value = ", ".join(answer) if isinstance(answer, list) else str(answer)
        el.fill(value)
        self.log(label, value)
        return True

    def fill_combo(self, el: Locator) -> bool:
        claimed = self.claim(el)
        if not claimed:
            return False
        field_id, label = claimed
        state = el.evaluate(COMBO_STATE_JS)
        if state["filled"]:
            return True
        required = is_required(el)
        answer = self.resolve(
            field_id, label, required, "multiselect" if state["multi"] else "select",
            options_fn=lambda: combo_options_for_prompt(self.page, el),
            multi=state["multi"],
        )
        if answer is None:
            if required:
                self.blank_required.append(label)
            return True
        chosen = self.pick_all(el, answer)
        if not chosen:
            # The rule/remembered answer fits none of this form's options
            # (or is ambiguous) - let the user pick.
            options = combo_options_for_prompt(self.page, el)
            if options:
                kind = "multiselect" if state["multi"] else "select"
                answer = self.ask_user(label, required, kind, options, state["multi"],
                                       recorded=self.answers.store.get(label) is not None)
                chosen = self.pick_all(el, answer) if answer is not None else []
        if chosen:
            self.log(label, chosen)
        else:
            if answer is not None:
                print(f"  ! '{label}': no option matched {answer!r} - pick it in the browser")
            if required:
                self.blank_required.append(label)
        return True

    def pick_all(self, el: Locator, answer) -> list[str]:
        chosen = []
        for item in answer if isinstance(answer, list) else [answer]:
            picked = pick_in_combo(self.page, el, str(item))
            if picked:
                chosen.append(picked)
        return chosen

    def fill_native_select(self, el: Locator) -> bool:
        claimed = self.claim(el)
        if not claimed or not el.is_visible():
            return bool(claimed)
        field_id, label = claimed
        options = [o.strip() for o in el.locator("option").all_inner_texts()]
        if el.input_value() and el.input_value() not in ("", "--"):
            return True
        required = is_required(el)
        answer = self.resolve(field_id, label, required, "select", options_fn=lambda: [o for o in options if o])
        index = best_option(str(answer), options) if answer is not None else None
        if index is None and answer is not None:
            answer = self.ask_user(label, required, "select", [o for o in options if o],
                                   recorded=self.answers.store.get(label) is not None)
            index = best_option(str(answer), options) if answer is not None else None
        if index is None:
            if required:
                self.blank_required.append(label)
            return True
        el.select_option(label=el.locator("option").nth(index).inner_text())
        self.log(label, options[index])
        return True

    def fill_choice_group(self, group: Locator) -> bool:
        legend = group.locator("legend")
        label = (legend.first.inner_text() if legend.count() else group.inner_text().split("\n")[0])
        label = re.sub(r"\s*\*\s*$", "", label).strip()
        key = "fieldset:" + label
        if key in self.done:
            return False
        self.done.add(key)

        boxes = group.locator("input[type=checkbox], input[type=radio]").all()
        if any(box.is_checked() for box in boxes):
            return True
        texts = [(label_of(box) or box.get_attribute("value") or "") for box in boxes]
        multi = boxes[0].get_attribute("type") == "checkbox" and len(boxes) > 1
        required = any(is_required(box) for box in boxes) or "*" in (legend.first.inner_text() if legend.count() else "")
        kind = "checkbox" if boxes[0].get_attribute("type") == "checkbox" else "radio"
        answer = self.resolve(key, label, required, kind, options_fn=lambda: texts, multi=multi)
        if answer is None:
            if required:
                self.blank_required.append(label)
            return True

        chosen = []
        if len(boxes) == 1:  # single consent checkbox
            if norm(str(answer)) in YES:
                boxes[0].check(force=True)
                chosen.append(texts[0] or "checked")
        else:
            for item in answer if isinstance(answer, list) else [answer]:
                index = best_option(str(item), texts)
                if index is not None:
                    boxes[index].check(force=True)
                    chosen.append(texts[index])
        if chosen:
            self.log(label, chosen)
        elif required:
            self.blank_required.append(label)
        return True

    def report(self) -> None:
        print()
        if self.blank_required:
            print("  Required fields still empty - fill these in the browser:")
            for label in self.blank_required:
                print(f"    - {label}")
        else:
            print("  All required fields I found are filled.")
        if self.unknown:
            print(f"  {len(self.unknown)} question(s) not covered by profile.json were saved to answers.json"
                  " - see: python forms_automation/questions.py list")
        print("  Check the form (and any captcha), then click Submit yourself.")


def wait_for_review(n: int, total: int, url: str, filler: Filler, error: str | None) -> None:
    """Pop-up shown after each job while you review and submit in Chrome."""
    last = n == total
    lines = [url, ""]
    if error:
        lines.append(f"Stopped early: {error}")
    if filler.blank_required:
        lines.append("Required fields still empty - fill these in the browser:")
        lines += [f"   \u2022 {label}" for label in filler.blank_required]
    elif not error:
        lines.append("All required fields I found are filled.")
    lines += ["", "Check the form (and any captcha), then click Submit in Chrome."]
    if last:
        lines.append("Closing this window closes the browser - submit first!")
    button = "Close browser" if last else "Next job \u2192"
    gui.notice(f"Job {n} of {total}", f"Job {n} of {total} filled - review and submit", lines, [button])


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="Greenhouse job URLs")
    parser.add_argument("--file", type=Path, help="text file with one job URL per line")
    parser.add_argument("--profile", type=Path, default=HERE / "profile.json")
    parser.add_argument("--no-ask", action="store_true",
                        help="don't prompt; just record unknown questions in answers.json")
    parser.add_argument("--terminal", action="store_true",
                        help="ask questions in the terminal instead of pop-up windows")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        urls += [line.strip() for line in args.file.read_text("utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
    if not urls:
        parser.error("give at least one job URL (or --file)")

    answers = Answers(json.loads(args.profile.read_text("utf-8")), QuestionStore(),
                      interactive=not args.no_ask, use_gui=not args.terminal)

    with sync_playwright() as p:
        options = {"headless": False, "no_viewport": True, "args": ["--start-maximized"]}
        try:
            context = p.chromium.launch_persistent_context(BROWSER_PROFILE, channel="chrome", **options)
        except Exception:  # Chrome not installed - use Playwright's Chromium
            context = p.chromium.launch_persistent_context(BROWSER_PROFILE, **options)

        for n, url in enumerate(urls, 1):
            print(f"\n[{n}/{len(urls)}] {url}")
            page = context.new_page()
            filler = Filler(page, answers)
            error = None
            try:
                open_job(page, url)
                filler.run()
            except Exception as exc:  # keep going with the next job
                error = str(exc).splitlines()[0]
                print(f"  ! stopped early: {error}")
            if args.terminal:
                last = n == len(urls)
                input("\n  Press Enter " + ("to close the browser (submit first!)..." if last
                                             else "for the next job (this tab stays open)..."))
            else:
                wait_for_review(n, len(urls), url, filler, error)
        context.close()


if __name__ == "__main__":
    main()
