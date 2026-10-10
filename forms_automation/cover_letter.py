"""Write a tailored cover letter with a local Ollama model.

Reads your resume (the PDF in profile.json) and the job description (fetched
from a Greenhouse job link, or a text file), asks the model for a letter, and
saves it to forms_automation/cover_letters/ as .txt and .pdf. Every letter is
also logged in cover_letters/cover_letters.json. Everything runs on this PC.

greenhouse_apply.py uses the same functions to write, review and upload a
letter for each job that has a Cover Letter field.

Usage:
    python forms_automation/cover_letter.py https://job-boards.greenhouse.io/striveworks/jobs/8012592003
    python forms_automation/cover_letter.py --jd-file job.txt --company "Acme" --role "ML Engineer"
    python forms_automation/cover_letter.py URL --notes "mention I can start in 30 days" --words 250

Needs Ollama running with the model pulled:  ollama pull llama3.1:8b
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import html2text
import httpx
from pypdf import PdfReader

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT_DIR = HERE / "cover_letters"
LOG_FILE = OUT_DIR / "cover_letters.json"
OLLAMA = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_WORDS = 300
MAX_JD_CHARS = 12_000  # keeps resume + job description well inside the context window

# Clichés the model is told to avoid; any that slip through are flagged afterwards.
BANNED_PHRASES = [
    "I am writing to", "I'm writing to", "I am confident", "I'm confident", "passionate", "perfect fit",
    "strong fit", "strong candidate", "great fit", "thrilled", "excited", "seasoned", "esteemed",
    "leverage", "synergy", "dynamic", "fast-paced", "I believe I would", "look forward to discussing",
]

SYSTEM_PROMPT = f"""You write cover letters for job applications. Rules:
- Use ONLY facts from the candidate's resume. Never invent employers, job titles, degrees,
  certifications, years of experience, metrics or numbers. If a requirement isn't covered
  by the resume, don't claim it.
- Only name tools, languages and technologies that appear in the resume. If the job asks for
  one the resume doesn't list, leave it out.
- The job description lists what the company WANTS - it is not the candidate's experience.
  Never copy its sentences or restate its requirements as things the candidate has done.
- Name the company and the role. Pick the 2-3 requirements from the job description that the
  resume matches best, and back each with a concrete achievement from the resume.
- Open with what the candidate has built that matters to this role, not with a summary of
  themselves. End with one short, plain closing sentence.
- Plain, specific language. Never use any of these words or phrases:
  {", ".join(f'"{p}"' for p in BANNED_PHRASES)}.
- Do not mention visas, work authorization, salary or location.
- Plain text only: no markdown, no bullet points, no placeholders like [Company].
- Start with "Dear Hiring Team at <company>," - or just "Dear Hiring Team," when the company
  isn't named (then say "your team", never invent a company name). End with "Sincerely," and
  the candidate's name on its own line. Output only the letter."""


class CoverLetterError(Exception):
    """A problem the user can fix (Ollama not running, job not found, ...)."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def resume_text(path: Path) -> str:
    path = path if path.is_absolute() else REPO / path
    if not path.exists():
        raise CoverLetterError(f"Resume not found: {path}")
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return re.sub(r"[ \t]+", " ", text).strip()


def greenhouse_job(url: str) -> dict:
    """Title, company and plain-text description of a Greenhouse job."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    board = job_id = None
    if "greenhouse.io" in parsed.netloc:
        if "for" in query and "token" in query:
            board, job_id = query["for"][0], query["token"][0]
        elif match := re.match(r"/([^/]+)/jobs/(\d+)", parsed.path):
            board, job_id = match[1], match[2]
    if not board and "gh_jid" in query:
        # Company careers page: Greenhouse redirects the bare id to its board.
        r = httpx.get(f"https://boards.greenhouse.io/embed/job_app?token={query['gh_jid'][0]}",
                      follow_redirects=True, timeout=30)
        redirected = parse_qs(urlparse(str(r.url)).query)
        board, job_id = redirected.get("for", [None])[0], query["gh_jid"][0]
    if not board:
        raise CoverLetterError("Not a Greenhouse job link - use --jd-file with the job description instead.")

    r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}", timeout=30)
    if r.status_code == 404:
        raise CoverLetterError(f"Job {job_id} not found on Greenhouse board '{board}' (closed?).")
    r.raise_for_status()
    data = r.json()
    converter = html2text.HTML2Text()
    converter.ignore_links = converter.ignore_images = converter.ignore_emphasis = True
    converter.body_width = 0
    return {
        "company": data.get("company_name") or board.replace("-", " ").title(),
        "role": data.get("title", ""),
        "description": converter.handle(html.unescape(data.get("content", ""))).strip(),
        "slug": f"{board}-{job_id}",
        "url": url,
    }


