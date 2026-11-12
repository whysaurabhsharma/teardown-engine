"""Manual entry: a role you saw yourself (docs/PLAN.md Step 23).

`te signal add --company "..." --title "..." --url "..."` records it with
source="manual". The id is "mn-{company slug}-{sha1(company|title|url)[:12]}",
so adding the same role twice inserts nothing the second time. LinkedIn and
Naukri URLs are fine here: a person read them, not code.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

from engine.models import Signal

SOURCE = "manual"
PREFIX = "mn"


def _norm(text: str) -> str:
    return " ".join(text.split())


def company_slug(company: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _norm(company).lower()).strip("-")
    return slug[:40].strip("-") or "company"


def manual_signal_id(company: str, title: str, url: str) -> str:
    key = "\n".join((_norm(company).lower(), _norm(title).lower(), url.strip()))
    return f"{PREFIX}-{company_slug(company)}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def make_signal(
    *,
    company: str,
    title: str,
    url: str,
    today: date,
    location: str | None = None,
    posted_at: date | None = None,
) -> Signal:
    """Raises ValueError for a blank company or title or a non-http(s) URL."""
    company, title, url = _norm(company), _norm(title), url.strip()
    if not company or not title:
        raise ValueError("company and title must not be blank")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"url must be an http(s) URL: {url!r}")
    return Signal(
        signal_id=manual_signal_id(company, title, url),
        source=SOURCE,
        company_name_raw=company,
        title=title,
        location=_norm(location) if location and location.strip() else None,
        posted_at=posted_at,
        url=url,
        first_seen=today,
    )
