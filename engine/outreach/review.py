"""The review queue (docs/PLAN.md Steps 66 and 68; Gate 5.3).

    te queue open [--pilot] [--company <id>] [--limit n] [--port N] [--no-browser] [--dry-run]
    te queue report [--pilot] [--company <id>]

`open` serves one local HTML page per teardown from a stdlib http.server bound
to 127.0.0.1 only (never 0.0.0.0): the page as it renders now on the left (an
iframe of the current teardown file rendered in memory, which is what
`te teardown render` would write), and on the right the evidence (every
diagnosis claim with its quote, link and whether it passes), the diagnosis,
the facts, the live links to the captured pages, the quality_checks result with
its named reasons (and whether it is stale against the file), the `te teardown
check` failures, and the email preview with {first_name} unfilled. No contact
value appears anywhere on the page.

Every URL carries a random per-run token as its first path segment; a request
without it is a 404. A POST must also carry the token as a form field, come
with a Host of 127.0.0.1 or localhost on the served port, and, when the browser
sends an Origin, that same origin. Responses send Referrer-Policy: no-referrer
(the live links are external; the token must not leak) and a strict CSP.

The decisions (each a form POSTed to the local server):
  approve   calls draft.approve, the function behind `te queue approve`; a
            reviews row (decision approve) is written only when it approves.
  kill      with a reason code (KILL_REASONS) and a note. The approach, one
            rule for every kill: a kill is a hold on the teardown, not a drop.
            teardowns.review_state becomes 'killed', the reviews row keeps the
            reason and the file's sha256, and `te queue approve` refuses until
            the teardown file has changed (re-drafted or edited), because a
            Step 68 kill is fixed by changing a prompt and re-running one stage,
            which a terminal drop would forbid. Only a reason that is an ICP
            drop reason a person decides (ICP_DROP_REASONS, protocol/ICP.md:
            too_large, duplicate, conflict, staffing_or_agency, consumer,
            out_of_scope) says the company itself is out; it also drops the
            company with that reason at stage 'review_queue'.
  edited    records which drafted sections the person changed in the YAML:
            drafter.human_edits compares each section against the hash `te
            draft` recorded; for each changed section the fraction of its text
            changed against the drafter's cached output (difflib), or null when
            that output is not in llm_calls. A file te draft never wrote has no
            drafted sections and records none. approve and kill snapshot the same.
  render    re-renders the page (`te teardown render`), so an edited file can
            be approved.
Review minutes: typed in the form, else the time from first opening the
company's page in this run to the decision; written on the reviews row and, for
approve and kill, to teardowns.review_minutes (draft.done, Step 55).

`report` prints, for the trial batch (Step 68), the latest decision per
company: approvals, kills by reason (with the stage a fix belongs to, and the
notes), the most-edited sections, average review minutes, and the Gate 5.3
numbers.

No network, no model call, nothing sent.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import html
import json
import re
import secrets
import socketserver
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yaml

from engine import db, states
from engine.build import render as R
from engine.build import teardown as T
from engine.models import Review
from engine.outreach import draft as Q
from engine.states import Status

REVIEW_VERSION = "review-1.0"
HOST = "127.0.0.1"  # never 0.0.0.0
DEFAULT_PORT = 8765
KILLED = "killed"  # teardowns.review_state
DROP_STAGE = "review_queue"
MAX_BODY = 64 * 1024
LIGHT_EDIT = 0.3  # report: an approval whose every edited section changed less than this
GATE_PASS_SHARE, GATE_MINUTES = 0.8, 25.0  # Gate 5.3

# kill reason -> (the stage a fix belongs to, what it means)
KILL_REASONS: dict[str, tuple[str, str]] = {
    "wrong_gap": ("judge", "the gap named does not hold up on reading the pages"),
    "weak_evidence": ("judge", "the claims are true but too thin to send on"),
    "factual_error": ("extract/judge", "a fact or claim on the page is wrong"),
    "page_copy": ("drafter", "the page's words need more than light edits"),
    "email_copy": ("drafter", "the email needs more than light edits"),
    "tone": ("drafter", "reads as a grade for the company, not help for the hire"),
    "weak_slice": ("slices", "the proof slice is thin or off target"),
    "stale_role": ("signal", "the role looks closed or changed"),
    "other": ("-", "say what in the note"),
}
ICP_DROP_REASONS = ("too_large", "duplicate", "conflict", "staffing_or_agency", "consumer",
                    "out_of_scope")
ALL_REASONS = tuple(KILL_REASONS) + ICP_DROP_REASONS
_CID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ReviewError(RuntimeError):
    """A decision that cannot be taken. The message never carries a contact value."""


# -- the file and its edits ------------------------------------------------------------------


def file_sha(company_id: str, teardowns: Path | None = None) -> str | None:
    try:
        p = T.file_for(company_id, teardowns)
    except T.TeardownError:
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def last_kill(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reviews WHERE company_id = ? AND decision = 'kill' "
                        "ORDER BY review_id DESC LIMIT 1", (company_id,)).fetchone()


def kill_hold(conn: sqlite3.Connection, company_id: str, *,
              teardowns: Path | None = None) -> str | None:
    """Why approve must refuse: killed in review and the file unchanged since; else None."""
    k = last_kill(conn, company_id)
    if k is None or k["reason"] in ICP_DROP_REASONS:
        return None
    later = conn.execute("SELECT 1 FROM reviews WHERE company_id = ? AND decision = 'approve' "
                         "AND review_id > ?", (company_id, k["review_id"])).fetchone()
    if later is not None:
        return None
    if file_sha(company_id, teardowns) != k["file_sha256"]:
        return None
    return (f"killed in review ({k['reason']}) and the teardown file has not changed since; "
            "re-draft or edit it, render, then approve")


def _drafted_blocks(conn: sqlite3.Connection, company_id: str,
                    raw: dict[str, Any]) -> dict[str, Any] | None:
    """The sections as `te draft` wrote them, rebuilt from its cached output."""
    from engine.build import drafter as D
    from engine.llm import gateway

    mark = raw.get("drafted")
    if not isinstance(mark, dict) or not mark.get("key"):
        return None
    try:
        hit, output = gateway.cached_output(conn, D.STAGE, company_id, str(mark["key"]))
        if not hit or not isinstance(output, dict):
            return None
        return D.sections_from(T.load_context(conn, company_id), output)
    except Exception:  # an output the current code cannot rebuild: compare nothing
        return None


def _changed(a: Any, b: Any) -> float:
    def text(v: Any) -> str:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, indent=1)
    return round(1 - difflib.SequenceMatcher(None, text(a), text(b), autojunk=False).ratio(), 3)


def edits(conn: sqlite3.Connection, company_id: str, *,
          teardowns: Path | None = None) -> dict[str, float | None]:
    """Drafted section -> fraction of its text changed (None: not comparable)."""
    from engine.build import drafter as D

    try:
        p = T.file_for(company_id, teardowns)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (T.TeardownError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    mark = raw.get("drafted")
    if not isinstance(mark, dict) or not isinstance(mark.get("sections"), dict):
        return {}  # written by hand, not by te draft: no prompt to learn from
    changed = D.human_edits(raw)
    if not changed:
        return {}
    drafted = _drafted_blocks(conn, company_id, raw)
    out: dict[str, float | None] = {}
    for s in changed:
        out[s] = (_changed(drafted[s], raw.get(s))
                  if drafted is not None and s in drafted else None)
    return out


# -- decisions -------------------------------------------------------------------------------


@dataclass
class Decision:
    company_id: str
    outcome: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)


def _record(conn: sqlite3.Connection, company_id: str, decision: str, *,
            reason: str | None = None, note: str | None = None,
            edited: dict[str, float | None] | None = None, minutes: float | None = None,
            teardowns: Path | None = None) -> None:
    with conn:
        db.insert(conn, Review(company_id=company_id, decision=decision, reason=reason,
                               note=(note or "").strip() or None, edited=edited or {},
                               minutes=minutes, file_sha256=file_sha(company_id, teardowns),
                               version=REVIEW_VERSION))


def _minutes(conn: sqlite3.Connection, company_id: str, minutes: float | None) -> None:
    if minutes is not None and minutes > 0:
        Q.done(conn, company_id, minutes)


def approve(conn: sqlite3.Connection, company_id: str, *, minutes: float | None = None,
            note: str | None = None, teardowns: Path | None = None,
            sender_file: Path | None = None, site: Path | None = None) -> Decision:
    r = Q.approve(conn, company_id, teardowns=teardowns, sender_file=sender_file, site=site)
    if r.outcome != "approved":
        return Decision(company_id, r.outcome, r.detail, r.warnings)
    _record(conn, company_id, "approve", note=note, minutes=minutes, teardowns=teardowns,
            edited=edits(conn, company_id, teardowns=teardowns))
    _minutes(conn, company_id, minutes)
    return Decision(company_id, "approved", r.detail, r.warnings)


def kill(conn: sqlite3.Connection, company_id: str, reason: str, *, note: str | None = None,
         minutes: float | None = None, teardowns: Path | None = None) -> Decision:
    reason = (reason or "").strip()
    if reason not in ALL_REASONS:
        raise ReviewError(f"kill reason {reason!r}; one of {', '.join(ALL_REASONS)}")
    if reason == "other" and not (note or "").strip():
        raise ReviewError("a kill for 'other' needs a note saying why")
    c = Q.company_row(conn, company_id)
    st = Status(c["status"])
    if st not in (Status.BUILT, Status.APPROVED):
        raise ReviewError(f"status {st}; only a built or approved teardown is killed in review")
    lines: list[str] = []
    t1 = Q.touch_one(conn, company_id)
    if t1 is not None and t1["draft_id"]:
        lines.append("a Gmail draft exists for touch 1: delete it in Gmail")
    _record(conn, company_id, "kill", reason=reason, note=note, minutes=minutes,
            teardowns=teardowns, edited=edits(conn, company_id, teardowns=teardowns))
    _minutes(conn, company_id, minutes)
    if reason in ICP_DROP_REASONS:
        states.drop(company_id, reason, DROP_STAGE, conn)
        td = Q.teardown_row(conn, company_id)
        if td is not None:
            lines.append(f"delete site/{td['page_slug']}/ before the next deploy")
        return Decision(company_id, "dropped", f"{reason} (stage {DROP_STAGE})", lines)
    with conn:
        conn.execute("UPDATE teardowns SET review_state = ? WHERE company_id = ?",
                     (KILLED, company_id))
    stage = KILL_REASONS[reason][0]
    lines.append(f"fix in: {stage}; re-run that stage, then the teardown comes back to review")
    return Decision(company_id, "killed", reason, lines)


def mark_edited(conn: sqlite3.Connection, company_id: str, *, note: str | None = None,
                teardowns: Path | None = None) -> Decision:
    Q.company_row(conn, company_id)
    e = edits(conn, company_id, teardowns=teardowns)
    if not e:
        return Decision(company_id, "unchanged", "no drafted section differs from what te draft "
                                                 "wrote; nothing recorded")
    _record(conn, company_id, "edited", note=note, edited=e, teardowns=teardowns)
    return Decision(company_id, "edited", ", ".join(
        f"{s} ({'?' if v is None else f'{v:.0%}'})" for s, v in e.items()))


def rerender(conn: sqlite3.Connection, company_id: str, *, teardowns: Path | None = None,
             sender_file: Path | None = None, site: Path | None = None) -> Decision:
    r = R.render_company(conn, company_id, teardowns=teardowns, sender_file=sender_file,
                         site=site)
    return Decision(company_id, r.outcome, r.detail, [str(f) for f in r.failures])


# -- the report (Step 68) --------------------------------------------------------------------


def report(conn: sqlite3.Connection, *, pilot: bool = False,
           company_id: str | None = None) -> list[str]:
    sql = ("SELECT r.* FROM reviews r JOIN companies c ON c.company_id = r.company_id "
           "WHERE 1 = 1")
    args: list[Any] = []
    if pilot:
        sql += " AND c.pilot = 1"
    if company_id:
        sql += " AND r.company_id = ?"
        args.append(company_id)
    rows = conn.execute(sql + " ORDER BY r.review_id", args).fetchall()
    final: dict[str, sqlite3.Row] = {}
    latest: dict[str, sqlite3.Row] = {}
    for r in rows:
        latest[r["company_id"]] = r
        if r["decision"] in ("approve", "kill"):
            final[r["company_id"]] = r
    if not rows:
        return ["no review decisions recorded yet (te queue open)"]
    approved = [r for r in final.values() if r["decision"] == "approve"]
    killed = [r for r in final.values() if r["decision"] == "kill"]
    out = [f"decided: {len(final)} companies ({len(approved)} approved, {len(killed)} killed); "
           f"{len(latest) - len(final)} with edits recorded and no decision yet"]

    out.append("kill reasons:")
    by_reason: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in killed:
        by_reason[r["reason"]].append(r)
    if not by_reason:
        out.append("  (none)")
    for reason, rs in sorted(by_reason.items(), key=lambda x: (-len(x[1]), x[0])):
        stage = KILL_REASONS[reason][0] if reason in KILL_REASONS else "dropped (ICP)"
        out.append(f"  {reason}: {len(rs)}  fix in: {stage}")
        for r in rs:
            out.append(f"    {r['company_id']}" + (f": {r['note']}" if r["note"] else ""))

    out.append("most-edited sections (companies; mean fraction changed):")
    count: Counter[str] = Counter()
    fractions: dict[str, list[float]] = defaultdict(list)
    for r in latest.values():
        for s, v in json.loads(r["edited"]).items():
            count[s] += 1
            if v is not None:
                fractions[s].append(float(v))
    if not count:
        out.append("  (none)")
    for s, n in count.most_common():
        fr = fractions.get(s)
        mean = f"{sum(fr) / len(fr):.0%}" if fr else "n/a"
        out.append(f"  {s}: {n}; {mean}")

    mins = [float(r["minutes"]) for r in final.values() if r["minutes"] is not None]
    avg = sum(mins) / len(mins) if mins else None
    out.append(f"average review minutes: {f'{avg:.1f}' if avg is not None else 'n/a'} "
               f"({len(mins)} timed)")
    light = [r for r in approved
             if all(v is not None and v < LIGHT_EDIT for v in json.loads(r["edited"]).values())]
    share = len(light) / len(final) if final else 0.0
    out.append(f"Gate 5.3: {len(light)}/{len(final)} passed review with light edits (every "
               f"edited section under {LIGHT_EDIT:.0%} changed) = {share:.0%} (needs "
               f"{GATE_PASS_SHARE:.0%}); every kill has a reason: yes; average minutes "
               f"{f'{avg:.1f}' if avg is not None else 'n/a'} (needs {GATE_MINUTES:g} or less)")
    return out


# -- the pages -------------------------------------------------------------------------------


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def link(url: str | None, text: str | None = None) -> str:
    if not url or not R.is_http(url):
        return esc(text or url or "")
    return (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
            f"{esc(text or url)}</a>")


STYLE = """
body{margin:0;font:14px/1.45 system-ui,sans-serif;background:#fafafa;color:#1a1a1a}
.wrap{display:flex;height:100vh}
.left{flex:1 1 55%;border-right:1px solid #ccc}
.left iframe{width:100%;height:100%;border:0;background:#fff}
.right{flex:1 1 45%;overflow:auto;padding:12px 16px}
h1{font-size:18px;margin:4px 0}h2{font-size:15px;margin:18px 0 6px;border-bottom:1px solid #ddd}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #e2e2e2;padding:3px 5px;
vertical-align:top;text-align:left;font-size:13px}
pre{white-space:pre-wrap;background:#fff;border:1px solid #ddd;padding:8px}
.bad{color:#a00}.ok{color:#060}.muted{color:#666}
form{margin:6px 0;padding:6px;border:1px solid #ddd;background:#fff}
input[type=text]{width:60%}
"""


def _doc(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{esc(title)}</title>"
            f"<style>{STYLE}</style></head><body>{body}</body></html>").encode("utf-8")


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


class QueueApp:
    """The pages and the decisions, independent of the socket (tests call handle)."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], *, token: str | None = None,
                 port: int = 0, pilot: bool = False, company_id: str | None = None,
                 limit: int | None = None, teardowns: Path | None = None,
                 sender_file: Path | None = None, site: Path | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.connect = connect
        self.token = token or secrets.token_urlsafe(18)
        self.port = port
        self.pilot, self.company_id, self.limit = pilot, company_id, limit
        self.teardowns, self.sender_file, self.site = teardowns, sender_file, site
        self.clock = clock
        self.opened: dict[str, float] = {}

    @property
    def base(self) -> str:
        return f"/{self.token}/"

    def url(self) -> str:
        return f"http://{HOST}:{self.port}{self.base}"

    # -- selection

    def companies(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM companies WHERE status IN ('built', 'approved')")
        args: list[Any] = []
        if self.company_id:
            sql = "SELECT * FROM companies WHERE company_id = ?"
            args = [self.company_id]
        elif self.pilot:
            sql += " AND pilot = 1"
        rows = conn.execute(sql + " ORDER BY company_id", args).fetchall()
        return rows[:self.limit] if self.limit is not None else rows

    # -- dispatch

    def _host_ok(self, headers: Mapping[str, str]) -> bool:
        host = (headers.get("Host") or headers.get("host") or "").strip().lower()
        return host in {f"{HOST}:{self.port}", f"localhost:{self.port}"}

    def _origin_ok(self, headers: Mapping[str, str]) -> bool:
        origin = headers.get("Origin") or headers.get("origin")
        if origin is None:
            return True
        return origin.strip().lower() in {f"http://{HOST}:{self.port}",
                                          f"http://localhost:{self.port}"}

    def handle(self, method: str, path: str, headers: Mapping[str, str],
               body: bytes = b"") -> Response:
        parts = urlsplit(path)
        p = parts.path
        if not p.startswith(self.base) or not hmac.compare_digest(
                p[:len(self.base)].encode(), self.base.encode()):
            return Response(404, b"not found", "text/plain; charset=utf-8")
        if not self._host_ok(headers):
            return Response(403, b"wrong host", "text/plain; charset=utf-8")
        rest = p[len(self.base):]
        conn = self.connect()
        try:
            if method == "GET":
                return self._get(conn, rest)
            if method == "POST":
                return self._post(conn, rest, headers, body)
            return Response(405, b"method not allowed", "text/plain; charset=utf-8")
        finally:
            conn.close()

    def _get(self, conn: sqlite3.Connection, rest: str) -> Response:
        if rest == "":
            return Response(200, self.index(conn))
        m = re.fullmatch(r"c/([^/]+)", rest)
        if m and _CID.match(m.group(1)):
            cid = m.group(1)
            self.opened.setdefault(cid, self.clock())
            return Response(200, self.company_page(conn, cid))
        m = re.fullmatch(r"page/([^/]+)/(teardown\.css)?", rest)
        if m and _CID.match(m.group(1)):
            if m.group(2):
                css = (R.TEMPLATES / R.STYLESHEET).read_bytes()
                return Response(200, css, "text/css; charset=utf-8")
            return Response(200, self.rendered(conn, m.group(1)))
        return Response(404, b"not found", "text/plain; charset=utf-8")

    def _post(self, conn: sqlite3.Connection, rest: str, headers: Mapping[str, str],
              body: bytes) -> Response:
        if not self._origin_ok(headers):
            return Response(403, b"wrong origin", "text/plain; charset=utf-8")
        if len(body) > MAX_BODY:
            return Response(413, b"too large", "text/plain; charset=utf-8")
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"),
                                             keep_blank_values=True).items()}
        if not hmac.compare_digest(form.get("token", "").encode(), self.token.encode()):
            return Response(403, b"missing or wrong token", "text/plain; charset=utf-8")
        m = re.fullmatch(r"c/([^/]+)", rest)
        if not m or not _CID.match(m.group(1)):
            return Response(404, b"not found", "text/plain; charset=utf-8")
        cid = m.group(1)
        try:
            d = self.decide(conn, cid, form)
        except (ReviewError, Q.QueueError, states.TransitionError, ValueError) as e:
            d = Decision(cid, "refused", str(e))
        back = f'<p><a href="{esc(self.base)}c/{esc(cid)}">back to {esc(cid)}</a> · ' \
               f'<a href="{esc(self.base)}">the queue</a></p>'
        lines = "".join(f"<li>{esc(x)}</li>" for x in d.lines)
        page = (f"<div class=right><h1>{esc(cid)}: {esc(d.outcome)}</h1>"
                f"<p>{esc(d.detail)}</p><ul>{lines}</ul>{back}</div>")
        return Response(200 if d.outcome != "refused" else 409, _doc(f"{cid}: {d.outcome}", page))

    def decide(self, conn: sqlite3.Connection, cid: str, form: Mapping[str, str]) -> Decision:
        action = form.get("action", "")
        note = form.get("note") or None
        minutes: float | None = None
        typed = (form.get("minutes") or "").strip()
        if typed:
            try:
                minutes = float(typed)
            except ValueError as e:
                raise ReviewError(f"minutes {typed!r} is not a number") from e
            if not 0 < minutes <= 600:
                raise ReviewError("minutes must be more than 0 and at most 600")
        elif cid in self.opened and action in ("approve", "kill"):
            minutes = max(0.1, round((self.clock() - self.opened[cid]) / 60, 1))
        kw: dict[str, Any] = {"teardowns": self.teardowns}
        if action == "approve":
            d = approve(conn, cid, minutes=minutes, note=note, sender_file=self.sender_file,
                        site=self.site, **kw)
        elif action == "kill":
            d = kill(conn, cid, form.get("reason", ""), note=note, minutes=minutes, **kw)
        elif action == "edited":
            d = mark_edited(conn, cid, note=note, **kw)
        elif action == "render":
            d = rerender(conn, cid, sender_file=self.sender_file, site=self.site, **kw)
        else:
            raise ReviewError(f"unknown action {action!r}")
        if d.outcome in ("approved", "killed", "dropped"):
            self.opened.pop(cid, None)
            if minutes is not None:
                d.lines.append(f"{minutes:g} review minutes recorded")
        return d

    # -- the pages

    def index(self, conn: sqlite3.Connection) -> bytes:
        rows = []
        for c in self.companies(conn):
            cid = c["company_id"]
            td = Q.teardown_row(conn, cid)
            q = conn.execute("SELECT passed FROM quality_checks WHERE company_id = ?",
                             (cid,)).fetchone()
            last = conn.execute("SELECT decision, reason FROM reviews WHERE company_id = ? "
                                "ORDER BY review_id DESC LIMIT 1", (cid,)).fetchone()
            rows.append(
                f"<tr><td><a href=\"{esc(self.base)}c/{esc(cid)}\">{esc(cid)}</a></td>"
                f"<td>{esc(c['name'])}</td><td>{esc(c['status'])}</td>"
                f"<td>{esc(td['review_state'] if td is not None else '-')}</td>"
                f"<td>{'-' if q is None else ('pass' if q['passed'] else 'FAIL')}</td>"
                f"<td>{esc((last['decision'] + (' ' + last['reason'] if last['reason'] else '')) if last else '')}</td></tr>")
        body = ("<div class=right><h1>Review queue</h1><p class=muted>Local only; nothing is "
                "sent. Approve, kill with a reason, or say you edited.</p><table><tr><th>company"
                "</th><th>name</th><th>status</th><th>review</th><th>quality</th><th>last "
                "decision</th></tr>" + "".join(rows) + "</table></div>")
        return _doc("Review queue", body)

    def rendered(self, conn: sqlite3.Connection, cid: str) -> bytes:
        try:
            chk = T.check(conn, cid, directory=self.teardowns, sender_file=self.sender_file)
        except T.TeardownError as e:
            return _doc(cid, f"<p class=bad>{esc(e)}</p>")
        if chk.ok:
            return R.html(R.build_page(chk)).encode("utf-8")
        td = Q.teardown_row(conn, cid)
        if td is not None:
            page = R.site_dir(self.site) / td["page_slug"] / "index.html"
            if page.is_file():
                return page.read_bytes()
        items = "".join(f"<li>{esc(f)}</li>" for f in chk.failures)
        return _doc(cid, f"<p class=bad>The teardown check fails; nothing renders.</p><ul>{items}"
                         "</ul>")

    def company_page(self, conn: sqlite3.Connection, cid: str) -> bytes:
        try:
            c = Q.company_row(conn, cid)
        except Q.QueueError as e:
            return _doc(cid, f"<p class=bad>{esc(e)}</p>")
        try:
            ctx: T.Context | None = T.load_context(conn, cid)
        except T.TeardownError:
            ctx = None
        parts = [f"<h1>{esc(c['name'])} <span class=muted>({esc(cid)})</span></h1>",
                 f"<p><a href=\"{esc(self.base)}\">the queue</a> · status {esc(c['status'])}"]
        td = Q.teardown_row(conn, cid)
        if td is not None:
            parts.append(f" · review {esc(td['review_state'])}")
        if ctx is not None:
            parts.append(f" · {esc(ctx.offer_type or '-')} · role {esc(ctx.role or '-')}")
        parts.append("</p>")
        parts += self._forms(cid)
        parts += self._checks(conn, cid, td)
        parts += self._email(conn, cid)
        if ctx is not None:
            parts += self._evidence(ctx)
        parts += self._facts(conn, cid)
        if ctx is not None:
            parts += self._links(ctx)
        parts += self._history(conn, cid)
        left = (f"<div class=left><iframe sandbox=\"allow-same-origin\" title=\"the page\" "
                f"src=\"{esc(self.base)}page/{esc(cid)}/\"></iframe></div>")
        return _doc(f"Review {cid}", f"<div class=wrap>{left}<div class=right>"
                                     f"{''.join(parts)}</div></div>")

    def _forms(self, cid: str) -> list[str]:
        tok = f'<input type=hidden name=token value="{esc(self.token)}">'
        act = f'action="{esc(self.base)}c/{esc(cid)}" method=post'
        mins = 'minutes <input name=minutes size=4 placeholder="auto">'
        opts = "".join(f"<option value=\"{esc(r)}\">{esc(r)}: {esc(KILL_REASONS[r][1])}</option>"
                       for r in KILL_REASONS)
        opts += "".join(f"<option value=\"{esc(r)}\">{esc(r)} (ICP: drop the company)</option>"
                        for r in ICP_DROP_REASONS)
        return [
            "<h2>Decide</h2>",
            f"<form {act}>{tok}<input type=hidden name=action value=approve>"
            f"{mins} note <input type=text name=note> <button>Approve</button></form>",
            f"<form {act}>{tok}<input type=hidden name=action value=kill>"
            f"reason <select name=reason>{opts}</select><br>{mins} note "
            f"<input type=text name=note> <button>Kill</button></form>",
            f"<form {act}>{tok}<input type=hidden name=action value=edited>"
            f"note <input type=text name=note> <button>I edited the file</button></form>",
            f"<form {act}>{tok}<input type=hidden name=action value=render>"
            f"<button>Re-render the page</button></form>",
        ]

    def _checks(self, conn: sqlite3.Connection, cid: str, td: sqlite3.Row | None) -> list[str]:
        from engine.build import quality as QL

        out = ["<h2>Quality gates</h2>"]
        q = QL.stored(conn, cid)
        if q is None:
            out.append("<p class=muted>not run (te quality --company " + esc(cid) + ")</p>")
        else:
            stale = q["file_sha256"] != file_sha(cid, self.teardowns)
            out.append(f"<p class={'ok' if q['passed'] else 'bad'}>"
                       f"{'passed' if q['passed'] else 'failed'} ({esc(q['version'])}, "
                       f"{esc(q['checked_at'])}){' — stale: the file changed since' if stale else ''}"
                       "</p>")
            items = "".join(f"<li><b>{esc(r.get('gate'))}</b>: {esc(r.get('message'))}</li>"
                            for r in json.loads(q["reasons"]))
            if items:
                out.append(f"<ul>{items}</ul>")
        try:
            chk = T.check(conn, cid, directory=self.teardowns, sender_file=self.sender_file)
        except T.TeardownError as e:
            out.append(f"<p class=bad>te teardown check: {esc(e)}</p>")
            return out
        if chk.ok:
            out.append("<p class=ok>te teardown check passes</p>")
            if td is not None:
                page = R.site_dir(self.site) / td["page_slug"] / "index.html"
                pp = Q.page_problem(chk, page)
                if pp:
                    out.append(f"<p class=bad>{esc(pp)}</p>")
        else:
            out.append("<p class=bad>te teardown check fails:</p><ul>" + "".join(
                f"<li>{esc(f)}</li>" for f in chk.failures) + "</ul>")
        hold = kill_hold(conn, cid, teardowns=self.teardowns)
        if hold:
            out.append(f"<p class=bad>{esc(hold)}</p>")
        e = edits(conn, cid, teardowns=self.teardowns)
        if e:
            out.append("<p>edited since te draft: " + esc(", ".join(
                f"{s} ({'?' if v is None else f'{v:.0%}'})" for s, v in e.items())) + "</p>")
        return out

    def _email(self, conn: sqlite3.Connection, cid: str) -> list[str]:
        out = ["<h2>The email ({first_name} unfilled)</h2>"]
        try:
            r = Q.prepare(conn, cid, teardowns=self.teardowns, sender_file=self.sender_file,
                          site=self.site, need_link=False)
        except (Q.QueueError, T.TeardownError) as e:
            out.append(f"<pre class=bad>{esc(e)}</pre>")
            return out
        assert r.msg is not None
        out.append(f"<pre>{esc(chr(10).join(Q.preview(r.msg)))}</pre>")
        out += [f"<p class=muted>{esc(w)}</p>" for w in r.warnings]
        return out

    def _evidence(self, ctx: T.Context) -> list[str]:
        d = ctx.diagnosis
        out = ["<h2>Diagnosis</h2>"]
        if d is None:
            return out + ["<p class=bad>no diagnosis at the judge's current prompt version</p>"]
        out.append(f"<p>gap {esc(d['gap_type'])} · confidence {esc(d['confidence'])} · "
                   f"secondary {esc(', '.join(json.loads(d['secondary'] or '[]')) or '-')} · "
                   f"verifier {esc(ctx.verdict.outcome if ctx.verdict else 'not run')}</p>"
                   f"<p><b>need</b>: {esc(d['need'])}<br><b>cannot see</b>: "
                   f"{esc(d['cannot_see'])}</p>")
        verdicts = {v.index: v for v in (ctx.verdict.claims if ctx.verdict else [])}
        rows = []
        for i, cl in enumerate(ctx.claims):
            prob = T.claim_problem(ctx, i)
            v = verdicts.get(i)
            quote = f"“{esc(cl.quote)}”" if cl.found else "<i>not found</i>"
            rows.append(f"<tr><td>[{i}]</td><td>{esc(cl.artifact_kind)}</td><td>{esc(cl.text)}"
                        f"<br>{quote}<br>{link(cl.url)}</td><td class={'ok' if prob is None else 'bad'}>"
                        f"{'passes' if prob is None else esc(prob)}"
                        f"{'<br>' + esc(v.verdict) if v else ''}</td></tr>")
        out.append("<h2>Evidence: the claims</h2><table><tr><th>#</th><th>kind</th><th>claim, "
                   "quote, source</th><th>check</th></tr>" + "".join(rows) + "</table>")
        return out

    def _facts(self, conn: sqlite3.Connection, cid: str) -> list[str]:
        rows = conn.execute("SELECT fact, value, artifact_kind, quote, found FROM facts WHERE "
                            "company_id = ? ORDER BY fact, fact_id LIMIT 300", (cid,)).fetchall()
        if not rows:
            return ["<h2>Facts</h2><p class=muted>none</p>"]
        body = "".join(f"<tr><td>{esc(r['fact'])}</td><td>{esc(r['value'])}</td>"
                       f"<td>{esc(r['artifact_kind'])}</td><td>"
                       f"{esc(r['quote']) if r['found'] else '<i>not found</i>'}</td></tr>"
                       for r in rows)
        return ["<h2>Facts</h2><table><tr><th>fact</th><th>value</th><th>kind</th><th>quote"
                "</th></tr>" + body + "</table>"]

    def _links(self, ctx: T.Context) -> list[str]:
        rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(a['outcome'])}</td>"
                       f"<td>{esc(str(a['fetched_at'])[:10])}</td><td>{link(a['url'])}</td></tr>"
                       for k, a in sorted(ctx.arts.items()))
        return ["<h2>Live links (opens the company's own pages)</h2><table><tr><th>kind</th>"
                "<th>capture</th><th>date</th><th>url</th></tr>" + rows + "</table>"]

    def _history(self, conn: sqlite3.Connection, cid: str) -> list[str]:
        rows = conn.execute("SELECT * FROM reviews WHERE company_id = ? ORDER BY review_id",
                            (cid,)).fetchall()
        if not rows:
            return []
        body = "".join(f"<li>{esc(r['decided_at'])} {esc(r['decision'])}"
                       f"{' ' + esc(r['reason']) if r['reason'] else ''}"
                       f"{' — ' + esc(r['note']) if r['note'] else ''}"
                       f"{' (' + esc(r['minutes']) + ' min)' if r['minutes'] else ''}</li>"
                       for r in rows)
        return [f"<h2>Decisions so far</h2><ul>{body}</ul>"]


# -- the server ------------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": ("default-src 'none'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self'; frame-src 'self'; form-action 'self'; "
                                "frame-ancestors 'self'; base-uri 'none'"),
}


def make_handler(app: QueueApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "te-queue"
        sys_version = ""

        def _send(self, r: Response) -> None:
            self.send_response(r.status)
            self.send_header("Content-Type", r.content_type)
            self.send_header("Content-Length", str(len(r.body)))
            for k, v in {**SECURITY_HEADERS, **r.headers}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(r.body)

        def do_GET(self) -> None:  # noqa: N802
            self._send(app.handle("GET", self.path, dict(self.headers.items())))

        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._send(Response(413, b"too large", "text/plain; charset=utf-8"))
                return
            body = self.rfile.read(n) if n > 0 else b""
            self._send(app.handle("POST", self.path, dict(self.headers.items()), body))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # paths carry the token; nothing is logged

    return Handler


class LocalServer(HTTPServer):
    """HTTPServer without the reverse lookup of its own address (getfqdn)."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, int(self.server_address[1])


def make_server(app: QueueApp, port: int = DEFAULT_PORT) -> HTTPServer:
    """A server on 127.0.0.1 only; app.port is set to the bound port."""
    httpd = LocalServer((HOST, port), make_handler(app))
    app.port = int(httpd.server_address[1])
    return httpd