def file_job(path: Path, company: str | None = None, role: str | None = None) -> dict:
    """A job description saved in a text/markdown file (any job site)."""
    if not path.exists():
        raise CoverLetterError(f"Job description file not found: {path}")
    description = path.read_text("utf-8")
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    role = role or (first_line if len(first_line) <= 80 else "")
    slug = re.sub(r"[^a-z0-9]+", "-", " ".join(filter(None, [company, role])).lower() or path.stem.lower())
    return {"company": company or "", "role": role, "description": description,
            "slug": slug.strip("-"), "url": str(path)}


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------

def check_ollama(model: str) -> None:
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=10).json()
    except httpx.HTTPError as exc:
        raise CoverLetterError("Ollama isn't running. Start the Ollama app (or run `ollama serve`).") from exc
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        raise CoverLetterError(f"Model '{model}' isn't downloaded. Run:  ollama pull {model}")


def generate(model: str, system: str, prompt: str, temperature: float, echo: bool = True) -> str:
    """Ask the model; with echo, stream the reply to the terminal as it's written."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": True,
        # Ollama's default context is too small for resume + job description.
        "options": {"num_ctx": 8192, "temperature": temperature},
    }
    parts: list[str] = []
    started = time.time()
    first_token = None
    with httpx.stream("POST", f"{OLLAMA}/api/chat", json=body, timeout=None) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise CoverLetterError(f"Ollama error: {chunk['error']}")
            token = chunk.get("message", {}).get("content", "")
            if token and first_token is None:
                first_token = time.time()
                if echo:
                    print(f"(read the prompt in {first_token - started:.0f}s)\n")
            if echo:
                print(token, end="", flush=True)
            parts.append(token)
            if chunk.get("done"):
                break
    if echo:
        print(f"\n\n(written in {time.time() - (first_token or started):.0f}s)")
    return "".join(parts).strip()


def candidate_name(profile: dict) -> str:
    return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()


def write_letter(job: dict, profile: dict, resume_path: Path | None = None, model: str = DEFAULT_MODEL,
                 words: int = DEFAULT_WORDS, notes: str = "", temperature: float = 0.6,
                 echo: bool = True) -> dict:
    """Generate a letter for `job`; returns {"letter", "warnings", "resume"}."""
    check_ollama(model)
    resume = resume_text(resume_path or Path(profile["resume"]))
    prompt = (
        f"Write a cover letter of about {words} words, in 3-4 short paragraphs.\n\n"
        f"Candidate: {candidate_name(profile)}\nCompany: {job['company'] or '(not named in the job post)'}\n"
        f"Role: {job['role']}\n"
        + (f"Extra points from the candidate to include: {notes}\n" if notes else "")
        + f"\n=== CANDIDATE RESUME ===\n{resume}\n"
        f"\n=== JOB DESCRIPTION ===\n{job['description'][:MAX_JD_CHARS]}\n"
    )
    letter = generate(model, SYSTEM_PROMPT, prompt, temperature, echo)
    return {"letter": letter, "warnings": warnings_for(letter, resume, job, words, notes), "resume": resume}


# --------------------------------------------------------------------------
# Checks on the result
# --------------------------------------------------------------------------

def unsupported_numbers(letter: str, *sources: str) -> list[str]:
    """Numbers in the letter that appear in neither the resume nor the job post."""
    known = dashes(" ".join(sources))
    found = set(re.findall(r"\d[\d,.]*\+?%?", dashes(letter)))
    return sorted(n for n in found if n.rstrip(".,") not in known)


def unsupported_terms(letter: str, *sources: str) -> list[str]:
    """Capitalised names (tools, skills, companies) that appear in neither source.

    Catches the typical small-model slip of claiming a technology that isn't on
    the resume, e.g. "TensorFlow". Sentence-initial words are ignored.
    """
    known = dashes(" ".join(sources)).lower()
    body = [line for line in dashes(letter).splitlines() if not re.match(r"\s*(Dear|Sincerely)\b", line)]
    found = set()
    for sentence in re.split(r"(?<=[.!?:,])\s+|\n+", "\n".join(body)):
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]", sentence)[1:]:
            looks_like_name = any(c.isupper() for c in token) or "-" in token  # PyTorch, scikit-learn
            if looks_like_name and any(c.isalpha() for c in token) and token.lower() not in known:
                found.add(token)
    return sorted(found)


def copied_from_job(letter: str, job_text: str, resume: str, size: int = 6) -> list[str]:
    """Runs of `size`+ words lifted from the job post that aren't on the resume.

    Small models tend to restate the job's requirements as the candidate's own
    experience ("I have strong fundamentals in algorithms, data structures...").
    """
    def words(text: str) -> list[str]:
        return re.findall(r"[a-z0-9+#]+", dashes(text).lower())

    def shingles(seq: list[str]) -> set[tuple[str, ...]]:
        return {tuple(seq[i:i + size]) for i in range(len(seq) - size + 1)}

    letter_words = words(letter)
    job_shingles, resume_shingles = shingles(words(job_text)), shingles(words(resume))
    copied = [False] * len(letter_words)
    for i in range(len(letter_words) - size + 1):
        gram = tuple(letter_words[i:i + size])
        if gram in job_shingles and gram not in resume_shingles:
            copied[i:i + size] = [True] * size
    runs, start = [], None
    for i, flag in enumerate([*copied, False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append(" ".join(letter_words[start:i]))
            start = None
    return runs


def dashes(text: str) -> str:
    return text.replace("–", "-").replace("—", "-")


def warnings_for(letter: str, resume: str, job: dict, words: int, extra: str = "") -> list[str]:
    notes = []
    count = len(letter.split())
    if count > words * 1.3 or count < words * 0.6:
        notes.append(f"length is {count} words (asked for about {words})")
    if re.search(r"\[[^\]]+\]", letter):
        notes.append("contains a [placeholder] - replace it")
    sources = (resume, job["description"], job["role"], job["company"], extra)
    numbers = unsupported_numbers(letter, *sources)
    if numbers:
        notes.append("numbers not found in your resume or the job post - check them: " + ", ".join(numbers))
    # Skills must come from the resume - a tool named in the job post isn't one you have.
    terms = unsupported_terms(letter, resume, job["role"], job["company"], extra)
    if terms:
        notes.append("not on your resume - make sure the letter doesn't claim these: " + ", ".join(terms))
    # naming the role/company is fine - only flag copied requirements
    for run in copied_from_job(letter, job["description"], f"{resume} {job['role']} {job['company']}"):
        notes.append(f'copied from the job post - only keep it if it\'s true for you: "...{run}..."')
    cliches = [p for p in BANNED_PHRASES if p.lower() in letter.lower()]
    if cliches:
        notes.append("clichés to edit out: " + ", ".join(f'"{p}"' for p in cliches))
    if job["company"] and job["company"].lower() not in letter.lower():
        notes.append(f"doesn't mention {job['company']}")
    if not job["company"] and (greeting := re.search(r"Dear Hiring Team at ([^,\n]+)", letter)):
        notes.append(f'greets "{greeting[1]}", but the job post doesn\'t name the company - '
                     'change it to "Dear Hiring Team,"')
    return notes


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

PDF_STYLE = """
@page { size: A4; margin: 20mm 22mm; }
body { font-family: Calibri, "Segoe UI", Arial, sans-serif; font-size: 11pt; line-height: 1.45; color: #111; }
.name { font-size: 18pt; font-weight: 700; margin: 0; }
.contact { font-size: 9.5pt; color: #444; margin: 3px 0 18px; padding-bottom: 8px; border-bottom: 1px solid #999; }
.date { margin: 0 0 14px; }
p { margin: 0 0 10px; }
"""


def contact_lines(profile: dict) -> list[str]:
    """Email/phone/location, then profile links (LinkedIn, GitHub) on a second line."""
    links = [r["answer"] for r in profile.get("rules", [])
             if isinstance(r.get("answer"), str) and re.search(r"linkedin\.com|github\.com", r["answer"])]
    personal = [profile.get("email"), profile.get("phone"), profile.get("location")]
    web = [link.replace("https://", "").replace("www.", "") for link in dict.fromkeys(links)]
    return [" | ".join(p for p in group if p) for group in (personal, web) if any(group)]


def letter_html(letter: str, profile: dict) -> str:
    paragraphs = "".join(
        f"<p>{html.escape(block).replace(chr(10), '<br>')}</p>"
        for block in re.split(r"\n\s*\n", letter.strip())
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><style>{PDF_STYLE}</style></head><body>"
        f"<p class='name'>{html.escape(candidate_name(profile))}</p>"
        f"<p class='contact'>{'<br>'.join(html.escape(line) for line in contact_lines(profile))}</p>"
        f"<p class='date'>{datetime.now():%d %B %Y}</p>{paragraphs}</body></html>"
    )


def save_pdf(browser, letter: str, profile: dict, out: Path) -> Path:
    """Render the letter to PDF with a headless Chromium `browser` (Playwright)."""
    page = browser.new_page()
    try:
        page.set_content(letter_html(letter, profile))
        out.parent.mkdir(parents=True, exist_ok=True)
        page.pdf(path=str(out), format="A4", print_background=True)
    finally:
        page.close()
    return out


def output_paths(job: dict, profile: dict) -> tuple[Path, Path]:
    """(.txt, .pdf) for a job; the PDF name is what recruiters see."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", candidate_name(profile)).strip("_") or "Cover"
    return OUT_DIR / f"{job['slug']}.txt", OUT_DIR / f"{name}_Cover_Letter_{job['slug']}.pdf"


# --------------------------------------------------------------------------
# Log of every letter written (cover_letters/cover_letters.json)
# --------------------------------------------------------------------------

def _load_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    return json.loads(LOG_FILE.read_text("utf-8")).get("letters", [])


def _save_log(letters: list[dict]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"letters": letters}, indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, LOG_FILE)


