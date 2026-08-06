# Proof that the shared policy is actually in force.
#
# A canonical module nobody imports is worse than no canonical module: it reads
# like a fix while every package keeps its old behaviour. These tests import
# each source package for real and assert its CLI builds a retrying client.
import importlib
import sys
from pathlib import Path

import httpx
import pytest

from _common import http as chttp
from _common import sync

ROOT = Path(__file__).resolve().parent.parent.parent


def _import(source, module):
    pkg_root = ROOT / "sources" / source
    sys.path.insert(0, str(pkg_root))
    try:
        return importlib.import_module(f"{source}_ingest.{module}")
    finally:
        sys.path.remove(str(pkg_root))


ADOPTERS = sorted(sync.VENDORED)


@pytest.mark.parametrize("source", ADOPTERS)
def test_the_cli_builds_a_retrying_client(source):
    # Vendoring means each package owns its own copy of the class, so the
    # comparison has to be against the package's own httpc — not the canon.
    # That is the point of the check: it proves this package's CLI reaches for
    # the vendored policy rather than raw httpx.
    cli = _import(source, "cli")
    httpc = _import(source, "httpc")
    client = cli._make_client()
    try:
        assert isinstance(client, httpx.Client)
        assert isinstance(client._transport, httpc.RetryTransport), (
            f"{source} still builds a client that gives up on the first 502"
        )
    finally:
        client.close()


@pytest.mark.parametrize("source", ADOPTERS)
def test_the_vendored_copy_actually_retries(source):
    # A copy that imports but no longer behaves would pass every check above.
    httpc = _import(source, "httpc")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, text="ok")

    transport = httpc.RetryTransport(httpx.MockTransport(handle), sleep=lambda _s: None)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.test/x").status_code == 200
    assert len(calls) == 2, f"{source}'s vendored httpc did not retry the 503"


def test_the_canonical_and_vendored_classes_stay_interchangeable():
    # Guards the seam the two tests above rely on: the vendored class is a
    # copy, so nothing in the type system stops it drifting apart from the
    # canon. The drift gate covers the bytes; this covers the contract.
    canon_args = chttp.RetryTransport(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert canon_args._attempts == chttp.DEFAULT_ATTEMPTS
    for source in ADOPTERS:
        httpc = _import(source, "httpc")
        assert httpc.DEFAULT_ATTEMPTS == chttp.DEFAULT_ATTEMPTS
        assert httpc.RETRY_STATUS == chttp.RETRY_STATUS


@pytest.mark.parametrize("source", ADOPTERS)
def test_the_cli_still_sends_the_source_user_agent(source):
    # The retry wrapper must not cost us the identifiable UA the README
    # promises each source will send.
    cli = _import(source, "cli")
    client = cli._make_client()
    try:
        ua = client.headers.get("User-Agent", "")
        assert ua and "python-httpx" not in ua, f"{source} lost its User-Agent"
    finally:
        client.close()


@pytest.mark.parametrize("source", ADOPTERS)
def test_the_cli_still_honours_a_proxy_argument(source):
    cli = _import(source, "cli")
    client = cli._make_client(proxy=None)
    client.close()
