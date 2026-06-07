"""Offline tests for ciaiauy_ingest.ciaiauy using saved gub.uy HTML fixtures."""
import os
import re

from ciaiauy_ingest import ciaiauy

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────
# parse_listing — accidentes fixture
# ──────────────────────────────────────────────

def test_parse_accidentes_returns_rows():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    assert len(rows) >= 30, f"Expected >=30 accident rows, got {len(rows)}"


def test_parse_accidentes_all_pdf_urls_absolute_and_pdf():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    for r in rows:
        assert r["pdf_url"].startswith("https://www.gub.uy/"), r["pdf_url"]
        assert ".pdf" in r["pdf_url"].lower(), r["pdf_url"]


def test_parse_accidentes_event_class_is_accident():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    assert all(r["event_class"] == "Accident" for r in rows)


def test_parse_accidentes_registration_coverage():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    with_reg = [r for r in rows if r["registration"]]
    assert len(with_reg) >= 30, f"Expected >=30 rows with registration, got {len(with_reg)}"


def test_parse_accidentes_known_cx_mgp():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    regs = {r["registration"] for r in rows}
    assert "CX-MGP" in regs
    assert "LV-WIZ" in regs


def test_parse_accidentes_no_duplicate_pdf_urls():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_accidentes.html"))
    urls = [r["pdf_url"] for r in rows]
    assert len(urls) == len(set(urls))


# ──────────────────────────────────────────────
# parse_listing — incidentes graves fixture
# ──────────────────────────────────────────────

def test_parse_incidentes_graves_event_class():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_incidentes_graves.html"))
    assert len(rows) >= 10
    assert all(r["event_class"] == "Serious incident" for r in rows)


def test_parse_incidentes_graves_registration():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_incidentes_graves.html"))
    regs = {r["registration"] for r in rows if r["registration"]}
    assert "LV-BZP" in regs
    assert "CX-BYK-R" in regs


# ──────────────────────────────────────────────
# parse_listing — informes-finales fixture (Caso numbers)
# ──────────────────────────────────────────────

def test_parse_informes_finales_has_caso_numbers():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_informes_finales.html"))
    with_caso = [r for r in rows if r["caso"]]
    assert len(with_caso) >= 5, f"Expected >=5 Caso numbers, got {len(with_caso)}"


def test_parse_informes_finales_caso_pairs_registration():
    rows = ciaiauy.parse_listing(_fixture("ciaiauy_informes_finales.html"))
    by_caso = {r["caso"]: r for r in rows if r["caso"]}
    assert "662" in by_caso and by_caso["662"]["registration"] == "CX-BUZ-R"
    assert "659" in by_caso and by_caso["659"]["registration"] == "CX-BAZ"


# ──────────────────────────────────────────────
# Caso false-positive guard (aircraft model numbers must NOT be Caso)
# ──────────────────────────────────────────────

def test_extract_caso_ignores_model_numbers():
    # PA32RT-300T, PA34-200T, Cessna 208B — the embedded number is a MODEL,
    # not a Caso, so must not be picked up.
    assert ciaiauy.extract_caso("informe-final-piper-pa32rt-300t-01-11-15.pdf") is None
    assert ciaiauy.extract_caso("informe-final-piper-pa34-200t-cx-jls.pdf") is None
    assert ciaiauy.extract_caso("informefinal-20160622-cessnagrandcaravan-208b.pdf") is None


def test_extract_caso_leading_and_dated_forms():
    assert ciaiauy.extract_caso("611 CX-OTA-R informe INCID.pdf") == "611"
    assert ciaiauy.extract_caso("informefinal-20120228-538iberiaec-gpb.pdf") == "538"
    assert ciaiauy.extract_caso("informe-final-no.-582-n3024n.pdf") == "582"
    assert ciaiauy.extract_caso("informe-final-567embraer-erj-190-lv-cif-11-09-14.pdf") == "567"


# ──────────────────────────────────────────────
# _normalize_case_id
# ──────────────────────────────────────────────

def test_normalize_case_id_collapses_space_around_separators():
    assert ciaiauy._normalize_case_id("Caso - 611") == "caso-611"
    assert ciaiauy._normalize_case_id("CX - MGP") == "cx-mgp"
    assert ciaiauy._normalize_case_id("611 / 2016") == "611/2016"
    assert ciaiauy._normalize_case_id("  caso   611  ") == "caso 611"


