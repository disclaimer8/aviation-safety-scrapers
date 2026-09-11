"""The fetch path is plain HTTP now — prove it, and prove it still refuses junk.

This package used to run `ssh HETZNER "curl ... '<url>'"` for the listing and
every PDF. That made a quote in someone else's HTML into command execution on
the fetch host. The string was hardened first; the rewrite removes the shell
instead, so the category is gone rather than guarded.

These tests pin both halves: no subprocess is reachable from discover/fetch,
and a URL the allowlist rejects is still never fetched.
"""
import inspect
import sqlite3

from eaaid_ingest import db, eaaid, pipeline

from conftest import FakeClient, FakeResp

GUID = "0123abcd-4567-89ab-cdef-0123456789ab"
GOOD_HREF = f"/Accident_GenDownloadRes?id={GUID}%5C20240115_003.pdf&name=Report"
GOOD_URL = eaaid.DOWNLOAD_BASE + GOOD_HREF


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    return conn


def test_no_shell_is_reachable_from_discover_or_fetch():
    """The whole point of the rewrite: no subprocess, no ssh, no rsync."""
    src = inspect.getsource(pipeline)
    # strip the module docstring, which explains the history on purpose
    body = src.split('"""', 2)[-1]
    for forbidden in ("subprocess", "ssh", "rsync", "os.system", "popen"):
        assert forbidden not in body, (
            f"{forbidden!r} is back in pipeline.py outside the docstring — "
            "the shell-out was removed deliberately, see the module docstring"
        )


def test_discover_raises_when_the_listing_loses_its_table():
    """A listing that parses to nothing must not look like a site with no reports."""
    conn = _conn()
    client = FakeClient({eaaid.LISTING_URL: FakeResp(text="<html>nope</html>")})
    try:
        pipeline.discover(conn, client)
    except RuntimeError as exc:
        assert "did not return the expected table" in str(exc)
    else:
        raise AssertionError("discover() accepted a listing with no <tbody>")


def _seed_new(conn, case_id, url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO eaaid_reports (case_id, guid, source_url, status, "
        "discovered_at, updated_at) VALUES (?,?,?,?,?,?)",
        (case_id, GUID, url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_a_pdf(tmp_path):
    conn = _conn()
    _seed_new(conn, "C1", GOOD_URL)
    client = FakeClient({GOOD_URL: FakeResp(content=b"%PDF-1.7 body")})
    assert pipeline.fetch(conn, client, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM eaaid_reports").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert open(row["pdf_path"], "rb").read().startswith(b"%PDF")


def test_fetch_refuses_a_url_the_allowlist_rejects(tmp_path):
    """Rows written before the allowlist landed are untrusted input too."""
    conn = _conn()
    hostile = eaaid.DOWNLOAD_BASE + f"/Accident_GenDownloadRes?id={GUID}%5C'; id #"
    _seed_new(conn, "C2", hostile)
    client = FakeClient({hostile: FakeResp(content=b"%PDF-1.7")})
    assert pipeline.fetch(conn, client, str(tmp_path)) == 0
    row = conn.execute("SELECT status, skip_reason FROM eaaid_reports").fetchone()
    assert row["status"] == db.STATUS_SKIPPED
    assert row["skip_reason"] == "bad-source-url"
    assert client.calls == [], "a rejected URL must never be requested"


def test_fetch_rejects_a_response_that_is_not_a_pdf(tmp_path):
    conn = _conn()
    _seed_new(conn, "C3", GOOD_URL)
    client = FakeClient({GOOD_URL: FakeResp(content=b"<html>error page</html>")})
    assert pipeline.fetch(conn, client, str(tmp_path)) == 0
    row = conn.execute("SELECT status, skip_reason FROM eaaid_reports").fetchone()
    assert row["status"] == db.STATUS_SKIPPED
    assert row["skip_reason"] == "not-pdf"
