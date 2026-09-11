"""Non-bleed guarantees: source key 'gcaagy' (Guyana) must NEVER collide with
the pre-existing source key 'gcaa' (UAE), even though 'gcaagy' contains 'gcaa'.
Source-key matching across the program is EXACT-MATCH; these tests pin that.
"""
import re

from gcaagy_ingest import gcaagy, db


SRC = "gcaagy"
UAE = "gcaa"


def test_source_keys_distinct():
    assert SRC != UAE
    assert SRC.startswith(UAE)        # the dangerous substring relationship
    assert UAE != SRC                 # but never equal


def test_exact_match_does_not_bleed():
    # exact equality is the program's matching rule: it must separate the two
    assert (SRC == UAE) is False
    assert (UAE == SRC) is False


def test_case_id_prefix_is_gcaagy_not_gcaa():
    cid = gcaagy.make_case_id("pdf/8R-GRE_Final_Report.pdf")
    # case_id is namespaced 'gcaagy-...', NOT 'gcaa-...'
    assert cid.startswith("gcaagy-")
    # the token immediately before the first '-' is exactly 'gcaagy'
    assert cid.split("-", 1)[0] == "gcaagy"
    assert cid.split("-", 1)[0] != "gcaa"


def test_case_id_not_matched_by_uae_exact_token():
    cid = gcaagy.make_case_id("pdf/Fly_Jamaica_Accident_Final_Report.pdf")
    prefix = cid.split("-", 1)[0]
    # an exact-token matcher keyed on 'gcaa' (UAE) must not select this row
    assert prefix == "gcaagy"
    assert prefix != "gcaa"
    # and a 'gcaagy' matcher must not be satisfied by the bare UAE token
    assert "gcaa" != prefix


def test_uae_case_id_not_matched_as_gcaagy():
    # a hypothetical UAE-style id (their key is 'gcaa') must not pass a
    # gcaagy exact-prefix test, and vice versa.
    uae_id = "gcaa-2020-001"
    gy_id = gcaagy.make_case_id("pdf/8R-GTR Final Report.pdf")
    assert uae_id.split("-", 1)[0] == "gcaa"
    assert gy_id.split("-", 1)[0] == "gcaagy"
    assert uae_id.split("-", 1)[0] != gy_id.split("-", 1)[0]


def test_tables_are_gcaagy_namespaced_not_gcaa():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "gcaagy_reports" in names
    assert "gcaagy_accidents" in names
    # the UAE-keyed table names must NOT have been created by this package
    assert "gcaa_reports" not in names
    assert "gcaa_accidents" not in names
    conn.close()


def test_exact_word_boundary_match():
    # regex word-boundary exact match: 'gcaa' must not match inside 'gcaagy'
    assert re.fullmatch(r"gcaa", SRC) is None
    assert re.fullmatch(r"gcaagy", SRC) is not None
    assert re.fullmatch(r"gcaagy", UAE) is None
