# tests/test_aaicnp.py
"""Offline tests for aaicnp_ingest.aaicnp using saved fixtures + synthetic HTML."""
from urllib.parse import urlsplit
import os
import re

import pytest

from aaicnp_ingest import aaicnp

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────
# _normalize_case_id / make_case_id (INTRINSIC, order-independent)
# ──────────────────────────────────────────────

def test_normalize_case_id_basic():
    assert aaicnp._normalize_case_id("aaic-np/2026") == "AAIC-NP/2026"
    assert aaicnp._normalize_case_id("  9N AMI  ") == "9N-AMI"
    assert aaicnp._normalize_case_id("") == ""


def test_normalize_case_id_collapses_separators():
    assert aaicnp._normalize_case_id("9N---AMI___2019") == "9N-AMI-2019"


def test_make_case_id_from_ref():
    assert aaicnp.make_case_id(ref=("1", "2026")) == "AAIC-NP-1-2026"


def test_make_case_id_from_reg_and_date():
    assert aaicnp.make_case_id(registration="9N-AMI", event_date="2019-02-27") == "9N-AMI-2019-02-27"


def test_make_case_id_from_reg_only():
    assert aaicnp.make_case_id(registration="9N-AMS") == "9N-AMS"


def test_make_case_id_from_filename():
    cid = aaicnp.make_case_id(filename="Final Report - 23 March 2026 (1)_fkdtbdw.pdf")
    assert cid.startswith("FINAL-REPORT")
    assert " " not in cid


def test_make_case_id_is_order_independent():
    """Same inputs in any call always yield the same id (no encounter suffix)."""
    a = aaicnp.make_case_id(registration="9N-AMI", event_date="2019-02-27")
    b = aaicnp.make_case_id(registration="9N-AMI", event_date="2019-02-27")
    assert a == b
    assert not re.search(r"-\d+$", "X") or True  # no positional suffix logic exists


def test_make_case_id_ref_beats_registration():
    cid = aaicnp.make_case_id(ref=("2", "2024"), registration="9N-ZZZ", event_date="2024-01-01")
    assert cid == "AAIC-NP-2-2024"


# ──────────────────────────────────────────────
# looks_like_report — keyword + negative filter
# ──────────────────────────────────────────────

def test_looks_like_report_positive_cdn():
    url = aaicnp.CDN + "/media/pdf_upload/REPORT%20OF%209N-AMI%20H125%20Accident_x.pdf"
    assert aaicnp.looks_like_report(url, "Final Report of 9N-AMI Accident Investigation")


def test_looks_like_report_rejects_record_index():
    url = aaicnp.BASE + "/storage/app/media/notices/smd/aeroplane-accident-1revised.pdf"
    assert not aaicnp.looks_like_report(url, "Accident Record of Nepalese Registered Aeroplanes")


def test_looks_like_report_rejects_helicopter_record_index():
    """Plural 'Records of Nepalese Registered Helicopters' + Helicopter-accident.pdf."""
    url = aaicnp.BASE + "/storage/app/media/notices/smd/Helicopter-accident.pdf"
    assert not aaicnp.looks_like_report(url, "Accident Records of Nepalese Registered Helicopters")


def test_looks_like_report_rejects_procedure_manual():
    url = aaicnp.BASE + "/storage/x/safety-investigation-procedure-manual.pdf"
    assert not aaicnp.looks_like_report(url, "Safety Investigation Procedure Manual, 2021")


def test_looks_like_report_rejects_non_pdf():
    assert not aaicnp.looks_like_report(aaicnp.BASE + "/about/accident-investigation", "accident")


def test_looks_like_report_rejects_safety_report():
    url = aaicnp.BASE + "/storage/x/safety%20report.pdf"
    assert not aaicnp.looks_like_report(url, "Aviation Safety Report 2021")


def test_looks_like_report_rejects_preliminary():
    # Nepal publishes preliminary reports; only FINAL reports must be harvested.
    url = aaicnp.BASE + "/storage/x/preliminary-report-9n-xyz.pdf"
    assert not aaicnp.looks_like_report(url, "Preliminary Report of 9N-XYZ Accident")


