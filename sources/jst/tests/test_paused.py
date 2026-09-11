"""jst discover is paused, and that must be loud and explained.

It enumerated events from intranet.jst.gob.ar, whose robots.txt is a blanket
`Disallow: /` — a host that calls itself an intranet telling crawlers to stay
out entirely. The report PDFs and the manifest live on so.jst.gob.ar, which
serves `Allow: /`, so the data is public; this pipeline just reached it through
the wrong door.
"""
import pytest

from jst_ingest import jst, pipeline


def test_discover_refuses_to_run(conn):
    with pytest.raises(pipeline.SourcePaused):
        pipeline.discover(conn, object())


def test_the_error_says_what_to_do(conn):
    with pytest.raises(pipeline.SourcePaused) as excinfo:
        pipeline.discover(conn, object())
    message = str(excinfo.value)
    # An operator reading a failed timer has to learn three things from it:
    # which host, why, and what to do next.
    assert "intranet.jst.gob.ar" in message
    assert "Disallow" in message
    assert "timer" in message


def test_it_does_not_touch_the_network_first(conn, monkeypatch):
    # The pause has to come BEFORE the manifest fetch, or a paused source still
    # makes a request every cycle.
    def _boom(*a, **k):
        raise AssertionError("paused discover must not fetch anything")

    monkeypatch.setattr(jst, "fetch_manifest", _boom)
    monkeypatch.setattr(jst, "fetch_events_page", _boom)
    with pytest.raises(pipeline.SourcePaused):
        pipeline.discover(conn, object())


def test_the_real_walk_is_kept_not_deleted():
    # Reopening this source should start from working code, not a git
    # archaeology exercise.
    assert callable(pipeline._discover_impl)
