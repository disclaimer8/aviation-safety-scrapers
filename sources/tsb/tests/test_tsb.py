# tests/test_tsb.py
"""Offline tests for tsb_ingest.tsb using saved HTML fixtures."""
import os
import re

import pytest

from tsb_ingest import tsb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────────────────────────────────────
# parse_index
# ──────────────────────────────────────────────────────────────────────────────

class TestParseIndex:
    def setup_method(self):
        self.html = _fixture("tsb_index.html")
        self.rows = tsb.parse_index(self.html)

    def test_returns_more_than_1000_rows(self):
        assert len(self.rows) > 1000, (
            f"Expected >1000 rows, got {len(self.rows)}"
        )

    def test_all_rows_have_required_keys(self):
        required = {
            "case_id", "report_url", "event_date",
            "occurrence_type", "operator", "aircraft", "location",
            "occurrence_status",
        }
        for row in self.rows:
            assert required <= row.keys(), (
                f"Missing keys {required - row.keys()} in row {row.get('case_id')}"
            )

    def test_case_id_pattern_on_known_row(self):
        """A known case_id matches the TSB aviation pattern A<YY><REGION><NNNN>."""
        pat = re.compile(r"^[A-Z]\d{2}[A-Z]\d{4}$")
        matching = [r for r in self.rows if pat.match(r["case_id"])]
        assert len(matching) > 1000, (
            f"Expected >1000 rows with standard case_id, got {len(matching)}"
        )

    def test_known_case_id_a11q0170(self):
        ids = {r["case_id"] for r in self.rows}
        assert "A11Q0170" in ids, "A11Q0170 not found in parsed rows"

    def test_known_case_id_a24a0019(self):
        ids = {r["case_id"] for r in self.rows}
        assert "A24A0019" in ids, "A24A0019 not found in parsed rows"

    def test_report_url_is_absolute_https(self):
        for row in self.rows:
            assert row["report_url"].startswith("https://"), (
                f"report_url not absolute: {row['report_url']!r}"
            )

    def test_report_url_contains_tsb_domain(self):
        for row in self.rows:
            assert "tsb.gc.ca" in row["report_url"], (
                f"Unexpected domain in report_url: {row['report_url']!r}"
            )

    def test_event_date_format_when_present(self):
        date_pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for row in self.rows:
            if row["event_date"] is not None:
                assert date_pat.match(row["event_date"]), (
                    f"Unexpected event_date format: {row['event_date']!r} "
                    f"for {row['case_id']}"
                )

    def test_most_rows_have_event_date(self):
        with_date = [r for r in self.rows if r["event_date"] is not None]
        assert len(with_date) > 1000, (
            f"Expected >1000 rows with event_date, got {len(with_date)}"
        )

    def test_occurrence_status_non_empty(self):
        for row in self.rows:
            assert row["occurrence_status"], (
                f"Empty occurrence_status for {row['case_id']!r}"
            )

    def test_a11q0170_fields(self):
        row = next(r for r in self.rows if r["case_id"] == "A11Q0170")
        assert row["event_date"] == "2011-08-29"
        assert row["occurrence_status"] == "Completed"
        assert row["occurrence_type"] == "Risk of collision"
        assert row["report_url"].startswith("https://www.tsb.gc.ca")
        assert "a11q0170" in row["report_url"].lower()

    def test_a24a0019_fields(self):
        row = next(r for r in self.rows if r["case_id"] == "A24A0019")
        assert row["event_date"] == "2024-05-02"
        assert row["occurrence_type"] == "Collision with terrain"
        assert "Custom Helicopters" in (row["operator"] or "")
        assert row["aircraft"] is not None
        assert row["location"] is not None

    def test_no_duplicate_case_ids(self):
        ids = [r["case_id"] for r in self.rows]
        assert len(ids) == len(set(ids)), "Duplicate case_ids found"

    def test_report_url_ends_with_html(self):
        for row in self.rows:
            assert row["report_url"].endswith(".html"), (
                f"report_url does not end with .html: {row['report_url']!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# parse_report – a11q0170 (completed investigation, multiple sections)
# ──────────────────────────────────────────────────────────────────────────────

class TestParseReportA11Q0170:
    def setup_method(self):
        self.html = _fixture("tsb_report_a11q0170.html")
        self.text = tsb.parse_report(self.html)

    def test_returns_more_than_2000_chars(self):
        assert len(self.text) > 2000, (
            f"Expected >2000 chars, got {len(self.text)}"
        )

    def test_no_disclaimer_sentence(self):
        assert "not the function of the Board to assign fault" not in self.text, (
            "Disclaimer sentence leaked into narrative"
        )

    def test_no_skip_to_main_content(self):
        assert "Skip to main content" not in self.text

    def test_no_date_modified_footer(self):
        assert "Date modified" not in self.text

    def test_contains_factual_content(self):
        # The summary section mentions the involved aircraft
        assert "DHC-8" in self.text or "Bombardier" in self.text, (
            "Expected aircraft reference in narrative"
        )

    def test_no_excessive_whitespace(self):
        # Should not have runs of 4+ blank lines
        assert "\n\n\n\n" not in self.text


# ──────────────────────────────────────────────────────────────────────────────
# parse_report – a24a0019 (active/recently-completed investigation, minimal)
# ──────────────────────────────────────────────────────────────────────────────

class TestParseReportA24A0019:
    def setup_method(self):
        self.html = _fixture("tsb_report_a24a0019.html")
        self.text = tsb.parse_report(self.html)

    def test_returns_more_than_1000_chars(self):
        # a24a0019 is a Class 4 investigation — inherently shorter content
        assert len(self.text) > 1000, (
            f"Expected >1000 chars, got {len(self.text)}"
        )

    def test_no_disclaimer_sentence(self):
        assert "not the function of the Board to assign fault" not in self.text, (
            "Disclaimer sentence leaked into narrative"
        )

    def test_no_skip_to_main_content(self):
        assert "Skip to main content" not in self.text

    def test_contains_occurrence_narrative(self):
        # The occurrence narrative mentions Bell 206L
        assert "Bell" in self.text, "Expected Bell helicopter reference in narrative"

    def test_contains_location_reference(self):
        assert "Goose Bay" in self.text or "Newfoundland" in self.text


# ──────────────────────────────────────────────────────────────────────────────
# Module-level constants sanity checks
# ──────────────────────────────────────────────────────────────────────────────

def test_index_url_is_tsb():
    assert tsb.INDEX_URL.startswith("https://www.tsb.gc.ca")

def test_base_is_tsb():
    assert tsb.BASE == "https://www.tsb.gc.ca"

def test_delay_is_float():
    assert isinstance(tsb.DELAY, float)
    assert tsb.DELAY >= 0.5

def test_ua_constant_is_non_empty():
    assert tsb.UA and "Mozilla" in tsb.UA

def test_headers_has_user_agent():
    assert "User-Agent" in tsb.HEADERS
    assert tsb.HEADERS["User-Agent"] == tsb.UA


# ──────────────────────────────────────────────────────────────────────────────
# iter_index and fetch_report with fake client
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, index_html: str, report_html: str = ""):
        self._index = index_html
        self._report = report_html
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if url == tsb.INDEX_URL:
            return _FakeResp(self._index)
        return _FakeResp(self._report)


class TestIterIndex:
    def setup_method(self):
        self.html = _fixture("tsb_index.html")
        self.client = _FakeClient(self.html)

    def test_returns_list(self):
        rows = tsb.iter_index(self.client)
        assert isinstance(rows, list)

    def test_returns_more_than_1000_rows(self):
        rows = tsb.iter_index(self.client)
        assert len(rows) > 1000

    def test_calls_index_url(self):
        tsb.iter_index(self.client)
        assert tsb.INDEX_URL in self.client.calls


class TestFetchReport:
    def setup_method(self):
        self.report_html = _fixture("tsb_report_a11q0170.html")
        self.client = _FakeClient("", self.report_html)
        self.url = "https://www.tsb.gc.ca/eng/rapports-reports/aviation/2011/a11q0170/a11q0170.html"

    def test_returns_html_text(self):
        text = tsb.fetch_report(self.client, self.url)
        assert isinstance(text, str)
        assert len(text) > 10000

    def test_calls_given_url(self):
        tsb.fetch_report(self.client, self.url)
        assert self.url in self.client.calls