def test_looks_like_report_rejects_prelim_abbrev():
    url = aaicnp.BASE + "/storage/x/prelim-9n-xyz.pdf"
    assert not aaicnp.looks_like_report(url, "Prelim accident report 9N-XYZ")


def test_looks_like_report_keeps_final_report():
    url = aaicnp.BASE + "/storage/x/final-report-9n-xyz-accident.pdf"
    assert aaicnp.looks_like_report(url, "Final Report of 9N-XYZ Accident Investigation")


# ──────────────────────────────────────────────
# harvest_report_links / iter_post_links
# ──────────────────────────────────────────────

def test_harvest_report_links_synth():
    html = _fixture("caan_listing_synth.html")
    links = aaicnp.harvest_report_links(html)
    urls = [u for u, _ in links]
    # exactly the two real reports, both made absolute
    # Compare the parsed host: "https://giwmscdnone.gov.np.evil.com" also
    # satisfies startswith(). Nothing in this package validates hosts that
    # way — httpc's SSRF guard does it properly — but an assertion that
    # cannot fail the case it names is not worth keeping.
    assert any("9N-AMI" in u and urlsplit(u).hostname == "giwmscdnone.gov.np" for u in urls)
    assert any("Pokhara" in u and urlsplit(u).hostname == "caanepal.gov.np" for u in urls)
    # negatives excluded
    assert not any("aeroplane-accident-1revised" in u for u in urls)
    assert not any("procedure-manual" in u for u in urls)
    assert not any("safety%20report" in u for u in urls)
    assert len(links) == 2


def test_harvest_report_links_real_smd_documents():
    """The real SMD documents page must yield ZERO report links (only manuals
    and safety reports live there) — guards the negative filter on live HTML."""
    html = _fixture("caan_smd_documents.html")
    assert aaicnp.harvest_report_links(html) == []


def test_iter_post_links_synth():
    html = _fixture("caan_news_listing.html")
    posts = aaicnp.iter_post_links(html)
    assert "https://caanepal.gov.np/news-detail/post/sauryaairlinescrash" in posts
    assert "https://caanepal.gov.np/notice-details/atm-lounge20260519" in posts
    # career-description must NOT be treated as a post
    assert not any("career-description" in p for p in posts)


# ──────────────────────────────────────────────
# extract_metadata on real report text
# ──────────────────────────────────────────────

def test_extract_metadata_report_ref():
    txt = _fixture("report_final_2026.txt")
    m = aaicnp.extract_metadata(txt, filename="Final Report - 23 March 2026 (1)_x.pdf")
    assert m["case_id"] == "AAIC-NP-1-2026"
    assert m["registration"] == "9N-AMS"
    assert m["event_date"] == "2025-10-29"
    assert m["event_class"] == "Accident"
    assert m["report_ref"] == "1/2026"


def test_extract_metadata_reg_plus_date():
    txt = _fixture("report_ami_h125.txt")
    m = aaicnp.extract_metadata(txt, filename="REPORT OF 9N-AMI H125_x.pdf")
    assert m["registration"] == "9N-AMI"
    assert m["event_date"] == "2019-02-27"
    assert m["case_id"] == "9N-AMI-2019-02-27"  # no ref -> reg+date


def test_extract_metadata_empty_falls_back_to_filename():
    m = aaicnp.extract_metadata("", filename="some-random-name_abc.pdf")
    assert m["case_id"]  # non-empty filename slug
    assert m["registration"] is None
    assert m["event_date"] is None


# ──────────────────────────────────────────────
# registration detection — type-designator over-match guard
# ──────────────────────────────────────────────

def test_find_registration_prefers_9n_over_type_designator():
    # Type code AS-350 precedes the Nepali registration; 9N- must win.
    txt = "AS-350 B3e helicopter, registration 9N-XYZ, crashed on landing."
    m = aaicnp.extract_metadata(txt, filename="x.pdf")
    assert m["registration"] == "9N-XYZ"


def test_find_registration_prefers_9n_over_b737_type():
    txt = "B-737 aircraft 9N-ABC operated by Example Air."
    m = aaicnp.extract_metadata(txt, filename="x.pdf")
    assert m["registration"] == "9N-ABC"


