"""These clients follow redirects on a box that also hosts the databases.

A PDF href or a 302 taken from a scraped listing pointing at
http://169.254.169.254/ or http://127.0.0.1:8080/ used to be fetched without
question. The control plane's Go extract path has blocked exactly this since
GO-CP-8; the Python fetchers never did.
"""
import httpx
import pytest

from _common import http as chttp


class TestAPrivateTargetIsRefused:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/secret",
        "http://localhost:5000/secret",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://10.0.0.5/internal.pdf",
        "http://192.168.1.10/report.pdf",
        "http://172.16.0.1/report.pdf",
        "http://[::1]:8080/secret",
        "http://0.0.0.0/",
    ])
    def test_the_request_never_reaches_the_wire(self, url):
        reached = []
        guard = chttp.SSRFGuardTransport(
            httpx.MockTransport(lambda r: reached.append(r) or httpx.Response(200)))
        with httpx.Client(transport=guard) as client:
            with pytest.raises(chttp.SSRFBlocked):
                client.get(url)
        assert reached == [], "the guard must refuse before the transport runs"


class TestAPublicTargetStillWorks:
    def test_a_public_literal_is_allowed(self):
        guard = chttp.SSRFGuardTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
        with httpx.Client(transport=guard) as client:
            assert client.get("http://1.1.1.1/report.pdf").text == "ok"

    def test_a_name_that_does_not_resolve_is_left_to_the_transport(self):
        # Not the guard's job to invent a DNS failure — it must not swallow
        # the real error either.
        guard = chttp.SSRFGuardTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
        with httpx.Client(transport=guard) as client:
            assert client.get("http://nx.invalid.test/x").text == "ok"


class TestMakeClientInstallsTheGuard:
    def test_a_default_client_carries_it(self):
        client = chttp.make_client()
        try:
            chain = []
            t = client._transport
            while hasattr(t, "_inner"):
                chain.append(type(t).__name__)
                t = t._inner
            assert "SSRFGuardTransport" in chain
        finally:
            client.close()

    def test_a_source_can_opt_out_deliberately(self):
        # For a source that genuinely talks to something on the LAN. It has to
        # be asked for by name.
        client = chttp.make_client(allow_private=True)
        try:
            chain = []
            t = client._transport
            while hasattr(t, "_inner"):
                chain.append(type(t).__name__)
                t = t._inner
            assert "SSRFGuardTransport" not in chain
        finally:
            client.close()
