"""robots.txt enforcement must stay the default, with one named exception.

The gate is only worth having if `obey_robots=False` is hard to add quietly.
BFU is the single source that needs it — its robots.txt Disallows /SiteGlobals/
and its discover() enumerates through the search form that lives there — and
that is a documented stopgap until it moves to the allowed OpenData files.

If a second source turns up here, that is a decision to make on purpose, not a
line to copy from bfu/cli.py.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ALLOWED = {"bfu"}

_OPT_OUT = re.compile(r"obey_robots\s*=\s*False")


def _sources_opting_out():
    out = set()
    for path in sorted(ROOT.glob("sources/*/**/*.py")):
        if "tests" in path.parts:
            continue
        if _OPT_OUT.search(path.read_text(encoding="utf-8", errors="replace")):
            out.add(path.relative_to(ROOT).parts[1])
    return out


def test_only_the_named_sources_skip_robots():
    opting_out = _sources_opting_out()
    unexpected = opting_out - ALLOWED
    assert not unexpected, (
        f"{sorted(unexpected)} disable the robots.txt guard. That is allowed "
        "only as a deliberate, documented exception — add the source to "
        "ALLOWED here, with the reason in its cli.py, or leave the guard on."
    )


def test_the_exemption_is_documented_where_it_is_used():
    # An exemption with no explanation becomes folklore. Every one of them has
    # to say which rule it breaks and why, next to the call.
    for code in sorted(_sources_opting_out()):
        cli = ROOT / "sources" / code / f"{code}_ingest" / "cli.py"
        body = cli.read_text(encoding="utf-8", errors="replace")
        assert "robots.txt" in body, f"{code} opts out with no mention of robots.txt"
        assert "Disallow" in body, (
            f"{code} opts out without naming the rule it breaks"
        )


def test_bfu_still_needs_it():
    # If BFU ever stops driving the Disallowed search form — the OpenData
    # migration — this test is the reminder to take the exemption back out.
    bfu = (ROOT / "sources" / "bfu" / "bfu_ingest" / "bfu.py").read_text(encoding="utf-8")
    assert "/SiteGlobals/" in bfu, (
        "bfu no longer fetches anything under /SiteGlobals/ — the robots "
        "exemption in bfu/bfu_ingest/cli.py is obsolete, remove it"
    )
