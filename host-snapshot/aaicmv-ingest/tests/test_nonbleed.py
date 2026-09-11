# tests/test_nonbleed.py
"""Guard against source-key bleed: aaicmv must NOT collide with the sibling
keys aaicnp (Nepal), aaib, aaid, aaiu, or the generic 'aaic'. These keys are
visually adjacent and are EXACT-MATCH identifiers in the 53-source program."""
import re
from pathlib import Path

from aaicmv_ingest import db, aaicmv, text

PKG = Path(__file__).resolve().parent.parent / "aaicmv_ingest"

_SIBLINGS = ["aaicnp", "aaib", "aaid", "aaiu"]


def test_table_names_exact():
    assert "aaicmv_reports" in db.SCHEMA
    assert "aaicmv_accidents" in db.SCHEMA


def test_no_sibling_table_names_in_schema():
    for sib in _SIBLINGS + ["aaic_reports", "aaic_accidents"]:
        assert f"{sib}_reports" not in db.SCHEMA
        assert f"{sib}_accidents" not in db.SCHEMA


def test_case_id_prefix_is_mv_not_np():
    cid = aaicmv.make_case_id("2024/03", "Final report")
    assert cid.startswith("MV-")
    assert "NP" not in cid  # not Nepal (aaicnp)


def test_country_is_mv_not_np():
    assert "DEFAULT 'MV'" in db.SCHEMA
    assert "DEFAULT 'NP'" not in db.SCHEMA  # Nepal sibling
    assert "DEFAULT 'GB'" not in db.SCHEMA  # aaib (UK) sibling


def test_default_slug_is_aaicmv():
    assert text.make_site_slug(None, None, None) == "crash-aaicmv"
    assert text.make_site_slug(None, None, None) != "crash-aaicnp"
    assert text.make_site_slug(None, None, None) != "crash-aaib"


def test_no_sibling_keys_leak_in_source():
    """No source file should reference a sibling key as an identifier token.
    (Substring 'aaic' inside 'aaicmv' is fine; bare sibling tokens are not.)"""
    pattern = re.compile(r"\b(aaicnp|aaib|aaid|aaiu)\b")
    offenders = []
    for py in PKG.glob("*.py"):
        body = py.read_text(encoding="utf-8")
        for m in pattern.finditer(body):
            offenders.append((py.name, m.group(1)))
    assert offenders == [], f"sibling key bleed: {offenders}"


def test_index_url_is_maldives():
    assert "caa.gov.mv" in aaicmv.INDEX_URL
    # not a sibling host
    assert "aaib.gov" not in aaicmv.INDEX_URL
    assert "caa.gov.np" not in aaicmv.INDEX_URL  # Nepal
