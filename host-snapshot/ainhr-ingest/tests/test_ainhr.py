# tests/test_ainhr.py
"""Offline tests for ainhr_ingest.ainhr using saved live HTML fixtures."""
import os
import re

import pytest

from ainhr_ingest import ainhr

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── iter_post_slugs ───────────────────────────────────────────────────────────

def test_iter_post_slugs_count():
    slugs = ainhr.iter_post_slugs(_fixture("ainhr_category.html"))
    assert isinstance(slugs, list)
    assert 55 <= len(slugs) <= 75, f"expected ~63 slugs, got {len(slugs)}"


def test_iter_post_slugs_no_duplicates():
    slugs = ainhr.iter_post_slugs(_fixture("ainhr_category.html"))
    assert len(slugs) == len(set(slugs))


def test_iter_post_slugs_excludes_bare_index():
    slugs = ainhr.iter_post_slugs(_fixture("ainhr_category.html"))
    assert "" not in slugs
    assert all("/" not in s for s in slugs)


def test_iter_post_slugs_known_member():
    slugs = ainhr.iter_post_slugs(_fixture("ainhr_category.html"))
    assert "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022" in slugs


# ── case_id normalisation ──────────────────────────────────────────────────────

def test_make_case_id_is_slug():
    s = "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022"
    assert ainhr.make_case_id(s) == s


def test_normalize_case_id_idempotent():
    s = "apollo-fox-daruvar-01-08-2015"
    assert ainhr._normalize_case_id(s) == ainhr._normalize_case_id(ainhr._normalize_case_id(s))


def test_normalize_case_id_collapses_junk():
    assert ainhr._normalize_case_id("Foo--Bar__Baz!! ") == "foo-bar-baz"


# ── date_from_slug ─────────────────────────────────────────────────────────────

def test_date_from_slug_numeric():
    assert ainhr.date_from_slug("apollo-fox-daruvar-01-08-2015") == "2015-08-01"
    assert ainhr.date_from_slug(
        "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022"
    ) == "2022-05-29"


def test_date_from_slug_croatian_month():
    assert ainhr.date_from_slug(
        "nesreca-ovjesne-jedrilice-grobnicko-polje-01-lipnja-2025"
    ) == "2025-06-01"
    assert ainhr.date_from_slug(
        "nesreca-zrakoplova-reg-oznake-ha-tub-kerestinec-25-svibnja-2024"
    ) == "2024-05-25"


def test_date_from_slug_none_when_absent():
    assert ainhr.date_from_slug("padobran-sinj") is None


# ── event_class_from_slug ──────────────────────────────────────────────────────

def test_event_class_serious_incident():
    assert ainhr.event_class_from_slug(
        "ozbiljna-nezgoda-zrakoplova-airbus-220-300-reg-oznake-9a-can-zracna-luka-split-16-svibnja-2026"
    ) == "Serious incident"


def test_event_class_accident_default():
    assert ainhr.event_class_from_slug(
        "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022"
    ) == "Accident"
    assert ainhr.event_class_from_slug("pad-zrakoplova-lake-la-4-200-resnik-25-06-2015") == "Accident"


# ── parse_post ─────────────────────────────────────────────────────────────────

def test_parse_post_cessna182_prefers_hr_final():
    slug = "nesreca-zrakoplova-tipa-cessna-182-brocanac-slunj-29-05-2022"
    row = ainhr.parse_post(_fixture("ainhr_post_cessna182.html"), slug)
    assert row["case_id"] == slug
    assert row["lang"] == "hr"
    assert row["pdf_url"].endswith("zavrsno_izvjesce.pdf")
    assert row["pdf_url_en"] is not None and "report" in row["pdf_url_en"]
    assert row["report_url"] == "https://ain.hr/istrage/" + slug + "/"
    assert row["date_of_occurrence"] == "2022-05-29"
    assert row["event_class"] == "Accident"
    assert "CESSNA 182" in row["title"]


def test_parse_post_multi_pdf_picks_final():
    slug = "ozljedivanje-vratima-putnice-prilikom-izlaska-iz-putnickog-vagona-krizevci-04-04-2017"
    row = ainhr.parse_post(_fixture("ainhr_post_multi_pdf.html"), slug)
    assert row["pdf_url"].endswith("zavrsno_izvjesce.pdf")
    assert row["lang"] == "hr"


def test_parse_post_no_pdf():
    slug = "ozbiljna-nezgoda-zrakoplova-airbus-220-300-reg-oznake-9a-can-zracna-luka-split-16-svibnja-2026"
    row = ainhr.parse_post(_fixture("ainhr_post_nopdf.html"), slug)
    assert row["pdf_url"] is None
    assert row["lang"] is None
    assert row["event_class"] == "Serious incident"
    assert row["date_of_occurrence"] == "2026-05-16"


# ── registration_from_text ─────────────────────────────────────────────────────

def test_registration_from_text_croatian():
    assert ainhr.registration_from_text("ZAVRSNO IZVJESCE Cessna 150, 9A-DMM Lucko") == "9A-DMM"


def test_registration_from_text_foreign():
    assert ainhr.registration_from_text("Cessna 182, D-EGLF Slunj 2022") == "D-EGLF"


def test_registration_from_text_none():
    assert ainhr.registration_from_text("no marks here lowercase only") is None