def log_letter(job: dict, model: str, letter: str, warnings: list[str], status: str, *,
               source: str, txt: Path | None = None, pdf: Path | None = None,
               edited: bool = False, regenerations: int = 0) -> int:
    """Append a record; returns its id. status: generated | uploaded | uploaded-unreviewed | skipped."""
    letters = _load_log()
    entry_id = max((e["id"] for e in letters), default=0) + 1
    letters.append({
        "id": entry_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "status": status,
        "job_url": job.get("url"),
        "company": job["company"],
        "role": job["role"],
        "model": model,
        "edited": edited,
        "regenerations": regenerations,
        "warnings": warnings,
        "txt": str(txt.relative_to(HERE)) if txt else None,
        "pdf": str(pdf.relative_to(HERE)) if pdf else None,
        "letter": letter,
    })
    _save_log(letters)
    return entry_id


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="Greenhouse job link")
    parser.add_argument("--jd-file", type=Path, help="text file with the job description (any job site)")
    parser.add_argument("--company", help="company name (with --jd-file; leave out if the post doesn't name it)")
    parser.add_argument("--role", help="job title (with --jd-file; default: the file's first line)")
    parser.add_argument("--resume", type=Path, help="resume/CV PDF (default: the one in profile.json)")
    parser.add_argument("--notes", default="", help="extra points to include, in your own words")
    parser.add_argument("--words", type=int, default=DEFAULT_WORDS, help=f"target length (default {DEFAULT_WORDS})")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--profile", type=Path, default=HERE / "profile.json")
    parser.add_argument("--no-pdf", action="store_true", help="only write the .txt")
    args = parser.parse_args()

    try:
        if args.jd_file:
            job = file_job(args.jd_file, args.company, args.role)
        elif args.url:
            job = greenhouse_job(args.url)
        else:
            parser.error("give a Greenhouse job link or --jd-file")
        profile = json.loads(args.profile.read_text("utf-8"))

        at = f" at {job['company']}" if job["company"] else ""
        print(f"Writing a cover letter for {job['role'] or 'the role'}{at} with {args.model}.")
        print(f"Resume: {(args.resume or Path(profile['resume'])).name}")
        print("Running on CPU - expect 1-3 minutes.\n" + "-" * 70)
        result = write_letter(job, profile, args.resume, args.model, args.words, args.notes, args.temperature)
    except CoverLetterError as exc:
        sys.exit(str(exc))

    txt, pdf = output_paths(job, profile)
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(result["letter"] + "\n", "utf-8")
    if args.no_pdf:
        pdf = None
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            save_pdf(browser, result["letter"], profile, pdf)
            browser.close()
    log_letter(job, args.model, result["letter"], result["warnings"], "generated",
               source="cover_letter.py", txt=txt, pdf=pdf)

    print("-" * 70)
    for note in result["warnings"]:
        print(f"  ! {note}")
    print(f"Saved: {txt}" + (f"\n       {pdf}" if pdf else ""))
    print(f"Logged in {LOG_FILE}")
    print("Read it through and edit before sending - small models can still get details wrong.")


if __name__ == "__main__":
    main()
