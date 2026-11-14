"""`te sync`: the campaign into HubSpot (docs/PLAN.md Steps 67 and 76, Gates 5.3
and 6.2). SQLite stays the system of record; HubSpot is the view shown to people.

    te sync [--company <id>] [--limit n] [--pilot] [--dry-run]

For every company with a touch drafted or sent (every send to date, and the
queued drafts), an idempotent upsert of:
  company  by domain: name, domain
  contact  by email: email, firstname, lastname, jobtitle. Contact data goes to
           the person's own CRM by design of the plan; it never reaches a
           prompt, a log or this module's output. Skipped when there is no
           address, or the address or its domain is suppressed (an opt-out or
           a bounce: CONDUCT Part 3).
  deal     one per company, found by the te_company_id deal property, in the
           "Teardown outreach" pipeline: dealname, te_company_id, segment,
           offer_type, gap_type, slice_kind, teardown_url (left out once the
           domain is suppressed: the page is deleted), made_by, and the stage
           from STAGE_RULES.
and the associations deal-company, deal-contact and contact-company, once.

Idempotent: the HubSpot id of each record and a hash of the properties last
written are kept in hubspot_sync. A record whose properties hash the same is
not requested at all; a re-run with nothing changed sends no request. A record
with no stored id is searched for first (domain, email, te_company_id) and
updated when found, so a lost database row never makes a duplicate. The deal
id is also written to touches.hubspot_deal_id.

--dry-run needs no token and sends nothing: it prints, per company, what would
be created or updated and the stage. Output names company ids and HubSpot
stage labels only; never a name, an address or a title.

The pipeline and its stages are created by hand in the HubSpot UI (Step 67):
pipeline PIPELINE_LABEL with the stages of STAGES, and the deal properties of
DEAL_PROPERTIES. The pipeline's stage ids are read once per run, only when a
deal is written.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from engine import db
from engine.build import render as R
from engine.models import HubspotSync
from engine.outreach import metrics as M
from engine.outreach.draft import _domain_forms, now_utc, stamp
from engine.outreach.replies import latest_sent
from engine.states import Status

SYNC_VERSION = "sync-1.0"
PIPELINE_LABEL = "Teardown outreach"
STAGES = ("Queued", "Sent", "Replied", "Meeting held", "In process", "Won", "Closed")
DEAL_PROPERTIES = ("te_company_id", "segment", "offer_type", "gap_type", "slice_kind",
                   "teardown_url", "made_by")
CLOSE_STAGE = {"hiring_process": "In process", "paid_project": "Won", "not_now": "Closed",
               "no_fit": "Closed", "other": "Closed"}
CLOSING_REPLIES = ("optout", "negative")  # a reply that ends the thread


@dataclass(frozen=True)
class StageInput:
    status: str
    sent: bool
    reply_types: frozenset[str]
    latest_outcome: str | None  # the latest touch outcome (replied_<type>, bounced)
    meetings: int
    close: str | None


def stage_for(s: StageInput) -> str:
    """The deal stage: the one place the mapping is defined (STAGE_RULES, in order).

      a close logged (Step 82)       hiring_process -> In process, paid_project -> Won,
                                     not_now | no_fit | other -> Closed
      an opt-out reply               Closed
      status closed or dropped       Closed
      a meeting held                 Meeting held
      the latest outcome negative    Closed
      any reply                      Replied
      a touch sent (bounced too)     Sent
      otherwise (drafted, queued)    Queued
    """
    if s.close is not None:
        return CLOSE_STAGE[s.close]
    if "optout" in s.reply_types:
        return "Closed"
    if s.status in (Status.CLOSED, Status.DROPPED):
        return "Closed"
    if s.meetings > 0 or s.status == Status.MEETING:
        return "Meeting held"
    if s.latest_outcome == "replied_negative":
        return "Closed"
    if s.reply_types or s.status == Status.REPLIED:
        return "Replied"
    if s.sent:
        return "Sent"
    return "Queued"


STAGE_RULES = stage_for.__doc__


def _hash(props: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(props, sort_keys=True).encode("utf-8")).hexdigest()


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    sql = ("SELECT DISTINCT c.company_id FROM companies c JOIN touches t ON t.company_id = "
           "c.company_id WHERE (t.status IN ('drafted', 'sent') OR t.sent_at IS NOT NULL)")
    params: list[Any] = []
    if company_id is not None:
        sql += " AND c.company_id = ?"
        params.append(company_id)
    if pilot:
        sql += " AND c.pilot = 1"
    sql += " ORDER BY c.company_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [r[0] for r in conn.execute(sql, params)]


def stage_input(conn: sqlite3.Connection, company_id: str) -> StageInput:
    c = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    types: set[str] = set()
    for r in conn.execute("SELECT type FROM replies WHERE company_id = ?", (company_id,)):
        types.add(r["type"])
    latest = None
    for t in conn.execute("SELECT outcome FROM touches WHERE company_id = ? AND outcome IS "
                          "NOT NULL ORDER BY touch_no", (company_id,)):
        latest = t["outcome"]
        if latest.startswith("replied_"):
            types.add(latest[len("replied_"):])
    meetings = int(conn.execute("SELECT COUNT(*) FROM meetings WHERE company_id = ?",
                                (company_id,)).fetchone()[0])
    cl = conn.execute("SELECT outcome FROM closes WHERE company_id = ?",
                      (company_id,)).fetchone()
    return StageInput(c["status"], latest_sent(conn, company_id) is not None,
                      frozenset(types), latest, meetings, cl["outcome"] if cl else None)


def _suppressed(conn: sqlite3.Connection, values: set[str]) -> bool:
    rows = {r[0].strip().lower() for r in conn.execute("SELECT email_or_domain FROM suppressions")}
    return bool(values & rows)


@dataclass
class Plan:
    """What one company's sync writes. Holds contact values in memory only: never
    printed (repr is off for contact)."""

    company_id: str
    company: dict[str, str]
    contact_id: int | None
    contact: dict[str, str] | None = field(repr=False)
    contact_skip: str | None
    deal: dict[str, str]  # dealstage and pipeline as labels; ids at write time
    stage: str


def plan(conn: sqlite3.Connection, company_id: str) -> Plan:
    c = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    dims = M.dimensions(conn, company_id)[company_id]
    company = {"name": c["name"]}
    if c["domain"]:
        company["domain"] = c["domain"]
    domain_off = _suppressed(conn, _domain_forms(c["domain"]))
    from engine.people.contacts import recipient

    ct = recipient(conn, company_id)
    contact = None
    contact_id = None
    skip = None
    email = (ct["email"] or "").strip().lower() if ct is not None else ""
    if not email:
        skip = "no address on file"
    elif _suppressed(conn, {email} | _domain_forms(email.rsplit("@", 1)[-1])) or domain_off:
        skip = "address or domain suppressed"
    else:
        parts = (ct["name"] or "").split()
        contact = {"email": email}
        if parts:
            contact["firstname"] = parts[0]
        if len(parts) > 1:
            contact["lastname"] = " ".join(parts[1:])
        if ct["title"]:
            contact["jobtitle"] = ct["title"]
        contact_id = int(ct["contact_id"])
    si = stage_input(conn, company_id)
    stage = stage_for(si)
    deal = {"dealname": f"{c['name']} teardown", "pipeline": PIPELINE_LABEL,
            "dealstage": stage, "te_company_id": company_id,
            "made_by": dims["made_by"]}
    for key, dim in (("segment", "segment"), ("offer_type", "offer_type"),
                     ("gap_type", "gap"), ("slice_kind", "slice")):
        if dims[dim] != M.NONE:
            deal[key] = dims[dim]
    td = conn.execute("SELECT page_slug FROM teardowns WHERE company_id = ? AND page_slug IS "
                      "NOT NULL ORDER BY teardown_id LIMIT 1", (company_id,)).fetchone()
    if td is not None and not domain_off:
        url = R.future_url(td["page_slug"])
        if url:
            deal["teardown_url"] = url
    return Plan(company_id, company, contact_id, contact, skip, deal, stage)


def _stored(conn: sqlite3.Connection, obj: str, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hubspot_sync WHERE object = ? AND te_key = ?",
                        (obj, key)).fetchone()


def _store(conn: sqlite3.Connection, obj: str, key: str, hubspot_id: str,
           props_hash: str | None) -> None:
    with conn:
        if _stored(conn, obj, key) is None:
            db.insert(conn, HubspotSync(object=obj, te_key=key, hubspot_id=hubspot_id,
                                        props_sha256=props_hash))
        else:
            conn.execute("UPDATE hubspot_sync SET hubspot_id = ?, props_sha256 = ?, synced_at = ? "
                         "WHERE object = ? AND te_key = ?",
                         (hubspot_id, props_hash, stamp(now_utc()), obj, key))


@dataclass
class SyncResult:
    company_id: str
    stage: str
    actions: list[str] = field(default_factory=list)  # never a contact value

    @property
    def changed(self) -> bool:
        return any(not a.endswith("unchanged") and not a.startswith("contact skipped")
                   for a in self.actions)


class _Pipeline:
    """The pipeline's ids, read once per run on first need."""

    def __init__(self, client: Any):
        self.client = client
        self.ids: tuple[str, dict[str, str]] | None = None

    def resolve(self, deal: dict[str, str]) -> dict[str, str]:
        from engine.guard.hubspot import HubSpotError

        if self.ids is None:
            self.ids = self.client.deal_pipeline(PIPELINE_LABEL)
        pid, stages = self.ids
        if deal["dealstage"] not in stages:
            raise HubSpotError(f"the {PIPELINE_LABEL!r} pipeline has no stage "
                               f"{deal['dealstage']!r}; create the stages {', '.join(STAGES)}")
        return {**deal, "pipeline": pid, "dealstage": stages[deal["dealstage"]]}


