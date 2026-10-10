"""Tests for examples/ai_job_queries.py filtering, timelines and CSV appending."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ai_job_queries", Path(__file__).parents[1] / "examples" / "ai_job_queries.py"
)
ai_job_queries = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ai_job_queries
_SPEC.loader.exec_module(ai_job_queries)

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def _job(gid, title, ats="ashby", location="Remote - United States", **extra):
    row = {
        "global_id": gid,
        "url": f"https://example.com/{gid}",
        "title": title,
        "company": "Acme",
        "ats_type": ats,
        "location": location,
        "country_iso": "US",
        "is_remote": True,
        "description": "",
        "posted_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    row.update(extra)
    return row


class FakeClient:
    """Stands in for ParquetSlices: yields each ATS slice as one batch."""

    def __init__(self, rows):
        self.df = pd.DataFrame(rows)

    def __call__(self, ats):
        yield self.df[self.df["ats_type"] == ats].reset_index(drop=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("all", None),
        ("24h", timedelta(hours=24)),
        ("3d", timedelta(days=3)),
        ("2W", timedelta(weeks=2)),
    ],
)
def test_parse_timeline(value, expected):
    assert ai_job_queries.parse_timeline(value) == expected


@pytest.mark.parametrize("value", ["", "0h", "yesterday", "5m"])
def test_parse_timeline_rejects_bad_values(value):
    with pytest.raises(ValueError):
        ai_job_queries.parse_timeline(value)


def test_query_filters():
    rows = [
        _job("keep", "Senior AI Engineer"),
        _job("hybrid", "ML Engineer (Hybrid)"),
        _job("not-us", "AI Engineer", location="Remote - Germany", country_iso="DE"),
        _job("onsite", "AI Engineer", location="New York, NY", is_remote=False, country_iso=""),
        _job("wrong-title", "Account Executive"),
        _job("old", "LLM Engineer", posted_at=(NOW - timedelta(days=5)).isoformat()),
        _job("ds-llm", "Data Scientist", ats="lever", description="Build LLM evals"),
        _job("ds-plain", "Data Scientist", ats="lever", description="Forecasting"),
        _job("ent", "Generative AI Engineer", ats="workday", location="Remote, USA"),
    ]
    out = ai_job_queries.run(["1", "2", "3"], "24h", FakeClient(rows), now=NOW)
    assert set(out["global_id"]) == {"keep", "ds-llm", "ent"}

    everything = ai_job_queries.run(["1"], "all", FakeClient(rows), now=NOW)
    assert set(everything["global_id"]) == {"keep", "old"}


def test_job_matching_several_queries_is_one_row():
    rows = [_job("both", "AI Engineer", ats="greenhouse")]
    rows[0]["title"] = "Applied Scientist, GenAI"
    out = ai_job_queries.run(["1", "3"], "all", FakeClient(rows), now=NOW)
    assert len(out) == 1
    assert out.loc[0, "query"] == "startup_tech_ats; data_scientist_llm"


def test_append_csv_skips_existing_jobs(tmp_path):
    output = tmp_path / "jobs.csv"
    first = ai_job_queries.run(["1"], "all", FakeClient([_job("a", "AI Engineer")]), now=NOW)
    assert ai_job_queries.append_csv(first, output) == 1

    rows = [_job("a", "AI Engineer"), _job("b", "ML Engineer")]
    second = ai_job_queries.run(["1"], "all", FakeClient(rows), now=NOW)
    assert ai_job_queries.append_csv(second, output) == 1

    saved = pd.read_csv(output)
    assert saved["global_id"].tolist() == ["a", "b"]
    assert saved.columns.tolist() == ai_job_queries.OUTPUT_COLUMNS


class FakeWorksheet:
    def __init__(self, values=None):
        self.values = values or []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def append_rows(self, rows, value_input_option):
        assert value_input_option == "RAW"
        self.values.extend(rows)


def test_append_sheet_writes_header_then_skips_existing_jobs():
    sheet = FakeWorksheet()
    first = ai_job_queries.run(["1"], "all", FakeClient([_job("a", "AI Engineer")]), now=NOW)
    assert ai_job_queries.append_sheet(first, sheet) == 1
    assert sheet.values[0] == ai_job_queries.OUTPUT_COLUMNS

    rows = [_job("a", "AI Engineer"), _job("b", "ML Engineer", salary_summary=None)]
    second = ai_job_queries.run(["1"], "all", FakeClient(rows), now=NOW)
    assert ai_job_queries.append_sheet(second, sheet) == 1

    gid = ai_job_queries.OUTPUT_COLUMNS.index("global_id")
    assert [r[gid] for r in sheet.values[1:]] == ["a", "b"]
    assert all(isinstance(v, str) for r in sheet.values for v in r)
    assert ai_job_queries.append_sheet(second, sheet) == 0


def test_search_query_column_holds_original_query_text():
    rows = [_job("both", "Applied Scientist, GenAI", ats="greenhouse")]
    out = ai_job_queries.run(["1", "3"], "all", FakeClient(rows), now=NOW)
    q = ai_job_queries.QUERIES
    assert out.loc[0, "search_query"] == f"{q['1'].search} | {q['3'].search}"
    assert 'intitle:"GenAI"' in q["1"].search


def _old_layout(values):
    """Rows as written before the search_query column existed."""
    header = [c for c in ai_job_queries.OUTPUT_COLUMNS if c != "search_query"]
    return [header, *[[v.get(c, "") for c in header] for v in values]]


class UpgradableWorksheet(FakeWorksheet):
    col_count = 26

    def clear(self):
        self.values = []

    def update(self, values, range_name, value_input_option):
        assert range_name == "A1"
        self.values = [list(r) for r in values]


def test_append_sheet_adds_search_query_to_existing_rows():
    sheet = UpgradableWorksheet(_old_layout([{"query": "enterprise_ats", "global_id": "old"}]))
    rows = [_job("new", "AI Engineer")]
    results = ai_job_queries.run(["1"], "all", FakeClient(rows), now=NOW)
    assert ai_job_queries.append_sheet(results, sheet) == 1

    header = sheet.values[0]
    assert header == ai_job_queries.OUTPUT_COLUMNS
    col = header.index("search_query")
    assert sheet.values[1][col] == ai_job_queries.QUERIES["2"].search
    assert sheet.values[2][col] == ai_job_queries.QUERIES["1"].search


def test_append_csv_adds_search_query_to_existing_file(tmp_path):
    output = tmp_path / "jobs.csv"
    old = _old_layout([{"query": "data_scientist_llm", "global_id": "old"}])
    pd.DataFrame(old[1:], columns=old[0]).to_csv(output, index=False)
    results = ai_job_queries.run(["1"], "all", FakeClient([_job("new", "AI Engineer")]), now=NOW)
    assert ai_job_queries.append_csv(results, output) == 1

    saved = pd.read_csv(output)
    assert saved.columns.tolist() == ai_job_queries.OUTPUT_COLUMNS
    assert saved["search_query"].tolist() == [
        ai_job_queries.QUERIES["3"].search,
        ai_job_queries.QUERIES["1"].search,
    ]


def test_load_env_file_sets_unset_keys_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nATS_JOBS_SHEET="abc"\nexport KEEP=from-file\n', encoding="utf-8")
    monkeypatch.delenv("ATS_JOBS_SHEET", raising=False)
    monkeypatch.setenv("KEEP", "from-env")

    ai_job_queries.load_env_file(env)

    assert ai_job_queries.os.environ["ATS_JOBS_SHEET"] == "abc"
    assert ai_job_queries.os.environ["KEEP"] == "from-env"
