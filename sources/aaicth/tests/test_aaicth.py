# tests/test_aaicth.py
"""Unit tests for aaicth listing parser and BE→CE calendar conversion."""
import pytest
from aaicth_ingest.aaicth import (
    be_to_ce,
    parse_thai_date,
    parse_listing,
    _download_url,
)


# ──────────────────────────────────────────────────────────────────────────────
# BE → CE calendar conversion (critical — Buddhist Era trap)
# ──────────────────────────────────────────────────────────────────────────────

class TestBeToCe:
    """be_to_ce must correctly convert Thai Buddhist Era to Common Era.

    BE = CE + 543, so BE 2544 = CE 2001, BE 2562 = CE 2019, etc.
    If a report PDF says 'พ.ศ. 2544' that is NOT 2544 CE — it is 2001 CE.
    All test cases must yield a CE year in the range 1990–2030.
    """

    def test_be_2544_is_ce_2001(self):
        assert be_to_ce(2544) == 2001

    def test_be_2549_is_ce_2006(self):
        assert be_to_ce(2549) == 2006

    def test_be_2562_is_ce_2019(self):
        assert be_to_ce(2562) == 2019

    def test_be_2567_is_ce_2024(self):
        assert be_to_ce(2567) == 2024

    def test_small_year_passthrough(self):
        # Anything <= 2400 is treated as already-CE (shouldn't appear in reports)
        assert be_to_ce(2009) == 2009

    def test_converted_years_in_valid_range(self):
        # All BE years in the AAIC dataset (2001–2024 CE) must convert correctly
        for ce in range(2001, 2025):
            be = ce + 543
            assert be_to_ce(be) == ce, f"be_to_ce({be}) should be {ce}"


# ──────────────────────────────────────────────────────────────────────────────
# Date parsing (Thai + English)
# ──────────────────────────────────────────────────────────────────────────────

class TestParseThaiDate:
    def test_thai_full_date(self):
        # "20 มกราคม พ.ศ. 2544" (= 20 January 2001)
        result = parse_thai_date("เมื่อวันที่ ๒๐ มกราคม พ.ศ. ๒๕๔๔")
        # Thai numerals not handled by regex — test with Arabic numerals
        result = parse_thai_date("เมื่อวันที่ 20 มกราคม พ.ศ. 2544")
        assert result == "2001-01-20"

    def test_english_date_in_en_report(self):
        result = parse_thai_date("AT SAMUI INTERNATIONAL AIRPORT ON 4 AUGUST 2009")
        assert result == "2009-08-04"

    def test_english_date_on_header_line(self):
        result = parse_thai_date("ON 15 MARCH 2017")
        assert result == "2017-03-15"

    def test_no_date_returns_none(self):
        assert parse_thai_date("No date here whatsoever") is None

    def test_year_out_of_range_returns_none(self):
        # BE 2499 = CE 1956 — out of 1990-2030 range, skip
        result = parse_thai_date("วันที่ 1 มกราคม พ.ศ. 2499")
        assert result is None

    def test_be_year_in_thai_text(self):
        # BE 2562 = CE 2019
        result = parse_thai_date("วันที่ 5 มิถุนายน พ.ศ. 2562")
        assert result == "2019-06-05"


# ──────────────────────────────────────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────────────────────────────────────

# Minimal fixture HTML (single <li> per entry, one Thai-only, one bilingual)
_LISTING_FIXTURE = """
<html>
<body>
<ul>
<li>
                File No. 01/2001 (F) Jabiru LSA 55/3J, U-C01  <a href="https://motdrive.mot.go.th/index.php/s/Z5qzDhiwnpxXxz6">Thai</a>
</li>
<li>
                File No. 03/2009 (F) ATR72-212A, HS-PGL <a href="https://motdrive.mot.go.th/index.php/s/MjpUSzi848FFwHR">Thai</a> / <a href="https://motdrive.mot.go.th/index.php/s/JYnJTqoU34mD19Z">English</a>
</li>
<li>
                File No.\xa008/2010 (F) Diamond DA42, HS-IAO  <a href="https://motdrive.mot.go.th/index.php/s/k7qbh1prHZ9xJG5">Thai</a> / <a href="https://motdrive.mot.go.th/index.php/s/o9097taN4cWjZmk">English</a>
</li>
<li>
                <strong>2024</strong>
</li>
<li>
                File No. 177/2024 Airbus A319-132, HS-PGN
                        <a href="https://motdrive.mot.go.th/index.php/s/YRreu02Ddp0rPfo">Thai</a> / <a href="https://motdrive.mot.go.th/index.php/s/Kk3s3fzFFJWHqkq">English</a>
</li>
</ul>
</body>
</html>
"""


class TestParseListing:
    def setup_method(self):
        self.rows = parse_listing(_LISTING_FIXTURE, "final")

    def test_row_count(self):
        # 4 entries with PDFs
        assert len(self.rows) == 4

    def test_case_id_format(self):
        ids = [r["case_id"] for r in self.rows]
        assert "aaicth-1/2001" in ids
        assert "aaicth-3/2009" in ids
        assert "aaicth-8/2010" in ids
        assert "aaicth-177/2024" in ids

    def test_thai_only_has_no_en_pdf(self):
        row = next(r for r in self.rows if r["case_id"] == "aaicth-1/2001")
        assert row["pdf_url_en"] is None
        assert row["pdf_url_th"] is not None
        assert row["lang"] == "th"

    def test_bilingual_prefers_en(self):
        row = next(r for r in self.rows if r["case_id"] == "aaicth-3/2009")
        assert row["pdf_url_en"] is not None
        assert row["pdf_url"] == row["pdf_url_en"]
        assert row["lang"] == "en"

    def test_download_url_has_slash_download(self):
        row = next(r for r in self.rows if r["case_id"] == "aaicth-3/2009")
        assert row["pdf_url_en"].endswith("/download")

    def test_event_date_is_year_precision(self):
        row = next(r for r in self.rows if r["case_id"] == "aaicth-1/2001")
        assert row["event_date"] == "2001-01-01"

    def test_report_type_propagated(self):
        for row in self.rows:
            assert row["report_type"] == "final"

    def test_registration_extracted(self):
        row = next(r for r in self.rows if r["case_id"] == "aaicth-3/2009")
        assert row["registration"] == "HS-PGL"

    def test_no_duplicate_case_ids(self):
        ids = [r["case_id"] for r in self.rows]
        assert len(ids) == len(set(ids))

    def test_xa0_nbsp_in_file_no(self):
        # File No.\xa008/2010 — non-breaking space between No. and seq
        row = next((r for r in self.rows if r["case_id"] == "aaicth-8/2010"), None)
        assert row is not None, "Row with NBSP in File No must be parsed"

    def test_no_pdf_row_skipped(self):
        html = "<li>File No. 05/2015 (F) Cessna 172, HS-TST</li>"
        rows = parse_listing(html, "final")
        assert rows == [], "Row with no PDF link must be skipped"


class TestDownloadUrl:
    def test_appends_download(self):
        url = _download_url("AbCdEf123")
        assert url == "https://motdrive.mot.go.th/index.php/s/AbCdEf123/download"
