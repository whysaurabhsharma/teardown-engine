"""Ashby job board collector.

GET https://api.ashbyhq.com/posting-api/job-board/{name}
The response carries no company name, so company_name_raw falls back to the
board name. posted_at is `publishedAt`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.guard.http import Guard
from engine.models import Signal
from engine.signal import ats

SOURCE = "ashby"
PREFIX = "ab"


def board_url(board_slug: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{ats.path_slug(board_slug)}"


def jobs_of(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    ats.require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
                SOURCE, "no jobs list")
    return payload["jobs"], {}


def to_signal(slug: str, job: dict[str, Any], context: dict[str, Any], today: date) -> Signal:
    secondary = [s.get("location") for s in job.get("secondaryLocations") or []
                 if isinstance(s, dict)]
    return Signal(
        signal_id=ats.signal_id(PREFIX, slug, job["id"]),
        source=SOURCE,
        board_slug=slug,
        company_name_raw=slug,
        title=job["title"],
        location=ats.join_locations(job.get("location"), *secondary),
        posted_at=ats.iso_date(job.get("publishedAt")),
        url=ats.text_or_none(job.get("jobUrl")),
        first_seen=today,
        description_text=(ats.text_or_none(job.get("descriptionPlain"))
                          or ats.html_to_text(job.get("descriptionHtml"))),
    )


async def fetch_board(
    board_slug: str, guard: Guard | None = None, *, today: date | None = None
) -> ats.BoardResult:
    return await ats.fetch_board(
        source=SOURCE, board_slug=board_slug, url=board_url(board_slug),
        jobs_of=jobs_of, to_signal=to_signal, guard=guard, today=today,
    )


async def collect(
    board_slug: str, guard: Guard | None = None, *, today: date | None = None
) -> list[Signal]:
    return (await fetch_board(board_slug, guard, today=today)).signals