_OBJ = {"company": ("companies", "domain"), "contact": ("contacts", "email"),
        "deal": ("deals", "te_company_id")}


def _upsert(conn: sqlite3.Connection, client: Any, kind: str, key: str, props: dict[str, str],
            search_value: str | None, dry_run: bool, pipeline: _Pipeline | None,
            res: SyncResult) -> str | None:
    h = _hash(props)
    rec = _stored(conn, kind, key)
    if rec is not None and rec["props_sha256"] == h:
        res.actions.append(f"{kind} unchanged")
        return rec["hubspot_id"]
    if dry_run:
        res.actions.append(f"{kind} would be {'updated' if rec is not None else 'upserted'}")
        return rec["hubspot_id"] if rec is not None else None
    obj, prop = _OBJ[kind]
    send = pipeline.resolve(props) if pipeline is not None else props
    hid = rec["hubspot_id"] if rec is not None else None
    if hid is None and search_value:
        hid = client.search(obj, prop, search_value)
    if hid is None:
        hid = client.create(obj, send)
        res.actions.append(f"{kind} created")
    else:
        client.update(obj, hid, send)
        res.actions.append(f"{kind} updated")
    _store(conn, kind, key, hid, h)
    return hid


def _associate(conn: sqlite3.Connection, client: Any, company_id: str, pair: str,
               a: tuple[str, str | None], b: tuple[str, str | None], dry_run: bool,
               res: SyncResult) -> None:
    if a[1] is None or b[1] is None:  # only on a dry run: a record not yet in HubSpot
        if dry_run:
            res.actions.append(f"{pair} would be associated")
        return
    key, joined = f"{company_id}:{pair}", f"{a[1]}-{b[1]}"
    rec = _stored(conn, "assoc", key)
    if rec is not None and rec["hubspot_id"] == joined:
        return
    if dry_run:
        res.actions.append(f"{pair} would be associated")
        return
    client.associate(a[0], a[1], b[0], b[1])
    _store(conn, "assoc", key, joined, None)
    res.actions.append(f"{pair} associated")


