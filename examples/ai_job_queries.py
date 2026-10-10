"""Run the AI/ML job queries against the public dataset; append hits to a Google Sheet or CSV.

Each query mirrors a Google search over ATS job boards::

    1. startup/tech ATS   (site:ashby/greenhouse/lever)      AI/ML/LLM/GenAI titles,
                                                              remote US, no hybrid/onsite
    2. enterprise ATS     (site:workday/icims/smartrecruiters/workable)
                                                              AI/ML/GenAI/NLP titles, remote US
    3. data scientist     (site:ashby/greenhouse/lever/workday)
                                                              Data/Applied Scientist titles that
                                                              mention LLM/GenAI, remote US

``site:`` maps to the dataset's ``ats_type``, ``intitle:`` to the job title,
and other quoted terms to the title/location/description text.

Usage:
    python examples/ai_job_queries.py                  # asks which timeline to use
    python examples/ai_job_queries.py --timeline 24h   # posted in the last 24 hours
    python examples/ai_job_queries.py --timeline 7d --output my_jobs.csv
    python examples/ai_job_queries.py --timeline all --queries 1 3
    python examples/ai_job_queries.py --timeline 24h --sheet <sheet URL or key>

Google Sheets: pass ``--sheet``, or put ``ATS_JOBS_SHEET=<sheet URL or key>``
in a ``.env`` file next to where you run the script (or in the environment).
Rows go to the ``--tab`` worksheet (default ``ATSjobs``, created if missing)
using the service-account key in ``--credentials`` (default
``credentials.json``). Share the sheet with that key's ``client_email`` as an
Editor. Needs the sheets extra: ``uv run --extra parquet --extra sheets ...``.
Without a sheet the results go to ``--output`` (default ``ai_jobs.csv``).

Timelines: ``all`` or ``<N>h`` / ``<N>d`` / ``<N>w`` (e.g. ``12h``, ``3d``, ``2w``),
measured against each job's ``posted_at``. Rows are appended; jobs already in
the sheet/CSV are skipped.

Requires the parquet extra: ``pip install "ats-scrapers[parquet]"``. The first
run downloads about 850 MB of Parquet slices into ``--cache-dir``; later runs
reuse them until the hosted dataset refreshes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from ats_scrapers import Client
from ats_scrapers.client import _normalize_dataset

TIMELINE_PRESETS = {"1": "24h", "2": "3d", "3": "7d", "4": "30d", "5": "all"}

OUTPUT_COLUMNS = [
    "query",
    "search_query",
    "title",
    "company",
    "ats_type",
    "location",
    "country_iso",
    "is_remote",
    "salary_summary",
    "posted_at",
    "url",
    "apply_url",
    "global_id",
    "timeline",
    "searched_at",
]

READ_COLUMNS = [
    "url",
    "title",
    "company",
    "ats_type",
    "ats_id",
    "location",
    "country_iso",
    "is_remote",
    "salary_summary",
    "description",
    "posted_at",
    "apply_url",
]


@dataclass(frozen=True)
class Query:
    name: str
    ats: tuple[str, ...]
    title_any: tuple[str, ...]
    text_any: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # The original Google query, written to the ``search_query`` column.
    search: str = ""


QUERIES = {
    "1": Query(
        name="startup_tech_ats",
        search=(
            "(site:jobs.ashbyhq.com OR site:boards.greenhouse.io OR "
            "site:job-boards.greenhouse.io OR site:jobs.lever.co) "
            '(intitle:"AI Engineer" OR intitle:"ML Engineer" OR '
            'intitle:"Machine Learning Engineer" OR intitle:"LLM Engineer" OR '
            'intitle:"GenAI") remote ("United States" OR "USA") -hybrid -onsite'
        ),
        ats=("ashby", "greenhouse", "lever"),
        title_any=(
            "AI Engineer",
            "ML Engineer",
            "Machine Learning Engineer",
            "LLM Engineer",
            "GenAI",
        ),
        exclude=("hybrid", "onsite", "on-site"),
    ),
    "2": Query(
        name="enterprise_ats",
        search=(
            "(site:myworkdayjobs.com OR site:icims.com OR site:smartrecruiters.com OR "
            'site:apply.workable.com) (intitle:"AI Engineer" OR '
            'intitle:"Machine Learning Engineer" OR intitle:"Generative AI" OR '
            'intitle:"NLP Engineer") remote ("United States" OR "USA")'
        ),
        ats=("workday", "icims", "smartrecruiters", "workable"),
        title_any=(
            "AI Engineer",
            "Machine Learning Engineer",
            "Generative AI",
            "NLP Engineer",
        ),
    ),
    "3": Query(
        name="data_scientist_llm",
        search=(
            "(site:jobs.ashbyhq.com OR site:boards.greenhouse.io OR site:jobs.lever.co OR "
            'site:myworkdayjobs.com) (intitle:"Data Scientist" OR '
            'intitle:"Applied Scientist") ("LLM" OR "GenAI" OR "Generative AI") '
            'remote ("United States" OR "USA")'
        ),
        ats=("ashby", "greenhouse", "lever", "workday"),
        title_any=("Data Scientist", "Applied Scientist"),
        text_any=("LLM", "GenAI", "Generative AI"),
    ),
}

# "US" stays case-sensitive so the word "us" in free text doesn't count.
_US_TEXT = re.compile(r"(?i:United States|\bUSA\b)|\bU\.S\.|\bUS\b")
_US_DESCRIPTION = re.compile(r"United States|\bUSA\b", re.IGNORECASE)


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """Case-insensitive whole-phrase match, like Google's quoted terms."""
    alternatives = (r"\s+".join(map(re.escape, p.split())) for p in phrases)
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


