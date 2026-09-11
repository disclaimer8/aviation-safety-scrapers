"""Offline tests for aacsv_ingest.aacsv using the saved live listing fixture."""
import os
import re

import pytest

from aacsv_ingest import aacsv

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def rows():
    return aacsv.parse_listing(_fixture("aacsv_informes.html"))


# ── parse_listing ─────────────────────────────────────────────────────────────

def test_parse_listing_row_count(rows):
    # live listing carries 45 report packages (finals + prelims + initials)
    assert len(rows) == 45, f"expected 45 rows, got {len(rows)}"


def test_every_row_has_slug_and_pdf_url(rows):
    for r in rows:
        assert r["slug"]
        assert r["pdf_url"].startswith("https://www.aac.gob.sv/download/")
        assert "wpdmdl=" in r["pdf_url"]


def test_refresh_param_dropped(rows):
    for r in rows:
        assert "refresh=" not in r["pdf_url"], r["pdf_url"]


def test_slugs_unique(rows):
    slugs = [r["slug"] for r in rows]
    assert len(slugs) == len(set(slugs))


def test_known_final_8_present_and_classified(rows):
    r = next(r for r in rows if r["slug"] == "informe-final-8")
    assert r["report_type"] == "final"
    assert r["registration"] == "YS-05P"
    assert r["pdf_url"].endswith("wpdmdl=12733")


def test_report_types_present(rows):
    kinds = {r["report_type"] for r in rows}
    assert "final" in kinds
    assert "preliminary" in kinds


def test_registration_extraction_samples(rows):
    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["informe-final-10"]["registration"] == "N9417T"
    assert by_slug["informe-final-9"]["registration"] == "YS-446P"
    assert by_slug["declaracion-provisional-5"]["registration"] == "YS-331PE"


def test_case_id_shape(rows):
    # case_id must be AAC-AIG-... and idempotent under normalisation
    pat = re.compile(r"^AAC-AIG-")
    for r in rows:
        assert pat.match(r["case_id"]), r["case_id"]
        assert aacsv._normalize_case_id(r["case_id"]) == r["case_id"]


def test_dates_parsed_for_dated_rows(rows):
    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["informe-final-10"]["date_of_occurrence"] == "2025-10-25"


# ── unit: normalisers ─────────────────────────────────────────────────────────

def test_normalize_reg():
    assert aacsv._normalize_reg("YS 331 PE") == "YS-331PE"
    assert aacsv._normalize_reg("ys-289p") == "YS-289P"
    assert aacsv._normalize_reg("N 95207") == "N95207"
    assert aacsv._normalize_reg("OM H747") == "OM-H747"


def test_reg_key_collapses_suffix():
    assert aacsv._reg_key("YS-289P") == aacsv._reg_key("YS-289PE")
    assert aacsv._reg_key("YS 331 PE") == aacsv._reg_key("YS-331PE")


def test_make_case_id_with_aacref():
    cid = aacsv.make_case_id(("ACCID", "5", "22"), "YS-243P", 2022, "final")
    assert cid == "AAC-AIG-005-YS-243P-2022"


def test_make_case_id_fallback():
    cid = aacsv.make_case_id(None, "YS-164", 2024, "final")
    assert cid == "AAC-AIG-YS-164-2024"


def test_normalize_case_id_idempotent():
    raw = "aac--aig  005 ys-243p"
    once = aacsv._normalize_case_id(raw)
    assert aacsv._normalize_case_id(once) == once
