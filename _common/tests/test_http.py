# The retry policy that the per-source CLIs were missing.
#
# Every scraper already passed httpx.HTTPTransport(retries=3), and the comment
# in ahac's CLI even credits it with fixing a crash. But httpx's own `retries`
# only covers connect errors — a 502 from the origin or a read timeout mid-body
# still raises on the first try. That is precisely the failure that truncates a
# listing walk, so it is the one worth testing.
import httpx
import pytest

from _common import http as chttp


class _Recorder:
    """Stands in for the real network: replays a scripted list of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def handle(self, request):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text="body")


def _client(outcomes, **kw):
    rec = _Recorder(outcomes)
    sleeps = []
    transport = chttp.RetryTransport(
        httpx.MockTransport(rec.handle),
        sleep=sleeps.append,
        **kw,
    )
    return httpx.Client(transport=transport), rec, sleeps


class TestRetriesTheThingsHttpxDoesNot:
    def test_a_503_is_retried_and_the_good_response_is_returned(self):
        client, rec, _ = _client([503, 200])
        resp = client.get("https://example.test/x")
        assert resp.status_code == 200
        assert rec.calls == 2

    def test_a_read_timeout_is_retried(self):
        client, rec, _ = _client([httpx.ReadTimeout("slow"), 200])
        assert client.get("https://example.test/x").status_code == 200
        assert rec.calls == 2

    def test_every_retryable_status_is_covered(self):
        for status in (429, 500, 502, 503, 504):
            client, rec, _ = _client([status, 200])
            assert client.get("https://example.test/x").status_code == 200, status
            assert rec.calls == 2, status

    def test_a_404_is_not_retried(self):
        # A missing report is an answer, not a transient fault. Retrying it
        # would triple the load we put on a source for nothing.
        client, rec, _ = _client([404])
        assert client.get("https://example.test/x").status_code == 404
        assert rec.calls == 1

    def test_a_200_costs_exactly_one_request(self):
        client, rec, _ = _client([200])
        assert client.get("https://example.test/x").status_code == 200
        assert rec.calls == 1


class TestGivingUp:
    def test_a_persistent_500_is_returned_rather_than_raised(self):
        # The caller decides what a 500 means. Raising here would turn a
        # single bad report into an exception that ends a whole crawl — the
        # ovv failure mode this module exists to prevent.
        client, rec, _ = _client([500, 500, 500], attempts=3)
        assert client.get("https://example.test/x").status_code == 500
        assert rec.calls == 3

    def test_a_persistent_timeout_raises_the_last_error(self):
        client, rec, _ = _client(
            [httpx.ReadTimeout("a"), httpx.ReadTimeout("b"), httpx.ReadTimeout("c")],
            attempts=3,
        )
        with pytest.raises(httpx.ReadTimeout):
            client.get("https://example.test/x")
        assert rec.calls == 3

    def test_attempts_is_honoured(self):
        client, rec, _ = _client([503, 503, 503, 503, 200], attempts=2)
        assert client.get("https://example.test/x").status_code == 503
        assert rec.calls == 2


class TestBackoff:
    def test_it_waits_longer_after_each_failure(self):
        client, _, sleeps = _client([503, 503, 200], attempts=3, backoff=1.0)
        client.get("https://example.test/x")
        assert sleeps == [1.0, 2.0]

    def test_a_successful_first_try_never_sleeps(self):
        client, _, sleeps = _client([200])
        client.get("https://example.test/x")
        assert sleeps == []


class TestMakeClient:
    def test_it_returns_a_client_carrying_the_source_headers(self):
        client = chttp.make_client(headers={"User-Agent": "test-agent/1.0"})
        try:
            assert client.headers["User-Agent"] == "test-agent/1.0"
            assert client.follow_redirects is True
        finally:
            client.close()

    def test_it_retries_by_default(self):
        # The whole point: a source package gets the policy without opting in.
        client = chttp.make_client()
        try:
            assert isinstance(client._transport, chttp.RetryTransport)
        finally:
            client.close()