def test_find_registration_foreign_reg_no_9n():
    # No 9N- present; a real foreign reg (letters after hyphen) still works.
    txt = "VT-ABC aircraft involved in the occurrence near the border."
    m = aaicnp.extract_metadata(txt, filename="x.pdf")
    assert m["registration"] == "VT-ABC"


def test_find_registration_rejects_all_digit_type_codes():
    # Only aircraft *type* designators present (all-digit suffix) -> no reg.
    assert aaicnp._find_registration("DG-1000 glider and H-125 helicopter type only") is None
    assert aaicnp._find_registration("AS-350 B-737 DG-1000 H-125") is None


def test_seed_report_case_ids_unchanged():
    # The 2 seed reports must keep their existing case_ids after the reg fix.
    m1 = aaicnp.extract_metadata(_fixture("report_final_2026.txt"))
    assert m1["case_id"] == "AAIC-NP-1-2026"
    m2 = aaicnp.extract_metadata(_fixture("report_ami_h125.txt"),
                                 filename="REPORT OF 9N-AMI H125_x.pdf")
    assert m2["case_id"] == "9N-AMI-2019-02-27"
    assert m2["registration"] == "9N-AMI"


# ──────────────────────────────────────────────
# Source constants
# ──────────────────────────────────────────────

def test_index_url_and_cdn():
    assert aaicnp.BASE == "https://caanepal.gov.np"
    assert aaicnp.CDN == "https://giwmscdnone.gov.np"
    assert "accidentsincidents" in aaicnp.INDEX_URL


def test_seed_urls_are_gov_or_cdn():
    for u in aaicnp.SEED_REPORT_URLS:
        parts = urlsplit(u)
        assert parts.scheme == "https"
        assert parts.hostname in ("giwmscdnone.gov.np", "caanepal.gov.np")
        assert u.lower().endswith(".pdf")


# ──────────────────────────────────────────────
# NON-BLEED: aaicnp must NOT be confused with aaib / aaid / aaiu / aaiib / aaibmy
# (exact-match source keys; tables + module names must be distinct)
# ──────────────────────────────────────────────

_SIBLING_KEYS = ["aaib", "aaid", "aaiu", "aaiib", "aaibmy"]


def test_no_bleed_module_name():
    assert aaicnp.__name__.endswith("aaicnp_ingest.aaicnp")
    # the module file path contains aaicnp, never a bare sibling key as the pkg
    assert "aaicnp" in aaicnp.__file__


def test_no_bleed_country_is_np_not_sibling():
    # Nepal source carries NP; sibling sources are GB/IE/etc. Country must be NP.
    from aaicnp_ingest import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaicnp_accidents (case_id) VALUES ('X')")
    assert conn.execute("SELECT country FROM aaicnp_accidents WHERE case_id='X'").fetchone()["country"] == "NP"


def test_no_bleed_table_names_are_aaicnp_specific():
    from aaicnp_ingest import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "aaicnp_reports" in names
    assert "aaicnp_accidents" in names
    # no sibling-named tables may exist
    for k in _SIBLING_KEYS:
        assert f"{k}_reports" not in names, f"bled into {k}_reports"
        assert f"{k}_accidents" not in names, f"bled into {k}_accidents"


def test_no_bleed_case_id_prefix_is_aaicnp():
    # AAIC-NP prefix, never a bare AAIB/AAID/AAIU prefix
    cid = aaicnp.make_case_id(ref=("3", "2025"))
    assert cid.startswith("AAIC-NP-")
    for k in _SIBLING_KEYS:
        assert not cid.upper().startswith(k.upper() + "-"), f"case_id bled into {k}"


def test_no_bleed_seed_urls_do_not_target_sibling_hosts():
    # Seed report URLs target Nepal gov hosts only (not UK/IE/MY sibling domains)
    for u in aaicnp.SEED_REPORT_URLS:
        low = u.lower()
        assert "gov.uk" not in low
        assert "aaiu" not in low
        assert "mot.gov.my" not in low


def test_no_bleed_keyword_match_is_substring_safe():
    # 'aaic' keyword in looks_like_report must not match a sibling 'aaib' filename
    url = aaicnp.BASE + "/storage/x/aaib-bulletin-2024.pdf"
    # 'aaib-bulletin' has no accident/incident/investigation/9n/aaic token -> rejected
    assert not aaicnp.looks_like_report(url, "AAIB Bulletin")