def _text(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series("", index=df.index)
    return df[column].fillna("").astype(str)


def parse_timeline(value: str) -> timedelta | None:
    """``all`` -> None, otherwise ``<N>h|d|w`` -> timedelta."""
    value = value.strip().lower()
    if value == "all":
        return None
    match = re.fullmatch(r"(\d+)\s*([hdw])", value)
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"Invalid timeline {value!r}; use 'all' or e.g. 24h, 3d, 2w")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[unit]: amount})


def ask_timeline() -> str:
    print("Choose a timeline:")
    print("  1) last 24 hours   2) last 3 days   3) last 7 days   4) last 30 days   5) all")
    print("  ...or type a custom value such as 12h, 48h, 14d, 2w")
    while True:
        choice = input("Timeline [1]: ").strip() or "1"
        value = TIMELINE_PRESETS.get(choice, choice)
        try:
            parse_timeline(value)
        except ValueError as exc:
            print(exc)
            continue
        return value


def filter_query(
    df: pd.DataFrame, query: Query, *, window: timedelta | None, now: datetime
) -> pd.DataFrame:
    """Apply one query's title, remote-US, keyword, exclusion and date filters."""
    df = df[_text(df, "ats_type").str.lower().isin(query.ats)]
    title = _text(df, "title")
    location = _text(df, "location")
    description = _text(df, "description")

    mask = title.str.contains(_phrase_pattern(query.title_any))

    # remote: structured flag, or "remote" in the title/location text.
    remote_text = (title + " " + location).str.contains("remote", case=False, regex=False)
    mask &= _text(df, "is_remote").str.lower().eq("true") | remote_text

    # ("United States" OR "USA"): country code, location text, or the posting body.
    country = _text(df, "country_iso").str.upper()
    us = country.eq("US") | location.str.contains(_US_TEXT)
    us |= country.eq("") & description.str.contains(_US_DESCRIPTION)
    mask &= us

    if query.text_any:
        body = title + " " + description
        mask &= body.str.contains(_phrase_pattern(query.text_any))

    if query.exclude:
        # Exclusions apply to title/location only; descriptions routinely say
        # "not onsite" or "no hybrid" and would knock out genuine remote roles.
        mask &= ~(title + " " + location).str.contains(_phrase_pattern(query.exclude))

    if window is not None:
        posted = pd.to_datetime(_text(df, "posted_at"), utc=True, errors="coerce", format="mixed")
        mask &= posted >= now - window

    return df[mask]


