"""robots.txt was documentation, not behaviour.

README and CONTRIBUTING both promise these scrapers honour it. Nothing did:
robots_policy was seed metadata, CrawlErrorTypeRobotsBlocked was an enum
member used only by tests, and no fetcher ever asked a site what it allowed.
"""
import httpx
import pytest

from _common import robots

UA = "bea-ingest/1.0"

ROBOTS = """\
User-agent: *
Crawl-delay: 5
Disallow: /private/
Disallow: /admin

User-agent: bea-ingest/1.0
Disallow: /private/
"""


def _client(body=ROBOTS, status=200):
    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text=body)
        return httpx.Response(200, text="page")
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def clean():
    robots.reset_cache()
    yield
    robots.reset_cache()


class TestADisallowIsEnforced:
    def test_a_disallowed_path_raises(self):
        with _client() as c:
            with pytest.raises(robots.RobotsDisallowed):
                robots.check(c, "https://example.test/private/report.pdf", UA)

    def test_an_allowed_path_passes(self):
        with _client() as c:
            robots.check(c, "https://example.test/reports/2019.pdf", UA)


class TestAMissingRobotsMeansNoRestrictions:
    # RFC 9309: 4xx means unrestricted. And treating a transient 5xx as
    # "crawl nothing" would silently empty a run — the failure mode all of
    # this work is about.
    @pytest.mark.parametrize("status", [404, 410, 500, 503])
    def test_it_does_not_block(self, status):
        with _client(status=status) as c:
            robots.check(c, "https://example.test/private/report.pdf", UA)

    def test_a_transport_failure_does_not_block(self):
        def boom(request):
            raise httpx.ConnectError("refused")
        with httpx.Client(transport=httpx.MockTransport(boom)) as c:
            robots.check(c, "https://example.test/private/report.pdf", UA)


class TestItIsFetchedOncePerHost:
    def test_the_second_check_uses_the_cache(self):
        calls = []

        def handle(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=ROBOTS)

        with httpx.Client(transport=httpx.MockTransport(handle)) as c:
            robots.check(c, "https://example.test/a.pdf", UA)
            robots.check(c, "https://example.test/b.pdf", UA)
        assert calls.count("https://example.test/robots.txt") == 1


class TestCrawlDelay:
    # These use an agent that only matches the "*" group. A site that names
    # our agent explicitly takes that group INSTEAD of "*", including its
    # absent Crawl-delay — that is RFC 9309's most-specific-group rule, and
    # the test below pins it so it is not mistaken for a bug later.
    OTHER = "aaib-ingest/1.0"

    def test_the_site_wins_when_it_asks_for_more(self):
        with _client() as c:
            assert robots.crawl_delay(c, "https://example.test/x", self.OTHER,
                                      default=1.5) == 5.0

    def test_our_own_delay_wins_when_it_is_slower(self):
        with _client() as c:
            assert robots.crawl_delay(c, "https://example.test/x", self.OTHER,
                                      default=9.0) == 9.0

    def test_a_global_crawl_delay_applies_to_a_named_agent_too(self):
        # bea-ingest/1.0 has its own group with no Crawl-delay. We consult "*"
        # as well and take the larger value: it is the more polite reading, and
        # it is stable across interpreters — urllib.robotparser falls back to
        # "*" on Python 3.11 and does not on 3.14, so relying on the stdlib
        # would make pacing depend on the Python version.
        with _client() as c:
            assert robots.crawl_delay(c, "https://example.test/x", UA,
                                      default=1.5) == 5.0

    def test_no_robots_leaves_our_delay_alone(self):
        with _client(status=404) as c:
            assert robots.crawl_delay(c, "https://example.test/x", UA, default=1.5) == 1.5


class TestTheGuardIsActuallyWiredIntoMakeClient:
    """A policy module nobody calls is the bug this replaces, so prove the
    wiring, not just the parser."""

    def _seed(self, host, body=ROBOTS):
        import time
        import urllib.robotparser
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        robots._CACHE[("https", host)] = (parser, time.time())

    def test_a_disallowed_url_is_refused_by_a_real_client(self):
        from _common import http as chttp

        self._seed("example.test")
        reached = []
        inner = httpx.MockTransport(lambda r: reached.append(r) or httpx.Response(200))
        guard = chttp.RobotsGuardTransport(inner, UA, delay=1.0)
        with httpx.Client(transport=guard) as client:
            with pytest.raises(robots.RobotsDisallowed):
                client.get("https://example.test/private/report.pdf")
        assert reached == []

    def test_an_allowed_url_goes_through(self):
        from _common import http as chttp

        self._seed("example.test")
        inner = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
        guard = chttp.RobotsGuardTransport(inner, UA, delay=1.0)
        with httpx.Client(transport=guard) as client:
            assert client.get("https://example.test/reports/2019.pdf").text == "ok"

    def test_make_client_installs_it_by_default(self):
        from _common import http as chttp

        client = chttp.make_client(headers={"User-Agent": UA})
        try:
            chain, t = [], client._transport
            while hasattr(t, "_inner"):
                chain.append(type(t).__name__)
                t = t._inner
            assert "RobotsGuardTransport" in chain
        finally:
            client.close()

    def test_a_source_can_opt_out_deliberately(self):
        from _common import http as chttp

        client = chttp.make_client(headers={"User-Agent": UA}, obey_robots=False)
        try:
            chain, t = [], client._transport
            while hasattr(t, "_inner"):
                chain.append(type(t).__name__)
                t = t._inner
            assert "RobotsGuardTransport" not in chain
        finally:
            client.close()
