"""The automated quality gates (docs/PLAN.md Step 65; protocol/TEARDOWN_SPEC.md
v1.2 "Quality gates"; protocol/GAPS.md v1.2 "Checks after the judge").

    te quality <company_id> | --company <id> | --pilot [--limit n] [--dry-run]

Fails a teardown before a person reviews it. Each failure is a reason
{gate, message}; the gates:

  every `te teardown check` rule  each failure under its rule's own name
                    (teardown.RULES: untraced, word_limit, total_words,
                    banned_phrase, superlative, company_name, competitor,
                    evidence, slice_row, contact_value, header, ...). That is
                    where these Step 65 conditions are decided: a section 3
                    sentence with no passing claim (untraced), a word limit or a
                    banned phrase (word_limit, total_words, banned_phrase), the
                    company name missing from the body (company_name), an
                    unconfirmed competitor (competitor).
  verifier          the verifier has not run, is stale, abstained, or its
                    outcome is not pass: one not_supported claim (outcome
                    review) fails the teardown here (GAPS.md), cited or not.
  artifact_kinds    the passing claims the page and email cite span fewer than
                    two artifact kinds.
  evidence_source   an evidence line with no source link, a quoted line whose
                    page is no longer captured ok, a "Not found" line resting on
                    a page that was blocked, dead or robots_denied (not seen,
                    so not known to be absent), or a slice row with no source.
  offer_type        the signal's offer type is not candidate | adjacent, the
                    file's offer type (which picks section 8's offer and the
                    email variant) differs from it, or an adjacent teardown
                    says the sender applied.
  slice             no proof slice selected, the selected slice failed, the
                    company is held as slice_thin, or a slice row on the page
                    cites a source the stored slice does not hold.
  email             no email block, or the email fails the spec's checks
                    (outreach/draft.py message_problems: under 120 words, one
                    link, banned phrases, superlatives, one-sentence
                    observation tracing to passing claims, the subject).

Each result is recorded in quality_checks (0017): passed, the reasons as JSON
and the sha256 of the file read, one row per company, rewritten only when it
changes. The review queue (Step 66) shows the reasons. --dry-run records
nothing.

Zero model calls and no network: everything here reads the database and the
files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from engine.build import render as R
from engine.build import sender as S
from engine.build import teardown as T
from engine.build.slices import store as SS
from engine.models import QualityReason

QUALITY_VERSION = "q-1.0"
GATES = ("verifier", "artifact_kinds", "evidence_source", "offer_type", "slice", "email")
UNSEEN = ("blocked", "dead", "robots_denied")
PLACEHOLDER_LINK = "https://teardown.invalid/{slug}/"
_APPLIED = re.compile(r"\bI\s+(?:have\s+|'ve\s+)?applied\b", re.IGNORECASE)
_SOURCE = re.compile(r"^(?:https?://|dns:)\S+$")


@dataclass
class Result:
    company_id: str
    reasons: list[QualityReason] = field(default_factory=list)
    file_sha256: str | None = None
    recorded: str = ""  # written | unchanged | dry_run

    @property
    def passed(self) -> bool:
        return not self.reasons

    @property
    def gates(self) -> set[str]:
        return {r.gate for r in self.reasons}


# -- the gates -----------------------------------------------------------------------------


def verifier_reasons(ctx: T.Context) -> list[str]:
    d, v = ctx.diagnosis, ctx.verdict
    if d is None or not d["attempted"]:
        return []  # the check's diagnosis rule says so
    if v is None:
        return ["the verifier has not run on this diagnosis (te judge verify)"]
    if v.key != ctx.key:
        return ["the verifier's verdict is for older claims; re-run te judge verify"]
    if v.outcome != "pass" or v.not_supported or any(c.verdict == "not_supported"
                                                     for c in v.claims):
        bad = sorted(c.index for c in v.claims if c.verdict == "not_supported")
        which = f" (claim {', '.join(f'[{i}]' for i in bad)})" if bad else ""
        return [f"the verifier's outcome is {v.outcome}: {v.not_supported} claim(s) "
                f"not_supported{which}; one not_supported fails the teardown before review"]
    return []


def cited(doc: T.TeardownDoc) -> list[int]:
    out = [i for s in doc.need.sentences for i in s.claims] + list(doc.need.evidence)
    if doc.email is not None:
        out += doc.email.claims
    return list(dict.fromkeys(out))


def kinds_reasons(doc: T.TeardownDoc, ctx: T.Context) -> list[str]:
    ok = [i for i in cited(doc) if T.claim_problem(ctx, i) is None]
    kinds = sorted({ctx.claims[i].artifact_kind for i in ok})
    if len(kinds) < T.MIN_KINDS:
        return [f"the passing claims cited span {len(kinds)} artifact kind(s) "
                f"({', '.join(kinds) or 'none'}); at least {T.MIN_KINDS}"]
    return []


def source_reasons(doc: T.TeardownDoc, ctx: T.Context) -> list[str]:
    out = []
    for i in doc.need.evidence:
        if not 0 <= i < len(ctx.claims):
            continue  # the check's evidence rule names it
        c = ctx.claims[i]
        if not c.url or not _SOURCE.match(c.url.strip()):
            out.append(f"evidence claim [{i}] has no source link")
        a = ctx.arts.get(c.artifact_kind)
        now = a["outcome"] if a is not None else "not captured"
        if c.found:
            if not (c.quote or "").strip():
                out.append(f"evidence claim [{i}] is a found line with no quote")
            if now != "ok":
                out.append(f"evidence claim [{i}] quotes the {c.artifact_kind}, which is "
                           f"now {now}")
        elif now in UNSEEN:
            out.append(f"evidence claim [{i}]: a Not found line rests on the "
                       f"{c.artifact_kind}, which was {now}: not seen, so not known absent")
        elif now != "not_found":
            out.append(f"evidence claim [{i}]: a Not found line rests on the "
                       f"{c.artifact_kind}, whose capture is {now}, not not_found")
    for k, r in enumerate(doc.slice.rows, start=1):
        if not r.url or not _SOURCE.match(r.url.strip()):
            out.append(f"slice row {k} has no source link")
    return out


def _drafted_text(doc: T.TeardownDoc) -> list[str]:
    out = [*(s.text for s in doc.need.sentences), doc.why_now, *doc.build,
           doc.might_have_wrong, doc.slice.title]
    if doc.email is not None:
        out += [doc.email.observation, doc.email.slice, doc.email.subject or ""]
    return out


def offer_reasons(doc: T.TeardownDoc, ctx: T.Context) -> list[str]:
    want, have = ctx.offer_type, doc.header.offer_type
    if want not in ("candidate", "adjacent"):
        return [f"the signal's offer type is {want!r}; it must be candidate or adjacent "
                "(protocol/OFFER.md) before section 8 and the email can be checked"]
    out = []
    if have != want:
        out.append(f"the file's offer type {have!r} (section 8 and the email variant) does not "
                   f"match the signal's {want!r}")
    if want == "adjacent" and any(_APPLIED.search(t) for t in _drafted_text(doc)):
        out.append("an adjacent teardown says the sender applied; that is the candidate offer")
    return out


def slice_reasons(conn: sqlite3.Connection, doc: T.TeardownDoc, ctx: T.Context) -> list[str]:
    out = []
    held = conn.execute("SELECT review_reason FROM companies WHERE company_id = ?",
                        (ctx.company_id,)).fetchone()
    if held is not None and (held["review_reason"] or "").startswith(f"{SS.HOLD_CODE}:"):
        out.append(f"held for review: {held['review_reason']}")
    st = ctx.slice
    if st is None:
        failed = [s for s in SS.slices(conn, ctx.company_id) if not s.ok]
        why = "; ".join(f"{s.kind} failed: {s.reason}" for s in failed)
        out.append("no proof slice selected" + (f" ({why})" if why else "; run te slice build"))
        return out
    if not st.ok:
        out.append(f"the selected slice {st.kind} failed: {st.reason}")
    if not st.rows:
        out.append(f"the selected slice {st.kind} has no rows")
    stored = {(r.get("url"), r.get("quote")) for r in st.rows}
    for k, r in enumerate(doc.slice.rows, start=1):
        if (r.url, r.quote) not in stored:
            out.append(f"slice row {k} cites a source and quote the stored {st.kind} slice "
                       "does not hold; never invent a source")
    return out


def email_reasons(conn: sqlite3.Connection, doc: T.TeardownDoc, ctx: T.Context,
                  snd: S.Sender | None) -> list[str]:
    from engine.outreach import draft as D

    if doc.email is None:
        return ["the teardown file has no email block (te draft writes it)"]
    if snd is None or doc.header.offer_type not in D.TEMPLATES:
        return []  # the sender and offer_type gates say why
    slug = R.stored_slug(conn, ctx.company_id)
    link = (R.future_url(slug) if slug else None) or PLACEHOLDER_LINK.format(
        slug=slug or ctx.company_id)
    try:
        msg = D.fill(doc.header.offer_type, doc, snd, link)
    except D.QueueError as e:
        return [str(e)]
    return D.message_problems(msg, doc, ctx)


# -- one company ---------------------------------------------------------------------------


def evaluate(conn: sqlite3.Connection, company_id: str, *, directory: Path | None = None,
             sender_file: Path | None = None) -> Result:
    path = T.file_for(company_id, directory)
    res = Result(company_id)
    if path.exists():
        res.file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def add(gate: str, messages: list[str]) -> None:
        for m in messages:
            res.reasons.append(QualityReason(gate=gate, message=" ".join(m.split())[:500]))

    chk = T.check(conn, company_id, directory=directory, sender_file=sender_file)
    for f in chk.failures:
        add(f.rule, [f.message])
    doc, ctx = chk.doc, chk.ctx
    if doc is None or ctx is None:
        return res  # the structure failure says why
    add("verifier", verifier_reasons(ctx))
    add("artifact_kinds", kinds_reasons(doc, ctx))
    add("evidence_source", source_reasons(doc, ctx))
    add("offer_type", offer_reasons(doc, ctx))
    add("slice", slice_reasons(conn, doc, ctx))
    add("email", email_reasons(conn, doc, ctx, chk.sender))
    return res


def record(conn: sqlite3.Connection, res: Result) -> str:
    """Upsert quality_checks; 'unchanged' when the same result is already there."""
    reasons = json.dumps([r.model_dump() for r in res.reasons], ensure_ascii=False)
    old = conn.execute("SELECT * FROM quality_checks WHERE company_id = ?",
                       (res.company_id,)).fetchone()
    if old is not None and (old["version"], bool(old["passed"]), old["reasons"],
                            old["file_sha256"]) == (QUALITY_VERSION, res.passed, reasons,
                                                    res.file_sha256):
        return "unchanged"
    with conn:
        conn.execute(
            "INSERT INTO quality_checks (company_id, version, passed, reasons, file_sha256, "
            "checked_at) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "ON CONFLICT(company_id) DO UPDATE SET version = excluded.version, passed = "
            "excluded.passed, reasons = excluded.reasons, file_sha256 = excluded.file_sha256, "
            "checked_at = excluded.checked_at",
            (res.company_id, QUALITY_VERSION, int(res.passed), reasons, res.file_sha256))
    return "written"


def run_company(conn: sqlite3.Connection, company_id: str, *, directory: Path | None = None,
                sender_file: Path | None = None, dry_run: bool = False) -> Result:
    res = evaluate(conn, company_id, directory=directory, sender_file=sender_file)
    res.recorded = "dry_run" if dry_run else record(conn, res)
    return res


def stored(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM quality_checks WHERE company_id = ?",
                        (company_id,)).fetchone()


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None, directory: Path | None = None) -> list[str]:
    """One company as asked, or every company (pilot = 1 with --pilot), not
    dropped, that has a teardown file."""
    if company_id is not None:
        return [company_id]
    ids = [i for i in R.select(conn, pilot=pilot) if T.file_for(i, directory).exists()]
    return ids[:limit] if limit is not None else ids
