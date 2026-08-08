import os
from cins_ingest import cins

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")
_INDEX = open(os.path.join(_FIX, "cins_index.html"), encoding="utf-8").read()


# ── case_id helpers ────────────────────────────────────────────────────────

def test_normalize_case_id_pads():
    assert cins._normalize_case_id("1-23") == "01-23"
    assert cins._normalize_case_id("01-24") == "01-24"
    assert cins._normalize_case_id("01-17s") == "01-17S"
    assert cins._normalize_case_id(None) is None
    assert cins._normalize_case_id("garbage") is None


def test_make_case_id_prefers_anchor_then_filename():
    assert cins.make_case_id("01-24", "01-24 cell", "/x/01-24.pdf") == "01-24"
    # empty anchor -> use cell text
    assert cins.make_case_id("", "01-08", "/x/whatever.pdf") == "01-08"
    # both empty -> filename
    assert cins.make_case_id("", "", "/doc/IZVESTAJ 03-07 X.pdf") == "03-07"


def test_classify_event():
    assert cins._classify_event("Udes") == "Accident"
    assert cins._classify_event("Ozbiljna nezgoda") == "Serious incident"
    assert cins._classify_event("Nezgoda") == "Incident"
    assert cins._classify_event("") is None


def test_date_to_iso():
    assert cins._date_to_iso("18.02.2024") == "2024-02-18"
    assert cins._date_to_iso("21.04.2023") == "2023-04-21"
    assert cins._date_to_iso("26.01.08.") == "2008-01-26"
    assert cins._date_to_iso("-") is None
    assert cins._date_to_iso("") is None


def test_abs_url_resolves_relative():
    assert cins._abs_url("../doc/vazdusni-saobracaj/2024/01-24-2.PDF") == \
        cins.BASE + "/doc/vazdusni-saobracaj/2024/01-24-2.PDF"
    assert cins._abs_url("http://x/y.pdf") == "http://x/y.pdf"


# ── live-fixture listing parse ─────────────────────────────────────────────

def test_parse_listing_count():
    rows = cins.parse_listing(_INDEX)
    # ~126 table rows (excludes ~16 annual summaries)
    assert 110 <= len(rows) <= 135


def test_parse_listing_excludes_annual_summaries():
    rows = cins.parse_listing(_INDEX)
    for r in rows:
        assert "godisnji-izvestaji" not in r["pdf_url"].lower()
        assert "vazdusni-saobracaj" in r["pdf_url"].lower()


def test_parse_listing_all_have_normalized_case_ids():
    rows = cins.parse_listing(_INDEX)
    import re
    for r in rows:
        assert re.match(r"^\d{2}-\d{2}S?$", r["case_id"]), r["case_id"]
    # case_ids unique
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_parse_listing_known_row():
    rows = cins.parse_listing(_INDEX)
    by_id = {r["case_id"]: r for r in rows}
    r = by_id["01-24"]
    assert r["aircraft"] == "Embraer E190-200LR"
    assert r["event_class"] == "Accident"   # "Udes"
    assert r["registration"] == "OY-GDC"
    assert r["date_of_occurrence"] == "2024-02-18"
    assert "Beograd" in r["location"]
    assert r["pdf_url"].endswith("01-24-2.PDF")
    assert r["report_url"] is None


def test_parse_listing_dash_registration_is_none():
    rows = cins.parse_listing(_INDEX)
    by_id = {r["case_id"]: r for r in rows}
    # 01-23 (paraglider) has registration "-" in the listing
    assert by_id["01-23"]["registration"] is None


def test_parse_listing_space_filename_href_present():
    rows = cins.parse_listing(_INDEX)
    spaced = [r for r in rows if " " in r["pdf_url"]]
    # many older reports have spaces in filenames
    assert len(spaced) >= 20


# ── download space-encoding ────────────────────────────────────────────────

class _Client:
    def __init__(self):
        self.requested = None

    def get(self, url, headers=None):
        self.requested = url

        class _R:
            content = b"%PDF-1.4 data"

            def raise_for_status(self_):
                pass
        return _R()


def test_download_url_encodes_spaces(tmp_path):
    c = _Client()
    url = cins.BASE + "/doc/vazdusni-saobracaj/2008/IZVESTAJ 01-08 CESSNA 172G YU-DOT.pdf"
    dest = tmp_path / "01-08.pdf"
    cins.download(c, url, str(dest))
    assert " " not in c.requested
    assert "%20" in c.requested
    assert dest.read_bytes().startswith(b"%PDF")
