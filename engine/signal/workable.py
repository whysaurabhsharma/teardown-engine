"""Workable account widget collector.

GET https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true
The Sourcing table gives the URL without a query; ?details=true is the widget
API's switch for including each job's description, the counterpart of
Greenhouse's ?content=true. posted_at is `published_on`, else `created_at`.
The account's `name` is the company name.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.guard.http import Guard
from engine.models import Signal
from engine.signal import ats

SOURCE = "workable"
PREFIX = "wk"


def board_url(board_slug: str) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{ats.path_slug(board_slug)}?details=true"


def jobs_of(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    ats.require(isinstance(payload, dict) and isinstance(payload.get("jobs"), list),
                SOURCE, "no jobs list")
    return payload["jobs"], {"name": ats.text_or_none(payload.get("name"))}


def _place(d: dict[str, Any], region_key: str, country_key: str) -> str | None:
    parts = [ats.text_or_none(d.get(k)) for k in ("city", region_key, country_key)]
    return ", ".join(p for p in parts if p) or None


def _location(job: dict[str, Any]) -> str | None:
    places = [_place(loc, "region", "country") for loc in job.get("locations") or []
              if isinstance(loc, dict) and not loc.get("hidden")]
    return ats.join_locations(*places) or _place(job, "state", "country")


def to_signal(slug: str, job: dict[str, Any], context: dict[str, Any], today: date) -> Signal:
    return Signal(
        signal_id=ats.signal_id(PREFIX, slug, job["shortcode"]),
        source=SOURCE,
        board_slug=slug,
        company_name_raw=context.get("name") or slug,
        title=job["title"],
        location=_location(job),
        posted_at=ats.iso_date(job.get("published_on")) or ats.iso_date(job.get("created_at")),
        url=ats.text_or_none(job.get("url")) or ats.text_or_none(job.get("shortlink")),
        first_seen=today,
        description_text=ats.html_to_text(job.get("description")),
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
