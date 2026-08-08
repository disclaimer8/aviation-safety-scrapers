# ovv_ingest/httpc.py
#
# VENDORED from _common/http.py — do not edit here.
# Edit the canonical file and run `python -m _common.sync`; a test fails if a
# vendored copy drifts.
#
# The shared HTTP client policy.
#
# Every source package built its own client with
# httpx.HTTPTransport(retries=3), which reads as "transient faults are
# handled". It is not: httpx's `retries` covers connect errors only. A 502
# from the origin, or a read timeout after the connection is established,
# still raises on the first attempt.
#
# That gap has a cost we have already paid. A discover loop that walks a
# paginated listing and catches exceptions per page treats one 502 on page 30
# as the end of the listing, and returns normally with a truncated crawl. No
# error, no alert, just missing reports. Retrying the request is the cheap
# half of the fix (the other half is not treating an exception as
# end-of-data — that belongs to each pipeline).
#
# Politeness note: this retries a small, fixed number of times with growing
# backoff, and never retries a 4xx other than 429. A missing report is an
# answer; hammering a source for it would break the "slow and polite" rule in
# the repository README.
import time

import httpx

# 429 is included deliberately: it is the one 4xx that means "later", not "no".
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Timeouts and connection resets mid-request. httpx.NetworkError covers
# ConnectError/ReadError/WriteError; RemoteProtocolError catches a server that
# hangs up in the middle of a response, which several PDF archives do under
# load.
RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 0.5
DEFAULT_TIMEOUT = 120


class RetryTransport(httpx.BaseTransport):
    """Wrap a transport so 5xx/429 responses and transient errors are retried.

    attempts is the total number of tries, not the number of retries: 3 means
    one request and at most two more. The wait doubles each time, starting at
    `backoff` seconds.

    A response that is still failing on the last attempt is RETURNED, not
    raised — the caller decides whether a 500 on one report should end the
    run. A transient exception that survives every attempt is re-raised,
    because there is no response to hand back.
    """

    def __init__(self, inner, attempts=DEFAULT_ATTEMPTS, backoff=DEFAULT_BACKOFF,
                 sleep=time.sleep, retry_status=RETRY_STATUS):
        self._inner = inner
        self._attempts = max(1, int(attempts))
        self._backoff = float(backoff)
        self._sleep = sleep
        self._retry_status = frozenset(retry_status)

    def handle_request(self, request):
        last_exc = None
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
            last = attempt == self._attempts - 1
            try:
                response = self._inner.handle_request(request)
            except RETRY_EXCEPTIONS as exc:
                last_exc = exc
                continue
            if response.status_code in self._retry_status and not last:
                # Release the connection before asking for it again.
                response.close()
                continue
            return response
        raise last_exc

    def close(self):
        self._inner.close()


def make_client(headers=None, proxy=None, timeout=DEFAULT_TIMEOUT,
                attempts=DEFAULT_ATTEMPTS, backoff=DEFAULT_BACKOFF, **kwargs):
    """Build the httpx.Client a source package should use.

    Keeps httpx's own connect-level retries (they are free and cover a
    different failure) and layers the response-level policy on top.
    """
    inner = httpx.HTTPTransport(proxy=proxy or None, retries=attempts)
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=headers or {},
        transport=RetryTransport(inner, attempts=attempts, backoff=backoff),
        **kwargs,
    )
