# dgaccl_ingest/robots.py
#
# VENDORED from _common/robots.py — do not edit here.
# Edit the canonical file and run `python -m _common.sync`; a test fails if a
# vendored copy drifts.
#
# robots.txt, actually enforced.
#
# README and CONTRIBUTING both promise these scrapers honour robots.txt. Until
# now that was documentation only: `robots_policy` was seed metadata and an
# export column in the control plane, `CrawlErrorTypeRobotsBlocked` existed as
# an enum member used by nothing outside tests, and no fetcher ever asked a
# site what it allowed.
#
# The policy this implements is deliberately narrow:
#   * A Disallow that matches is refused, and the refusal is loud — the caller
#     decides whether to skip the URL or fail the run.
#   * A robots.txt that cannot be fetched (404, 5xx, timeout) is treated as
#     "no restrictions". That is what RFC 9309 says for 4xx, and treating a
#     transient 5xx as "crawl nothing" would silently empty a run — the exact
#     failure mode the rest of this work is about.
#   * Crawl-delay is honoured when the site sets one and it is larger than the
#     source's own DELAY.
#
# One fetch per host per process, cached.
import time
import urllib.robotparser
from urllib.parse import urlparse

# Cache: host -> (RobotFileParser or None, fetched_at)
_CACHE = {}
_CACHE_TTL = 3600


class RobotsDisallowed(Exception):
    """The site's robots.txt refuses this URL for this User-Agent."""


def _parser_for(client, url, user_agent):
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    key = (parts.scheme, parts.netloc)
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[1]) < _CACHE_TTL:
        return hit[0]

    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = client.get(robots_url, headers={"User-Agent": user_agent})
        if resp.status_code >= 400:
            # No robots.txt, or the server is unhappy: no restrictions.
            parser = None
        else:
            parser.parse(resp.text.splitlines())
    except Exception:
        # A transport failure must not read as "crawl nothing".
        parser = None

    _CACHE[key] = (parser, time.time())
    return parser


def check(client, url, user_agent):
    """Raise RobotsDisallowed if robots.txt refuses url for user_agent."""
    parser = _parser_for(client, url, user_agent)
    if parser is not None and not parser.can_fetch(user_agent, url):
        raise RobotsDisallowed(
            f"robots.txt at {urlparse(url).netloc} disallows {url} for "
            f"User-Agent {user_agent!r}"
        )


def crawl_delay(client, url, user_agent, default=0.0):
    """The site's Crawl-delay for this agent, or `default` when it asks for less."""
    parser = _parser_for(client, url, user_agent)
    if parser is None:
        return default
    try:
        declared = parser.crawl_delay(user_agent)
    except Exception:
        return default
    if declared is None:
        return default
    return max(float(declared), float(default))


def reset_cache():
    """For tests, and for a long-running process that should re-read."""
    _CACHE.clear()
