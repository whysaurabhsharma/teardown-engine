"""Lever postings collector.

GET https://api.lever.co/v0/postings/{company}?mode=json
The response is a bare JSON list of postings and carries no company name, so
company_name_raw falls back to the board slug. posted_at is `createdAt`
(epoch milliseconds).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.guard.http import Guard
from engine.models import Signal
from engine.signal import ats

SOURCE = "lever"
PREFIX = "lv"


def board_url(board_slug: str) -> str:
    return f"https://api.lever.co/v0/postings/{ats.path_slug(board_slug)}?mode=json"


def jobs_of(payload: Any) -> tuple[list[Any], dict[str, Any]]:
    ats.require(isinstance(payload, list), SOURCE, "not a list of postings")
    return payload, {}


def _description(job: dict[str, Any]) -> str | None:
    lists = []
    for item in job.get("lists") or []:
        if isinstance(item, dict):
            lists.append(ats.join_text(ats.text_or_none(item.get("text")),
                                       ats.html_to_text(item.get("content"))))
    return ats.join_text(
        ats.text_or_none(job.get("descriptionPlain")) or ats.html_to_text(job.get("description")),
        *lists,
        ats.text_or_none(job.get("additionalPlain")) or ats.html_to_text(job.get("additional")),
    )


def to_signal(slug: str, job: dict[str, Any], context: dict[str, Any], today: date) -> Signal:
    cats = job.get("categories") if isinstance(job.get("categories"), dict) else {}
    all_locations = cats.get("allLocations") if isinstance(cats.get("allLocations"), list) else []
    return Signal(
        signal_id=ats.signal_id(PREFIX, slug, job["id"]),
        source=SOURCE,
        board_slug=slug,
        company_name_raw=slug,
        title=job["text"],
        location=ats.join_locations(cats.get("location"), *all_locations),
        posted_at=ats.iso_date(job.get("createdAt")),
        url=ats.text_or_none(job.get("hostedUrl")),
        first_seen=today,
        description_text=_description(job),
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
