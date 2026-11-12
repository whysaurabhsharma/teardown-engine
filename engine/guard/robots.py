"""robots.txt rules, parsed with urllib.robotparser.

Fetching is Guard's job (engine/guard/http.py); this module only turns a
robots.txt response into a policy. When in doubt it denies:

- 2xx: parse the body.
- 401, 403: disallow everything (urllib.robotparser's own rule).
- other 4xx (404, 410...): no robots.txt, allow everything.
- 429, 5xx, or a challenge page refusing us: disallow everything.
"""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

ROBOTS_AGENT = "teardown-engine"  # product token matched against User-agent lines


def policy_from_response(status: int, body: bytes | None) -> RobotFileParser:
    rp = RobotFileParser()
    if 200 <= status < 300:
        text = (body or b"").decode("utf-8", errors="replace")
        rp.parse(text.splitlines())
    elif status in (401, 403):
        rp.disallow_all = True
    elif 400 <= status < 500 and status != 429:
        rp.allow_all = True
    else:
        rp.disallow_all = True
    return rp


def allowed(rp: RobotFileParser, url: str) -> bool:
    return rp.can_fetch(ROBOTS_AGENT, url)


def crawl_delay(rp: RobotFileParser) -> float | None:
    """Crawl-delay or Request-rate for our agent, in seconds between requests."""
    delay = rp.crawl_delay(ROBOTS_AGENT)
    if delay:
        return float(delay)
    rate = rp.request_rate(ROBOTS_AGENT)
    if rate and rate.requests:
        return rate.seconds / rate.requests
    return None