class ParquetSlices:
    """Stream per-ATS Parquet slices from the hosted dataset in batches.

    Slices are cached under ``cache_dir`` by checksum, so repeated runs (e.g.
    with a different timeline) skip the download until the dataset refreshes.
    Only the columns the queries need are read, which keeps the multi-GB
    Workday slice out of memory.
    """

    def __init__(self, cache_dir: Path, client: Client | None = None) -> None:
        self.cache_dir = cache_dir
        self.client = client or Client(prefer_parquet=True)

    def _path(self, ats: str) -> Path:
        entry = self.client.manifest.by_ats[ats]
        # The CSV checksum changes whenever the slice is republished.
        digest = (entry.sha256 or self.client.manifest.generated_at.strftime("%Y%m%d%H%M"))[:16]
        path = self.cache_dir / f"{ats}-{digest}.parquet"
        if path.exists():
            return path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.cache_dir.glob(f"{ats}-*.parquet"):
            stale.unlink()
        url = self.client.manifest.url_for_ats(ats, prefer_parquet=True)
        print(f"  downloading {ats} ({entry.rows:,} jobs) ...", flush=True)
        partial = path.with_suffix(".part")
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
            response.raise_for_status()
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 20):
                    fh.write(chunk)
        partial.replace(path)
        return path

    def __call__(self, ats: str) -> Iterator[pd.DataFrame]:
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(self._path(ats))
        columns = [c for c in READ_COLUMNS if c in parquet.schema_arrow.names]
        for batch in parquet.iter_batches(batch_size=50_000, columns=columns):
            yield _normalize_dataset(batch.to_pandas())


