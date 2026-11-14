"""Greenhouse job board collector.

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Descriptions come only with ?content=true, entity-escaped HTML in `content`.
posted_at is the job's `updated_at`, as docs/PLAN.md Step 21 specifies.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.guard.http import Guard
from engine.models import Signal
from engine.signal import ats

SOURCE = "greenhouse"
PREFIX = "gh"


def board_url(board_slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{ats.path_slug(board_slug)}/jobs?content=true"


def jobs_of(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    ats.require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
                SOURCE, "no jobs list")
    return payload["jobs"], {}


def to_signal(slug: str, job: dict[str, Any], context: dict[str, Any], today: date) -> Signal:
    location = job.get("location")
    return Signal(
        signal_id=ats.signal_id(PREFIX, slug, job["id"]),
        source=SOURCE,
        board_slug=slug,
        company_name_raw=ats.text_or_none(job.get("company_name")) or slug,
        title=job["title"],
        location=ats.text_or_none(location.get("name")) if isinstance(location, dict) else None,
        posted_at=ats.iso_date(job.get("updated_at")),
        url=ats.text_or_none(job.get("absolute_url")),
        first_seen=today,
        description_text=ats.html_to_text(job.get("content"), escaped=True),
    )


async def fetch_board(
    board_slug: str, guard: Guard | None = None, *, today: date | None = None,
    fresh: bool = False,
) -> ats.BoardResult:
    return await ats.fetch_board(
        source=SOURCE, board_slug=board_slug, url=board_url(board_slug),
        jobs_of=jobs_of, to_signal=to_signal, guard=guard, today=today, fresh=fresh,
    )


async def collect(
    board_slug: str, guard: Guard | None = None, *, today: date | None = None
) -> list[Signal]:
    return (await fetch_board(board_slug, guard, today=today)).signals
