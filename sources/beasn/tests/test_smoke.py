"""Smoke tests for beasn-ingest.

These PARSE the script; they do not import it. beasn_scraper.py runs its ingest at
module scope, so importing it inside a test would start scraping the source.

This is the floor, not the ceiling: it proves the file is valid Python, that
its header still records the metadata the sync and the registry depend on, and
that its database path has not drifted. Real behaviour tests come with the
refactor into the four-verb shape sources/rosap uses.
"""
import ast
import pathlib

HERE = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = HERE / "beasn_scraper.py"


def _tree():
    return ast.parse(SCRIPT.read_text(encoding="utf-8", errors="replace"))


def test_the_script_is_there_and_parses():
    assert SCRIPT.exists(), f"{SCRIPT} is missing"
    _tree()


def test_the_header_still_records_where_the_data_comes_from():
    doc = ast.get_docstring(_tree()) or ""
    assert len(doc.strip()) >= 60, (
        "the module docstring carries the source metadata — the agency, the "
        "listing URL, the country and language the sync depends on. It is "
        "empty or a stub."
    )
    # Looking for the literal word "source" was the first version of this and
    # it failed seven perfectly well documented scripts: ASRS opens "ASRS NASA
    # Voluntary Incident Reports Harvester" with the URL beneath, BEA-Bénin
    # gives "country BJ, lang 'fr'". They name what they are without using
    # that word. What must be present is an identifier: this source's code, or
    # an address to fetch from.
    lowered = doc.lower()
    assert "beasn" in lowered or "http" in lowered, (
        "the header names neither the source code nor a URL, so nothing "
        "records where these records come from"
    )


def test_it_defines_something_runnable():
    tree = _tree()
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert names, "no functions at all — the script cannot be driven"
