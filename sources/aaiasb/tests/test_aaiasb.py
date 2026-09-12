"""Greece: AAIASB (the predecessor site) and HARSIA (the successor).

The package had no tests at all on the host, and 209 of its rows are in
production. These are characterisation tests as much as correctness ones —
they pin what the parser does today against a real listing page, so a later
change cannot alter it silently.
"""
import pathlib

from aaiasb_ingest import aaiasb

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LISTING = (FIXTURES / "aaiasb_listing.html").read_text(encoding="utf-8", errors="replace")


def test_the_listing_parses_to_the_expected_number_of_rows():
    rows = aaiasb.parse_listing(LISTING)
    assert len(rows) == 50, f"the listing page yields 50 entries, got {len(rows)}"


def test_every_row_carries_the_fields_the_pipeline_inserts():
    for r in aaiasb.parse_listing(LISTING):
        for field in ("case_id", "node_id", "report_url", "report_no"):
            assert r.get(field), f"{field} missing from {r.get('case_id') or r}"


def test_case_ids_are_unique_within_a_page():
    ids = [r["case_id"] for r in aaiasb.parse_listing(LISTING)]
    assert len(set(ids)) == len(ids)


def test_case_id_is_the_report_number_with_a_dash():
    """01/2023 -> 01-2023. The slash cannot survive into a slug or a filename."""
    rows = {r["case_id"]: r for r in aaiasb.parse_listing(LISTING)}
    assert "01-2023" in rows
    assert rows["01-2023"]["report_no"] == "01/2023"
    for cid in rows:
        assert "/" not in cid


def test_dates_are_iso_or_absent():
    for r in aaiasb.parse_listing(LISTING):
        d = r.get("date_of_occurrence")
        if d:
            assert len(d) == 10 and d[4] == "-" and d[7] == "-", f"not ISO: {d!r}"


def test_report_urls_stay_on_the_authority_host():
    """Compare the parsed host, not a prefix: '...aaiasb.eu.evil' would satisfy
    startswith. Nothing here validates hosts that way — httpc's SSRF guard
    does — but an assertion that cannot fail the case it names is not worth
    keeping."""
    from urllib.parse import urlsplit

    for r in aaiasb.parse_listing(LISTING):
        parts = urlsplit(r["report_url"])
        assert parts.scheme == "https"
        assert parts.hostname == "www.aaiasb.eu"


def test_an_empty_page_parses_to_nothing_rather_than_raising():
    assert aaiasb.parse_listing("<html><body></body></html>") == []
    assert aaiasb.parse_listing("") == []


def test_normalize_case_id_handles_both_series_the_site_publishes():
    """AAIASB numbers reports two ways and both appear on one page.

    Plain "01/2023" and an E-series "E01/2022" — 21 of the fixture's 50
    rows are the E form. The E form lowercases; both turn the slash into a dash so the id
    can be a slug and a filename.
    """
    assert aaiasb.normalize_case_id("01/2023") == "01-2023"
    assert aaiasb.normalize_case_id("01-2023") == "01-2023"
    assert aaiasb.normalize_case_id("E01/2022") == "e01-2022"
    assert aaiasb.normalize_case_id("E02/2016") == "e02-2016"


def test_the_e_series_survives_parsing_and_is_a_fifth_of_the_page():
    """Worth pinning: an id scheme that silently stopped being recognised
    would drop 21 of 50 rows while the run still reported success."""
    ids = [r["case_id"] for r in aaiasb.parse_listing(LISTING)]
    e_series = [i for i in ids if i.startswith("e")]
    assert len(e_series) == 21, f"expected 21 E-series rows, got {len(e_series)}"


def test_report_numbers_arrive_zero_padded():
    """Measured, not assumed: 61 of 61 on the live listing and every plain
    number in the fixture. normalize_case_id does NOT pad, and does not need
    to — but if the site ever emits "1/2024", it would become a second id for
    the same report, so this is the tripwire for that."""
    import re

    for r in aaiasb.parse_listing(LISTING):
        no = r.get("report_no") or ""
        m = re.match(r"^E?(\d+)/\d{4}$", no)
        if m:
            assert len(m.group(1)) == 2, f"unpadded report number: {no!r}"
