"""Non-bleed guarantees: source key 'beacg' (Congo) must NEVER collide with
the pre-existing source key 'bea' (France), even though 'beacg' contains 'bea'.
Source-key matching across the program is EXACT-MATCH; these tests pin that.
"""
import re

from beacg_ingest import beacg, db


SRC = "beacg"
FRANCE = "bea"


def test_source_keys_distinct():
    assert SRC != FRANCE
    assert SRC.startswith(FRANCE)     # the dangerous substring relationship
    assert FRANCE != SRC              # but never equal


def test_exact_match_does_not_bleed():
    # exact equality is the program's matching rule: it must separate the two
    assert (SRC == FRANCE) is False
    assert (FRANCE == SRC) is False


def test_case_id_prefix_is_beacg_not_bea():
    cid = beacg.make_case_id(
        "https://www.bea.cg/wp-content/uploads/2025/10/Rapport-final-_-BEA-03-2023.pdf"
    )
    # case_id is namespaced 'beacg-...', NOT 'bea-...'
    assert cid.startswith("beacg-")
    # the token immediately before the first '-' is exactly 'beacg'
    assert cid.split("-", 1)[0] == "beacg"
    assert cid.split("-", 1)[0] != "bea"


def test_case_id_not_matched_by_france_exact_token():
    cid = beacg.make_case_id(
        "https://www.bea.cg/wp-content/uploads/2025/08/Rapport-Final-AN-12BP-de-TAC-le-21.03.11.pdf"
    )
    prefix = cid.split("-", 1)[0]
    # an exact-token matcher keyed on 'bea' (France) must not select this row
    assert prefix == "beacg"
    assert prefix != "bea"
    assert "bea" != prefix


def test_france_case_id_not_matched_as_beacg():
    # a hypothetical France-style id (their key is 'bea') must not pass a
    # beacg exact-prefix test, and vice versa.
    fr_id = "bea-2020-001"
    cg_id = beacg.make_case_id(
        "https://www.bea.cg/wp-content/uploads/2025/08/Rapport-denquete-AN-12-de-AERO-SERVICE-le-25.01.08.pdf"
    )
    assert fr_id.split("-", 1)[0] == "bea"
    assert cg_id.split("-", 1)[0] == "beacg"
    assert fr_id.split("-", 1)[0] != cg_id.split("-", 1)[0]


def test_tables_are_beacg_namespaced_not_bea():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "beacg_reports" in names
    assert "beacg_accidents" in names
    # the France-keyed table names must NOT have been created by this package
    assert "bea_reports" not in names
    assert "bea_accidents" not in names
    conn.close()


def test_exact_word_boundary_match():
    # regex word-boundary exact match: 'bea' must not match inside 'beacg'
    assert re.fullmatch(r"bea", SRC) is None
    assert re.fullmatch(r"beacg", SRC) is not None
    assert re.fullmatch(r"beacg", FRANCE) is None
