"""Smoke tests for beasn-ingest.

These IMPORT the script and exercise what it exposes. The previous version
only ran `ast.parse` over the file and asserted nothing about behaviour, on
the stated grounds that "beasn_scraper.py runs its ingest at module scope, so
importing it inside a test would start scraping the source". That was not true
of this file, and it was not true of any of the other 42 either: every one
guards its entry point with `if __name__ == "__main__"`. All 43 were confirmed
to import cleanly, with no network access and no files created.

This is still the floor, not the ceiling — an import proves the module loads,
its constants evaluate and its entry point exists, which is strictly more than
a parse proved. Real pipeline tests come with the refactor into the four-verb
shape sources/rosap uses.
"""
import ast
import inspect
import pathlib

import beasn_scraper

HERE = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = HERE / "beasn_scraper.py"


def test_the_module_imports_without_touching_the_network():
    # pytest-socket is active in CI (--disable-socket), so an import that
    # opened a connection would fail here rather than on the ingest host.
    assert beasn_scraper.__file__


def test_the_header_still_records_where_the_data_comes_from():
    doc = (beasn_scraper.__doc__ or "").strip()
    assert len(doc) >= 60, (
        "the module docstring carries the source metadata — the agency, the "
        "listing URL, the country and language the registry depends on. It is "
        "empty or a stub."
    )
    lowered = doc.lower()
    assert "beasn" in lowered or "http" in lowered, (
        "the header names neither the source code nor a URL, so nothing "
        "records where these records come from"
    )


def test_it_exposes_a_callable_entry_point():
    functions = [name for name, obj in vars(beasn_scraper).items()
                 if inspect.isfunction(obj) and obj.__module__ == beasn_scraper.__name__]
    assert functions, "no functions at all — the script cannot be driven"


def test_the_entry_point_is_guarded():
    # This is what makes the module importable, and therefore testable. If it
    # regresses, importing this module would run a live scrape.
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8", errors="replace"))
    guarded = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in tree.body
    )
    assert guarded, (
        "the entry point is no longer behind `if __name__ == \"__main__\"`, so "
        "importing this module now runs a live scrape"
    )
