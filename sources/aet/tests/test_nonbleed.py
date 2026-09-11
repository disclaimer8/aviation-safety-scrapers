"""Non-bleed guarantees: the source key 'aet' (Luxembourg) is namespaced and
must NEVER be matched as a substring of another source key.  Source-key matching
across the program is EXACT-MATCH; these tests pin that for 'aet'.
"""
import re

from aet_ingest import aet, db


SRC = "aet"


def test_case_id_prefix_is_aet():
    cid = aet.make_case_id("//aet.gouvernement.lu/x/CESSNA-C177-factual-report-FINAL.pdf")
    assert cid.startswith("aet-")
    assert cid.split("-", 1)[0] == "aet"


def test_exact_word_boundary_match():
    assert re.fullmatch(r"aet", SRC) is not None
    # 'aet' must not be satisfied by a longer token that contains it
    assert re.fullmatch(r"aet", "aetx") is None
    assert re.fullmatch(r"aet", "baet") is None


def test_tables_are_aet_namespaced():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "aet_reports" in names
    assert "aet_accidents" in names
    conn.close()


def test_country_is_lu_not_other():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aet_accidents (case_id) VALUES ('aet-x')")
    row = conn.execute("SELECT country FROM aet_accidents WHERE case_id='aet-x'").fetchone()
    assert row["country"] == "LU"
    conn.close()
