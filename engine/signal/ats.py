"""What the four ATS collectors share: one board fetch through Guard, the
outcome of that fetch, and the normalising helpers (dates, text, ids).

A board fetch has one of these results, none of which raises:
- ok with jobs        a real board with open roles
- ok with no jobs     a real board with nothing open ("empty")
- not_found           404: a wrong or private board slug
- blocked, dead, robots_denied   Guard's other outcomes, recorded as they are

The raw JSON is stored by Guard's cache (data/cache/<sha256>, indexed by URL
with its outcome and status), which is also the durable record of a board's
outcome. Nothing here stores it a second time.

Only an ok response whose body is not the board JSON we expect raises
BoardFormatError; that is a change on their side we need to look at.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from engine.guard.http import FetchResult, Guard, default_guard
from engine.models import Outcome, Signal

FETCH_KIND = "ats_board"
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BoardFormatError(ValueError):
    """An ok response that is not the board JSON the collector expects."""


def check_slug(board_slug: str) -> str:
    slug = board_slug.strip()
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(f"not a board slug: {board_slug!r}")
    return slug


def path_slug(board_slug: str) -> str:
    return quote(check_slug(board_slug), safe="")


def signal_id(prefix: str, board_slug: str, job_id: object) -> str:
    """Deterministic id, e.g. gh-acme-4411: the same post always maps to the same row."""
    jid = str(job_id).strip()
    if not jid:
        raise ValueError("job has no id")
    return f"{prefix}-{board_slug.strip().lower()}-{jid}"


def iso_date(value: object) -> date | None:
    """A post date as a UTC calendar date.

    Accepts epoch milliseconds (Lever), ISO 8601 datetimes with or without an
    offset (Greenhouse, Ashby; naive ones are taken as UTC) and plain ISO dates
    (Workable). Anything else is None: a bad date does not drop the post.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if len(s) == 10:
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).date()


def html_to_text(markup: str | None, *, escaped: bool = False) -> str | None:
    """Plain text from description HTML. escaped=True first undoes entity
    escaping (Greenhouse sends its content as &lt;p&gt;...)."""
    if markup is None:
        return None
    if escaped:
        markup = html.unescape(markup)
    text = BeautifulSoup(markup, "html.parser").get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out = "\n".join(line for line in lines if line)
    return out or None


def join_text(*parts: str | None) -> str | None:
    kept = [p.strip() for p in parts if p and p.strip()]
    return "\n\n".join(kept) or None


def join_locations(*names: str | None) -> str | None:
    """Distinct non-blank locations, primary first, joined with '; '."""
    seen: list[str] = []
    for n in names:
        if isinstance(n, str) and n.strip() and n.strip() not in seen:
            seen.append(n.strip())
    return "; ".join(seen) or None


def text_or_none(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class BoardResult:
    source: str
    board_slug: str
    url: str
    outcome: Outcome
    status: int | None
    signals: list[Signal] = field(default_factory=list)
    skipped: int = 0  # jobs in the JSON without an id or a title
    from_cache: bool = False
    requests: int = 0
    sha256: str | None = None  # raw body in the cache
    detail: str = ""

    @property
    def state(self) -> str:
        """jobs | empty | not_found | blocked | dead | robots_denied."""
        if self.outcome == "ok":
            return "jobs" if self.signals else "empty"
        return self.outcome

    @property
    def note(self) -> str:
        return {
            "jobs": f"{len(self.signals)} jobs",
            "empty": "real board, nothing open",
            "not_found": "wrong or private board",
        }.get(self.state, self.detail)


JobsOf = Callable[[Any], tuple[list[Any], dict[str, Any]]]
ToSignal = Callable[[str, dict[str, Any], dict[str, Any], date], Signal]


async def fetch_board(
    *,
    source: str,
    board_slug: str,
    url: str,
    jobs_of: JobsOf,
    to_signal: ToSignal,
    guard: Guard | None = None,
    today: date | None = None,
    fresh: bool = False,
) -> BoardResult:
    """Fetch one board through Guard and normalise its jobs.

    jobs_of(payload) -> (jobs, board context) raises BoardFormatError on a
    payload of the wrong shape. to_signal(slug, job, context, today) builds one
    Signal; a job it cannot place (no id, no title) is counted as skipped.
    fresh=True bypasses today's cache (the send-day re-poll, Step 70).
    """
    slug = check_slug(board_slug)
    g = guard or default_guard()
    today = today or datetime.now(UTC).date()
    r: FetchResult = await g.fetch(url, FETCH_KIND, fresh=fresh)
    base = dict(
        source=source,
        board_slug=slug,
        url=r.url,
        outcome=r.outcome,
        status=r.status,
        from_cache=r.from_cache,
        requests=r.requests,
        sha256=r.sha256,
        detail=r.detail,
    )
    if r.outcome != "ok":
        return BoardResult(**base)
    try:
        payload = json.loads(r.body or b"")
    except ValueError as e:
        raise BoardFormatError(f"{source}:{slug}: response is not JSON ({e})") from None
    jobs, context = jobs_of(payload)
    signals: list[Signal] = []
    skipped = 0
    for job in jobs:
        if not isinstance(job, dict):
            skipped += 1
            continue
        try:
            signals.append(to_signal(slug, job, context, today))
        except (KeyError, TypeError, ValueError):
            skipped += 1
    return BoardResult(**base, signals=signals, skipped=skipped)


def require(cond: bool, source: str, what: str) -> None:
    if not cond:
        raise BoardFormatError(f"{source}: unexpected board JSON ({what})")


def dedupe(signals: Iterable[Signal]) -> list[Signal]:
    out: dict[str, Signal] = {}
    for s in signals:
        out.setdefault(s.signal_id, s)
    return list(out.values())