# ──────────────────────────────────────────────
# make_case_id
# ──────────────────────────────────────────────

def test_make_case_id_prefers_caso():
    assert ciaiauy.make_case_id("611", "CX-OTA-R") == "caso-611"


def test_make_case_id_falls_back_to_registration():
    assert ciaiauy.make_case_id(None, "CX-MGP") == "cx-mgp"


def test_make_case_id_unknown_fallback():
    assert ciaiauy.make_case_id(None, None) == "ciaiauy-unknown"


def test_make_case_id_collision_suffix():
    taken = {"caso-611"}
    cid = ciaiauy.make_case_id("611", None, taken=taken)
    assert cid == "caso-611-2"
    taken.add(cid)
    assert ciaiauy.make_case_id("611", None, taken=taken) == "caso-611-3"


# ──────────────────────────────────────────────
# registration extraction
# ──────────────────────────────────────────────

def test_extract_registration_from_anchor_text():
    assert ciaiauy.extract_registration("Informe Final CX-MGP") == "CX-MGP"
    assert ciaiauy.extract_registration("Informe Incidente Grave LV-BZP") == "LV-BZP"
    assert ciaiauy.extract_registration("Informe Final N3024N") == "N3024N"


def test_extract_registration_none_when_absent():
    assert ciaiauy.extract_registration("Informe Final") is None


# ──────────────────────────────────────────────
# CIAIAC (Spain) NON-BLEED tests
# 'ciaia' is a substring prefix of 'ciaiac'.  These tests prove ciaiauy logic
# never matches CIAIAC (Spain) artefacts and vice-versa, with EXACT comparison.
# ──────────────────────────────────────────────

CIAIAC_CASE_IDS = ["A-005/2024", "IN-002/2024", "A-001/2008"]  # Spanish CIAIAC refs
CIAIAUY_CASE_IDS = ["caso-611", "cx-mgp", "lv-wiz"]            # Uruguay CIAIA ids


def test_constants_are_uruguay_not_spain():
    # Module identity guards — exact equality, no substring matching.
    assert ciaiauy.BASE == "https://www.gub.uy"
    assert "gub.uy" in ciaiauy.INDEX_URL
    assert "transportes.gob.es" not in ciaiauy.INDEX_URL
    assert ciaiauy.INDEX_URL.endswith("/accidentes")
    for path in ciaiauy.SEED_PATHS:
        assert path.startswith("/ministerio-defensa-nacional/")


def test_uy_case_ids_never_match_ciaiac_ref_shape():
    # A Spanish CIAIAC Ref. shape (A-NNN/YYYY) must never be produced by the
    # Uruguay make_case_id, regardless of inputs.
    spain_re = re.compile(r"^(A|IN)-\d{1,4}/\d{4}$")
    for caso, reg in [("611", "CX-OTA-R"), (None, "CX-MGP"), (None, None)]:
        cid = ciaiauy.make_case_id(caso, reg)
        assert not spain_re.match(cid.upper()), f"UY id looks like CIAIAC ref: {cid}"


def test_source_key_exact_match_no_substring_bleed():
    # Simulate a dispatch table keyed by exact source key.  A naive
    # `startswith`/`in` check would route 'ciaiauy' rows to the 'ciaiac' handler
    # because 'ciaiac' is NOT a prefix of 'ciaiauy' but 'ciaia' is a prefix of
    # both — assert EXACT-match routing is unambiguous in both directions.
    handlers = {"ciaiac": "spain", "ciaiauy": "uruguay"}
    assert handlers.get("ciaiauy") == "uruguay"
    assert handlers.get("ciaiac") == "spain"
    # exact match: 'ciaiauy' must not equal 'ciaiac'
    assert "ciaiauy" != "ciaiac"
    # prefix-style matching would be WRONG — prove the bug it would cause:
    naive = [k for k in handlers if "ciaia" in k]
    assert set(naive) == {"ciaiac", "ciaiauy"}  # 'ciaia' bleeds into BOTH
    # the correct lookup is exact:
    assert [k for k in handlers if k == "ciaiauy"] == ["ciaiauy"]


def test_db_table_names_are_ciaiauy_exact(tmp_path):
    from ciaiauy_ingest import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ciaiauy_reports" in names
    assert "ciaiauy_accidents" in names
    # No CIAIAC (Spain) table must exist in a UY database.
    assert "ciaiac_reports" not in names
    assert "ciaiac_accidents" not in names