def sync_company(conn: sqlite3.Connection, client: Any, company_id: str, *,
                 dry_run: bool = False, pipeline: _Pipeline | None = None) -> SyncResult:
    p = plan(conn, company_id)
    res = SyncResult(company_id, p.stage)
    pipeline = pipeline or _Pipeline(client)
    co = _upsert(conn, client, "company", company_id, p.company, p.company.get("domain"),
                 dry_run, None, res)
    ct = None
    if p.contact is not None and p.contact_id is not None:
        ct = _upsert(conn, client, "contact", str(p.contact_id), p.contact,
                     p.contact["email"], dry_run, None, res)
    else:
        res.actions.append(f"contact skipped ({p.contact_skip})")
    deal = _upsert(conn, client, "deal", company_id, p.deal, company_id, dry_run, pipeline, res)
    _associate(conn, client, company_id, "deal-company", ("deals", deal), ("companies", co),
               dry_run, res)
    if p.contact is not None:
        _associate(conn, client, company_id, "deal-contact", ("deals", deal),
                   ("contacts", ct), dry_run, res)
        _associate(conn, client, company_id, "contact-company", ("contacts", ct),
                   ("companies", co), dry_run, res)
    if deal is not None and not dry_run:
        with conn:
            n = conn.execute("UPDATE touches SET hubspot_deal_id = ? WHERE company_id = ? AND "
                             "hubspot_deal_id IS NOT ?", (deal, company_id, deal)).rowcount
        if n:
            res.actions.append(f"{n} touch row(s) linked to the deal")
    return res


def sync(conn: sqlite3.Connection, client: Any, company_ids: list[str], *,
         dry_run: bool = False) -> list[SyncResult]:
    """Sync each company in turn. A HubSpot error stops the run (re-running resumes:
    every finished company is stored)."""
    return list(sync_iter(conn, client, company_ids, dry_run=dry_run))


def sync_iter(conn: sqlite3.Connection, client: Any, company_ids: list[str], *,
              dry_run: bool = False):
    """sync, one result at a time (the CLI prints as it goes)."""
    pipeline = _Pipeline(client)
    for cid in company_ids:
        yield sync_company(conn, client, cid, dry_run=dry_run, pipeline=pipeline)

