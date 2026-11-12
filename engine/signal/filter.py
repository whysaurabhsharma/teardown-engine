"""Freshness and place for each signal (docs/PLAN.md Step 25).

Freshness. A signal is fresh when its reference date, posted_at or, when the
post gives no date, first_seen (protocol/ICP.md, qualification rule 2), is no
more than WINDOW_DAYS (60) days before the run date, inclusive: 60 days old is
fresh, 61 is stale. A reference date after the run date (a board's clock, or a
`--today` in the past) counts as fresh. Stale signals are kept and marked
fresh = 0; later stages read only fresh = 1. Rows are never deleted.

Re-freshening. boards.store() is insert-only on signal_id: a re-collect never
rewrites posted_at or first_seen of a row it has seen before. So a stale signal
stays stale on every later run date. It becomes fresh again only if its dates
change, which today happens only by hand; a role re-posted under a new job id
or URL arrives as a new signal with its own dates. The same holds for careers
rows stored before datePosted was read: they keep posted_at NULL and first_seen
stands in.

Place. The head office is not known until Stage 2.2, so nothing is dropped on
location here. india_located records whether the location string names an
Indian place, for Step 28's rule (head office abroad needs an India-located
role). Matching is case-insensitive and whole-word against INDIA_TERMS: the
ICP.md discovery cities (Bengaluru, Bangalore, Hyderabad, Pune, Gurugram,
Gurgaon, Mumbai, Chennai, Noida, Remote India) plus Delhi, New Delhi, NCR and
India. "Remote" alone is not India, and a NULL location is not India.

The country code "IN" is also the US state code for Indiana ("Carmel, IN"), so
it counts only when a location entry (entries are split on ';' and '|', the
collectors' separator being '; ') is "IN" by itself, upper case, optionally
with the word Remote ("IN", "Remote - IN", "Remote (IN)"). "Pune, MH, IN"
matches through Pune; "Carmel, IN" does not match.

The filter makes no network or model call. It writes only fresh and
india_located, and only where the value differs, so a re-run with the same
inputs changes nothing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

WINDOW_DAYS = 60

INDIA_TERMS: tuple[str, ...] = (
    "bengaluru", "bangalore", "hyderabad", "pune", "gurugram", "gurgaon", "mumbai",
    "chennai", "noida", "new delhi", "delhi", "ncr", "remote india", "india",
)
_INDIA_RX = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in INDIA_TERMS)
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)
_ENTRY_SPLIT = re.compile(r"[;|]")
# "IN" upper case; "Remote" in any case.
_REMOTE_IN = re.compile(
    r"^(?:(?i:remote)\s*[-–—:,/(]?\s*)?IN\)?$|^IN\s*[-–—:,/(]\s*(?i:remote)\)?$")

# role_family values that are GTM: everything the classifier gives except not_gtm.
NOT_GTM = "not_gtm"
SEED_SOURCES = ("careers",)


def reference_date(posted_at: date | None, first_seen: date) -> date:
    return posted_at if posted_at is not None else first_seen


def is_fresh(
    posted_at: date | None, first_seen: date, today: date, window_days: int = WINDOW_DAYS
) -> bool:
    return (today - reference_date(posted_at, first_seen)).days <= window_days


def is_india_located(location: str | None) -> bool:
    if not location or not location.strip():
        return False
    if _INDIA_RX.search(location):
        return True
    for entry in _ENTRY_SPLIT.split(location):
        if _REMOTE_IN.match(entry.strip()):
            return True
    return False


def _date(value: object) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    return date.fromisoformat(s[:10]) if s else None


@dataclass
class FilterReport:
    total: int = 0
    fresh: int = 0
    stale: int = 0
    india_located: int = 0
    fresh_gtm: int = 0
    fresh_gtm_seed: int = 0  # source == careers; ATS hand-offs are not traceable
    fresh_gtm_missing: int = 0  # fresh GTM rows lacking url, offer_type or company name
    changed: int = 0  # rows whose fresh or india_located was (or would be) written
    by_source: dict[str, int] = field(default_factory=dict)  # fresh GTM per source


def run(
    conn: sqlite3.Connection,
    *,
    today: date,
    window_days: int = WINDOW_DAYS,
    limit: int | None = None,
    company_id: str | None = None,
    dry_run: bool = False,
) -> FilterReport:
    """Compute fresh and india_located for signals (all, or the first `limit` by
    signal_id, or one company's) and write the ones that differ."""
    sql = ("SELECT signal_id, source, company_name_raw, role_family, offer_type, url, "
           "location, posted_at, first_seen, fresh, india_located FROM signals")
    params: list[object] = []
    if company_id is not None:
        sql += " WHERE company_id = ?"
        params.append(company_id)
    sql += " ORDER BY signal_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    rep = FilterReport()
    updates: list[tuple[int, int, str]] = []
    for r in rows:
        first_seen = _date(r["first_seen"])
        assert first_seen is not None  # NOT NULL in the schema
        fresh = int(is_fresh(_date(r["posted_at"]), first_seen, today, window_days))
        india = int(is_india_located(r["location"]))
        rep.total += 1
        rep.fresh += fresh
        rep.stale += 1 - fresh
        rep.india_located += india
        if fresh and r["role_family"] is not None and r["role_family"] != NOT_GTM:
            rep.fresh_gtm += 1
            rep.by_source[r["source"]] = rep.by_source.get(r["source"], 0) + 1
            if r["source"] in SEED_SOURCES:
                rep.fresh_gtm_seed += 1
            if not (r["url"] and r["offer_type"] and r["company_name_raw"]):
                rep.fresh_gtm_missing += 1
        if (r["fresh"], r["india_located"]) != (fresh, india):
            updates.append((fresh, india, r["signal_id"]))
    rep.changed = len(updates)
    if updates and not dry_run:
        with conn:
            conn.executemany(
                "UPDATE signals SET fresh = ?, india_located = ? WHERE signal_id = ?", updates)
    return rep