def run(
    query_keys: list[str],
    timeline: str,
    load_batches: Callable[[str], Iterable[pd.DataFrame]],
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    now = now or datetime.now(UTC)
    window = parse_timeline(timeline)
    selected = [QUERIES[k] for k in query_keys]

    frames = []
    counts = dict.fromkeys((q.name for q in selected), 0)
    for ats in sorted({a for q in selected for a in q.ats}):
        print(f"  searching {ats} ...", flush=True)
        for batch in load_batches(ats):
            for query in (q for q in selected if ats in q.ats):
                hits = filter_query(batch, query, window=window, now=now)
                if not hits.empty:
                    hits = hits.drop(columns=["description"], errors="ignore").copy()
                    hits["query"] = query.name
                    counts[query.name] += len(hits)
                    frames.append(hits)
    for name, count in counts.items():
        print(f"  {name}: {count} jobs")

    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    results = pd.concat(frames, ignore_index=True)
    if results.empty:
        return results.reindex(columns=OUTPUT_COLUMNS)

    # A job matched by several queries becomes one row listing every query.
    key = results["global_id"].fillna(results["url"]).astype(str)
    queries = results.groupby(key)["query"].agg(lambda s: "; ".join(dict.fromkeys(s)))
    results = results.assign(_key=key).drop_duplicates("_key")
    results["query"] = results["_key"].map(queries)
    search_text = {q.name: q.search for q in selected}
    results["search_query"] = results["query"].map(
        lambda names: " | ".join(search_text[n] for n in names.split("; "))
    )
    results["timeline"] = timeline
    results["searched_at"] = now.isoformat(timespec="seconds")
    results = results.sort_values("posted_at", ascending=False, na_position="last")
    return results.reindex(columns=OUTPUT_COLUMNS).reset_index(drop=True)


def _unseen(results: pd.DataFrame, seen: set[str]) -> pd.DataFrame:
    return results[~(results["global_id"].isin(seen) | results["url"].isin(seen))]


def _upgrade_columns(existing: pd.DataFrame) -> pd.DataFrame:
    """Bring rows written by an older version up to ``OUTPUT_COLUMNS``.

    Missing columns are inserted in their standard position (columns the
    user added stay at the end), and ``search_query`` is backfilled from
    each row's ``query`` names.
    """
    extra = [c for c in existing.columns if c not in OUTPUT_COLUMNS]
    upgraded = existing.reindex(columns=OUTPUT_COLUMNS + extra)
    if "search_query" not in existing.columns and "query" in existing.columns:
        search_text = {q.name: q.search for q in QUERIES.values()}
        upgraded["search_query"] = upgraded["query"].map(
            lambda names: (
                " | ".join(
                    search_text.get(n, "") for n in str(names).split("; ") if n in search_text
                )
                if isinstance(names, str)
                else ""
            )
        )
    return upgraded


def append_csv(results: pd.DataFrame, output: Path) -> int:
    """Append rows whose job isn't already in ``output``; return how many were added."""
    if output.exists() and output.stat().st_size > 0:
        existing = pd.read_csv(output, dtype=str, keep_default_na=False)
        seen = set(existing.get("global_id", pd.Series(dtype=str))) - {""}
        seen |= set(existing.get("url", pd.Series(dtype=str))) - {""}
        new = _unseen(results, seen)
        if set(OUTPUT_COLUMNS) - set(existing.columns):
            existing = _upgrade_columns(existing)
            existing.to_csv(output, index=False)
        new.reindex(columns=existing.columns).to_csv(output, mode="a", header=False, index=False)
        return len(new)
    output.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(output, index=False)
    return len(results)


def open_worksheet(sheet: str, tab: str, credentials: Path):
    """Open (or create) ``tab`` in a Google Sheet given by URL or key."""
    try:
        import gspread
    except ImportError:
        raise SystemExit(
            "Writing to Google Sheets needs gspread. Run with the sheets extra:\n"
            "  uv run --extra parquet --extra sheets python examples/ai_job_queries.py"
        ) from None

    gc = gspread.service_account(filename=str(credentials))
    book = gc.open_by_url(sheet) if sheet.startswith("http") else gc.open_by_key(sheet)
    try:
        return book.worksheet(tab)
    except gspread.WorksheetNotFound:
        return book.add_worksheet(title=tab, rows=1000, cols=len(OUTPUT_COLUMNS))


def append_sheet(results: pd.DataFrame, worksheet) -> int:
    """Append rows whose job isn't already in ``worksheet``; return how many were added."""
    existing = worksheet.get_all_values()
    header = existing[0] if existing and any(existing[0]) else []
    if not header:
        header = OUTPUT_COLUMNS
        worksheet.append_rows([header], value_input_option="RAW")
    elif set(OUTPUT_COLUMNS) - set(header):
        width = len(header)
        old = pd.DataFrame([(r + [""] * width)[:width] for r in existing[1:]], columns=header)
        upgraded = _upgrade_columns(old).fillna("")
        header = upgraded.columns.tolist()
        existing = [header, *upgraded.astype(str).values.tolist()]
        if getattr(worksheet, "col_count", len(header)) < len(header):
            worksheet.add_cols(len(header) - worksheet.col_count)
        worksheet.clear()
        worksheet.update(existing, "A1", value_input_option="RAW")
    seen = {
        row[i]
        for row in existing[1:]
        for i, name in enumerate(header)
        if name in {"global_id", "url"} and i < len(row) and row[i]
    }
    new = _unseen(results, seen).reindex(columns=header)
    if new.empty:
        return 0
    values = new.astype(object).where(new.notna(), "").astype(str).values.tolist()
    worksheet.append_rows(values, value_input_option="RAW")
    return len(new)


def load_env_file(path: Path) -> None:
    """Set ``KEY=value`` lines from ``path`` without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(".env"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--timeline",
        help="all, or a window like 24h, 3d, 7d, 2w (prompted when omitted)",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        choices=sorted(QUERIES),
        default=sorted(QUERIES),
        help="which queries to run (default: all)",
    )
    parser.add_argument(
        "--sheet",
        default=os.environ.get("ATS_JOBS_SHEET"),
        help="Google Sheet URL or key to append to (default: ATS_JOBS_SHEET from env or .env)",
    )
    parser.add_argument("--tab", default="ATSjobs", help="worksheet tab name")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("credentials.json"),
        help="Google service-account key file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV to append to (default: ai_jobs.csv when no --sheet is given)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".ats_cache"),
        help="where downloaded dataset slices are kept between runs",
    )
    args = parser.parse_args(argv)
    if args.sheet is None and args.output is None:
        args.output = Path("ai_jobs.csv")
    if args.sheet and not args.credentials.exists():
        parser.error(f"credentials file not found: {args.credentials}")

    timeline = args.timeline
    if timeline is None:
        timeline = ask_timeline() if sys.stdin.isatty() else "24h"
    try:
        parse_timeline(timeline)
    except ValueError as exc:
        parser.error(str(exc))

    worksheet = open_worksheet(args.sheet, args.tab, args.credentials) if args.sheet else None

    print(f"Running queries {', '.join(args.queries)} (timeline: {timeline})")
    with Client(prefer_parquet=True) as client:
        results = run(args.queries, timeline, ParquetSlices(args.cache_dir, client))
    print(f"{len(results)} matching jobs")
    if worksheet is not None:
        added = append_sheet(results, worksheet)
        print(f"  {added} new rows appended to Google Sheet tab {args.tab!r}")
    if args.output is not None:
        added = append_csv(results, args.output)
        print(f"  {added} new rows appended to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
