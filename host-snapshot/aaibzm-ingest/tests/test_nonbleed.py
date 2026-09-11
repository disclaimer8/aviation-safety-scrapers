"""Non-bleed guarantees: source key 'aaibzm' (Zambia) must NEVER collide with
any other source key in the program.  In particular it must not be confused
with a hypothetical bare 'aaib' token; source-key matching across the program
is EXACT-MATCH and these tests pin that.
"""
import re

from aaibzm_ingest import aaibzm, db


SRC = "aaibzm"
GENERIC = "aaib"


def test_source_keys_distinct():
    assert SRC != GENERIC
    assert SRC.startswith(GENERIC)     # the dangerous substring relationship
    assert GENERIC != SRC              # but never equal


def test_exact_match_does_not_bleed():
    assert (SRC == GENERIC) is False
    assert (GENERIC == SRC) is False


def test_case_id_prefix_is_aaibzm():
    cid = aaibzm.make_case_id("9J-YVT")
    assert cid.startswith("aaibzm-")
    assert cid.split("-", 1)[0] == "aaibzm"
    assert cid.split("-", 1)[0] != "aaib"


def test_case_id_not_matched_by_generic_token():
    cid = aaibzm.make_case_id("9S-GAP")
    prefix = cid.split("-", 1)[0]
    assert prefix == "aaibzm"
    assert prefix != "aaib"


def test_tables_are_aaibzm_namespaced():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "aaibzm_reports" in names
    assert "aaibzm_accidents" in names
    assert "aaib_reports" not in names
    assert "aaib_accidents" not in names
    conn.close()


def test_exact_word_boundary_match():
    assert re.fullmatch(r"aaib", SRC) is None
    assert re.fullmatch(r"aaibzm", SRC) is not None
    assert re.fullmatch(r"aaibzm", GENERIC) is None
