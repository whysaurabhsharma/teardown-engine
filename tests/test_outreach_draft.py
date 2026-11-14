"""Step 54 (te queue draft), Step 55 (te queue done), Steps 69-72 (approve, the
send-day recheck, opt-outs) and the Gmail drafts-only client.

Invented companies and people only: Quorvane from tests/test_teardown.py, with
an invented recipient, Ilsa Marrow <ilsa@velmora.example>. The Gmail service is
fully faked (no Google network); the send-day recheck runs on a fake Checks
object (no board, no page is fetched); sockets are blocked by the fixtures.
"""

from __future__ import annotations

import asyncio
import base64
import email
import json
import re
from datetime import UTC, date, datetime, timedelta
from email import policy
from pathlib import Path
from typing import Any

import socket

import pytest
from click.testing import CliRunner

from engine import db
from engine.build import render as R
from engine.cli import main
from engine.extract.base import Art
from engine.guard import gmail
from engine.judge import faithfulness as F
from engine.models import Contact, Signal, Suppression
from engine.outreach import draft as Q
from engine.signal import ats
from tests.test_extract_facts import QID, Q_DOMAIN, bundles, cache  # noqa: F401 (fixtures)
from tests.test_judge import (  # noqa: F401 (fixtures)
    PRICING,
    PRICING_Q,
    extracted,
    no_net,
    split,
)
from tests.test_judge_verify import judged, providers  # noqa: F401 (fixtures)
from tests.test_teardown import (  # noqa: F401 (fixtures)
    _fresh_model_budget,
    dirs,
    load_doc,
    ready,
    save_doc,
    verified,
)

_REAL_CONNECT = socket.socket.connect  # taken before any fixture blocks the network
ROOT = Path(__file__).resolve().parents[1]
BASE = "https://notes.example"
TODAY = date(2026, 11, 17)
BOARD = "quorvane"
SIG1, SIG2 = "gh-quorvane-1", "gh-quorvane-2"
NAME, ADDRESS = "Ilsa Marrow", "ilsa@velmora.example"
OBSERVATION = ("The Quorvane job post asks the first sales hire to set up HubSpot, and no "
               "product page is linked from the site.")
SLICE = "a list of ten dental chains in Pune"
SUBJECT_OBS = "HubSpot before outbound"


# -- fakes ---------------------------------------------------------------------------------


class FakeGmail:
    """users().drafts().create(...).execute(); records each raw message. It has no
    send method, and nothing else."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def users(self) -> FakeGmail:
        return self

    def drafts(self) -> FakeGmail:
        return self

    def create(self, userId: str, body: dict[str, Any]) -> FakeGmail:  # noqa: N803
        self.created.append({"userId": userId, "body": body})
        return self

    def execute(self) -> dict[str, Any]:
        return {"id": f"r-draft-{len(self.created)}", "message": {"id": "m1"}}

    def message(self, i: int = -1) -> email.message.EmailMessage:
        raw = self.created[i]["body"]["message"]["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


@pytest.fixture
def fake_gmail(monkeypatch: pytest.MonkeyPatch) -> FakeGmail:
    svc = FakeGmail()

    def no_file(*a, **kw):
        raise AssertionError("a Gmail credential file was read")

    monkeypatch.setattr(gmail, "_credentials", lambda client, token: object())
    monkeypatch.setattr(gmail, "_build_service", lambda creds: svc)
    monkeypatch.setattr(gmail, "_token_scopes_ok", no_file)
    return svc


class FakeChecks:
    """The send-day recheck without a network: a board with these jobs open, and
    the stored artifacts as the fresh ones unless replaced."""

    def __init__(self, conn, cache, *, open_ids=(SIG1,), outcome="ok",  # noqa: F811
                 replace: dict[str, Art] | None = None, posted_days_ago: int = 3) -> None:
        self.conn, self.cache, self.posted_days_ago = conn, cache, posted_days_ago
        self.open_ids, self.outcome, self.replace = list(open_ids), outcome, replace or {}
        self.polled: list[tuple[str, str]] = []
        self.fetched: list[list[str]] = []
        self.closed = False

    async def poll_board(self, source: str, slug: str) -> ats.BoardResult:
        self.polled.append((source, slug))
        sigs = [Signal(signal_id=i, source=source, board_slug=slug, company_name_raw="Quorvane",
                       title="x", first_seen=TODAY,
                       posted_at=TODAY - timedelta(days=self.posted_days_ago))
                for i in self.open_ids] if self.outcome == "ok" else []
        return ats.BoardResult(source, slug, f"https://boards.example/{slug}", self.outcome,
                               200 if self.outcome == "ok" else 403, sigs)

    async def fresh_arts(self, conn, company_id, kinds):  # noqa: F811
        self.fetched.append(list(kinds))
        arts = F.load_arts(conn, self.cache, company_id)
        out = {k: arts[k] for k in kinds if k in arts}
        out.update(self.replace)
        return out

    async def aclose(self) -> None:
        self.closed = True


# -- fixtures ------------------------------------------------------------------------------


EMAIL_BLOCK = {"observation": OBSERVATION, "claims": [0, 2], "slice": SLICE,
               "subject": SUBJECT_OBS}


def add_contact(conn, *, channel: str = "email", email_addr: str | None = ADDRESS) -> None:
    with conn:
        db.insert(conn, Contact(company_id=QID, name=NAME, title="Head of Growth",
                                source_url=f"https://{Q_DOMAIN}/team", email=email_addr,
                                email_confidence=0.9 if email_addr else None,
                                confidence="high" if email_addr else None,
                                email_source="manual" if email_addr else None,
                                linkedin_url="https://www.linkedin.com/in/ilsa-marrow-x"
                                if channel == "linkedin" else None, channel=channel))


@pytest.fixture
def loopback_only(no_net, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """asyncio on Windows opens a loopback socket pair for its event loop; allow
    127.0.0.1 and nothing else."""
    def connect(self, address):
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return _REAL_CONNECT(self, address)
        raise AssertionError("the queue touched the network")

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def built(conn, ready, loopback_only, monkeypatch) -> dict[str, Path]:
    """Quorvane rendered (status built) with an email block, a board signal and a
    recipient; TEARDOWN_BASE_URL set."""
    doc = load_doc(ready)
    doc["email"] = dict(EMAIL_BLOCK)
    save_doc(ready, doc)
    with conn:
        conn.execute("UPDATE signals SET board_slug = ?, role_family = 'sdr', fresh = 1, "
                     "posted_at = ? WHERE signal_id = ?",
                     (BOARD, (TODAY - timedelta(days=10)).isoformat(), SIG1))
    add_contact(conn)
    monkeypatch.setenv("TEARDOWN_BASE_URL", BASE)
    r = R.render_company(conn, QID, teardowns=ready["td"], sender_file=ready["sender"],
                         site=ready["site"])
    assert r.outcome == "rendered", r
    return ready


@pytest.fixture
def approved(conn, built) -> dict[str, Path]:
    r = approve(conn, built)
    assert r.outcome == "approved", r
    return built


def approve(conn, d, **kw) -> Q.Result:
    return Q.approve(conn, QID, teardowns=d["td"], sender_file=d["sender"], site=d["site"], **kw)


def draft(conn, cache, d, checks=None, *, drafter=None, **kw) -> Q.Result:  # noqa: F811
    drafter = drafter if drafter is not None else gmail.create_draft
    return asyncio.run(Q.draft_company(conn, QID, checks=checks, cache=cache, drafter=drafter,
                                       teardowns=d["td"], sender_file=d["sender"],
                                       site=d["site"], today=TODAY, **kw))


def status(conn) -> str:
    return conn.execute("SELECT status FROM companies WHERE company_id = ?", (QID,)).fetchone()[0]


def review_state(conn) -> str:
    return conn.execute("SELECT review_state FROM teardowns WHERE company_id = ?",
                        (QID,)).fetchone()[0]


def touches(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM touches WHERE company_id = ?", (QID,))]


def dbf(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def no_contact_values(text: str) -> None:
    for v in (NAME, "Ilsa", "Marrow", ADDRESS, "velmora", "Head of Growth", "ilsa-marrow"):
        assert v.lower() not in text.lower(), v


# -- the templates -------------------------------------------------------------------------


def spec_variants() -> list[tuple[str, str]]:
    spec = (ROOT / "protocol" / "TEARDOWN_SPEC.md").read_text(encoding="utf-8")
    assert f"Version {Q.SPEC_VERSION} " in spec
    part = spec[spec.index("## The email, two variants"):spec.index("## Follow-ups")]
    blocks = re.findall(r"```\n(.*?)\n```", part, re.DOTALL)
    out = []
    for b in blocks:
        head, body = b.split("\n\n", 1)
        assert head.startswith("Subject: ")
        out.append((head[len("Subject: "):], body))
    return out


def test_templates_are_the_spec_verbatim() -> None:
    assert spec_variants() == [(Q.CANDIDATE_SUBJECT, Q.CANDIDATE_BODY),
                               (Q.ADJACENT_SUBJECT, Q.ADJACENT_BODY)]


def test_adjacent_variant_fills(conn, built) -> None:
    r = Q.prepare(conn, QID, teardowns=built["td"], sender_file=built["sender"],
                  site=built["site"])
    m = r.msg
    link = f"{BASE}/{r.slug}/"
    assert m.variant == "adjacent" and m.link == link
    assert m.subject == ("Quorvane's Founding Sales Development Representative hire: "
                         "HubSpot before outbound")
    body = Q.fill_name(m, "Ilsa")
    assert body == (
        "Hi Ilsa,\n\n"
        "I saw Quorvane is hiring a Founding Sales Development Representative. "
        f"{OBSERVATION}\n\n"
        f"I wrote up what I found and built {SLICE} for you: {link}\n\n"
        "I build outbound and enrichment systems. I am looking for a GTM engineer role and am "
        "also open to a short project. If the notes are useful, could we talk for 15 minutes?"
        "\n\nAvery Example\n"
        "I build outbound and enrichment systems. · 12 Example Road, Pune 411001\n"
        'Not relevant? Reply "no" and I will not write again.')
    assert Q.count_words(body) < Q.WORD_LIMIT
    assert Q.links_in(m.subject + body) == [link]
    assert Q.message_problems(m, r.doc, r.ctx, body) == []


def test_candidate_variant_fills(conn, built) -> None:
    doc = load_doc(built)
    doc["header"]["offer_type"] = "candidate"
    doc["email"].pop("subject")
    save_doc(built, doc)
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'candidate' WHERE signal_id = ?", (SIG1,))
    from engine.build import teardown as T

    chk = T.check(conn, QID, directory=built["td"], sender_file=built["sender"])
    assert chk.ok, chk.failures
    m = Q.fill("candidate", chk.doc, chk.sender, f"{BASE}/x/")
    assert m.subject == "Applied for Founding Sales Development Representative at Quorvane, " \
                        "with notes"
    body = Q.fill_name(m, "Ilsa")
    assert body.startswith(
        "Hi Ilsa,\n\nI applied for the Founding Sales Development Representative role at "
        "Quorvane today. Alongside the CV, I looked at what that hire will need on day one. "
        f"{OBSERVATION}\n\nI wrote it up and built {SLICE} for you: {BASE}/x/\n\n"
        "If it is useful, I would value 15 minutes. If I have misread something, I would like "
        "to hear that too.\n\nAvery Example\n")
    assert body.endswith('Not relevant? Reply "no" and I will not write again.')
    assert Q.message_problems(m, chk.doc, chk.ctx, body) == []
    assert Q.count_words(body) < Q.WORD_LIMIT


def test_article_before_the_role() -> None:
    assert [Q.article(w) for w in ("SDR", "Account Executive", "Founding AE", "BDR",
                                   "Head of Sales")] == ["an", "an", "a", "a", "a"]


@pytest.mark.parametrize("change, problem", [
    (dict(observation="Quorvane " + "word " * 60 + "ends."), "words; it must be under 120"),
    (dict(observation="Quorvane links https://quorvane.example/pricing from the nav."),
     "2 links"),
    (dict(observation="Quorvane could leverage its pricing page for the hire."),
     '"leverage" as a verb'),
    (dict(observation="Quick question about the Quorvane pricing page."), "'quick question'"),
    (dict(observation="Quorvane hires. The pricing page hides the price."), "one sentence"),
    (dict(claims=[]), "names no claim"),
    (dict(claims=[7]), "no claim [7]"),
    (dict(subject="HubSpot"), "three-word observation"),
    (dict(observation="Quorvane sells the best tool."), "superlative 'best'"),
])
def test_email_problems_refuse_approval(conn, built, change, problem) -> None:
    doc = load_doc(built)
    doc["email"].update(change)
    save_doc(built, doc)
    r = approve(conn, built)
    assert r.outcome == "refused" and problem in r.detail, r.detail
    assert status(conn) == "built"


def test_no_email_block_refuses(conn, built) -> None:
    doc = load_doc(built)
    doc.pop("email")
    save_doc(built, doc)
    r = approve(conn, built)
    assert r.outcome == "refused" and "no email block" in r.detail


# -- approve ---------------------------------------------------------------------------------


def test_approve_moves_built_to_approved_and_prints_the_email(conn, built) -> None:
    r = approve(conn, built, dry_run=True)
    assert r.outcome == "dry_run" and status(conn) == "built"
    res = CliRunner().invoke(main, ["queue", "approve", QID, "--dir", str(built["td"]),
                                    "--sender", str(built["sender"]), "--site-dir",
                                    str(built["site"]), "--db", dbf(conn)])
    assert res.exit_code == 0, res.output
    assert f"{QID}: approved" in res.output
    assert "Subject: Quorvane's Founding Sales Development Representative hire" in res.output
    assert "Hi {first_name}," in res.output
    no_contact_values(res.output)
    assert status(conn) == "approved" and review_state(conn) == "approved"
    assert approve(conn, built).outcome == "unchanged"


def test_approve_refuses_a_stale_render_or_a_failing_check(conn, built) -> None:
    doc = load_doc(built)
    doc["why_now"] = doc["why_now"].replace("now", "this quarter")
    save_doc(built, doc)
    r = approve(conn, built)
    assert r.outcome == "refused" and "not the current render" in r.detail
    doc["why_now"] = "Synergy for Quorvane, twice: Quorvane."
    save_doc(built, doc)
    r = approve(conn, built)
    assert r.outcome == "refused" and "banned_phrase" in r.detail
    assert status(conn) == "built"


def test_approve_refuses_before_built(conn, ready) -> None:
    r = Q.approve(conn, QID, teardowns=ready["td"], sender_file=ready["sender"],
                  site=ready["site"])
    assert r.outcome == "refused" and "status diagnosed" in r.detail


# -- draft -----------------------------------------------------------------------------------


def test_draft_makes_one_gmail_draft_and_a_touch(conn, cache, approved, fake_gmail) -> None:
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "drafted", r
    assert "r-draft-1" in r.detail
    no_contact_values(r.detail + "\n".join(r.lines))
    # the recheck: the best signal's board, fresh; only the cited kinds
    assert checks.polled == [("greenhouse", BOARD)]
    assert checks.fetched == [["pricing", "product", "job_post"]]
    # the message: plain text, one recipient, the name filled locally, nothing attached
    assert len(fake_gmail.created) == 1 and fake_gmail.created[0]["userId"] == "me"
    msg = fake_gmail.message()
    assert msg["To"] == ADDRESS and not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"
    body = msg.get_content()
    assert body.startswith("Hi Ilsa,") and "Marrow" not in body
    assert "<img" not in body and "<html" not in body.lower()
    assert len(Q.links_in(body)) == 1 and Q.count_words(body) < 120
    t = touches(conn)
    assert len(t) == 1
    assert (t[0]["touch_no"], t[0]["channel"], t[0]["draft_id"], t[0]["status"]) == (
        1, "email", "r-draft-1", "drafted")
    assert t[0]["drafted_at"] and t[0]["sent_at"] is None
    assert status(conn) == "approved"  # a person sends; te queue sent moves it


def test_draft_is_idempotent(conn, cache, approved, fake_gmail) -> None:
    assert draft(conn, cache, approved, FakeChecks(conn, cache)).outcome == "drafted"
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "unchanged" and "already drafted" in r.detail
    assert len(fake_gmail.created) == 1 and checks.polled == [] and checks.fetched == []
    assert len(touches(conn)) == 1


def test_draft_refuses_without_the_base_url(conn, cache, approved, fake_gmail,
                                            monkeypatch) -> None:
    monkeypatch.delenv("TEARDOWN_BASE_URL")
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "refused" and "TEARDOWN_BASE_URL is not set" in r.detail
    assert fake_gmail.created == [] and checks.polled == [] and touches(conn) == []


@pytest.mark.parametrize("base, why", [("http://notes.example", "https"),
                                       ("https://bit.ly", "shortener")])
def test_draft_refuses_a_bad_base_url(conn, cache, approved, fake_gmail, monkeypatch,
                                      base, why) -> None:
    monkeypatch.setenv("TEARDOWN_BASE_URL", base)
    r = draft(conn, cache, approved, FakeChecks(conn, cache))
    assert r.outcome == "refused" and why in r.detail and fake_gmail.created == []


@pytest.mark.parametrize("entry, why", [
    (ADDRESS, "the recipient's address is suppressed"),
    ("ILSA@velmora.example", "the recipient's address is suppressed"),
    ("velmora.example", "domain velmora.example is suppressed"),
    (Q_DOMAIN, f"domain {Q_DOMAIN} is suppressed"),
])
def test_draft_refuses_a_suppressed_address_or_domain(conn, cache, approved, fake_gmail,
                                                      entry, why) -> None:
    with conn:
        db.insert(conn, Suppression(email_or_domain=entry, reason="optout"))
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "refused" and why in r.detail
    assert ADDRESS not in r.detail
    assert fake_gmail.created == [] and checks.polled == [] and touches(conn) == []


def test_linkedin_channel_prints_the_hand_text_and_makes_no_draft(conn, cache, approved,
                                                                  fake_gmail) -> None:
    with conn:
        conn.execute("DELETE FROM contacts")
    add_contact(conn, channel="linkedin", email_addr=None)
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "linkedin", r
    slug = conn.execute("SELECT page_slug FROM teardowns").fetchone()[0]
    note = r.lines[0]
    assert note == ("I wrote up notes on what Quorvane's Founding Sales Development "
                    f"Representative hire will need, and built {SLICE} for you: "
                    f"{BASE}/{slug}/")
    from engine.build.teardown import sentences

    assert len(sentences(note)) == 1
    no_contact_values(note + r.detail)
    assert fake_gmail.created == [] and checks.polled  # the recheck still ran
    t = touches(conn)
    assert (t[0]["channel"], t[0]["draft_id"], t[0]["status"]) == ("linkedin", None, "drafted")
    assert draft(conn, cache, approved, FakeChecks(conn, cache)).outcome == "unchanged"


def test_closed_role_switches_to_another_open_gtm_role(conn, cache, approved,
                                                       fake_gmail) -> None:
    with conn:
        db.insert(conn, Signal(signal_id=SIG2, source="greenhouse", board_slug=BOARD,
                               company_name_raw="Quorvane", title="Account Executive",
                               role_family="ae", offer_type="adjacent", fresh=1,
                               first_seen=TODAY - timedelta(days=5), company_id=QID))
    checks = FakeChecks(conn, cache, open_ids=[SIG2, "gh-quorvane-99"])
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "switched", r
    assert SIG2 in r.detail and "1 open role(s) on the board are not in the database" in r.detail
    assert any("back in review" in ln for ln in r.lines)
    best = conn.execute("SELECT best_signal_id FROM companies").fetchone()[0]
    assert best == SIG2
    assert status(conn) == "approved" and review_state(conn) == "pending"
    assert fake_gmail.created == [] and touches(conn) == [] and checks.fetched == []
    # nothing is drafted until the teardown is approved again
    r = draft(conn, cache, approved, FakeChecks(conn, cache, open_ids=[SIG2]))
    assert r.outcome == "refused" and "back in review" in r.detail


def test_closed_role_with_no_other_drops_stale_signal(conn, cache, approved,
                                                      fake_gmail) -> None:
    with conn:  # another GTM role, but stale: it does not count
        db.insert(conn, Signal(signal_id=SIG2, source="greenhouse", board_slug=BOARD,
                               company_name_raw="Quorvane", title="Account Executive",
                               role_family="ae", offer_type="adjacent", fresh=1,
                               posted_at=TODAY - timedelta(days=90),
                               first_seen=TODAY - timedelta(days=90), company_id=QID))
    poll = FakeChecks(conn, cache, open_ids=[SIG2], posted_days_ago=90)
    r = draft(conn, cache, approved, poll)
    assert r.outcome == "dropped" and r.detail.startswith("stale_signal")
    row = conn.execute("SELECT status, drop_reason, drop_stage FROM companies").fetchone()
    assert tuple(row) == ("dropped", "stale_signal", "send_day")
    assert fake_gmail.created == [] and touches(conn) == []


def test_unreadable_board_refuses_and_changes_nothing(conn, cache, approved,
                                                      fake_gmail) -> None:
    r = draft(conn, cache, approved, FakeChecks(conn, cache, outcome="blocked"))
    assert r.outcome == "refused" and "was blocked" in r.detail
    assert status(conn) == "approved" and review_state(conn) == "approved"
    assert fake_gmail.created == []


def test_signal_without_a_collector_needs_role_checked(conn, cache, approved,
                                                       fake_gmail) -> None:
    with conn:
        conn.execute("UPDATE signals SET source = 'careers' WHERE signal_id = ?", (SIG1,))
    checks = FakeChecks(conn, cache)
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "refused" and "--role-checked" in r.detail and checks.polled == []
    r = draft(conn, cache, approved, FakeChecks(conn, cache), role_checked=True)
    assert r.outcome == "drafted"


def test_failing_refaithfulness_refuses_and_pulls_back(conn, cache, approved,
                                                       fake_gmail) -> None:
    gone = Art("pricing", PRICING, "ok", "Plans start at 49 dollars a month.",
               {"links": [], "removed_text": []})
    checks = FakeChecks(conn, cache, replace={"pricing": gone})
    r = draft(conn, cache, approved, checks)
    assert r.outcome == "refused" and "no longer holds" in r.detail
    assert r.lines == [f"claim [1] (pricing, quote {PRICING_Q!r}): miss"]
    assert review_state(conn) == "pending" and status(conn) == "approved"
    assert fake_gmail.created == [] and touches(conn) == []
    # a person reads it again and re-approves; the recheck runs again on the next draft
    assert approve(conn, approved).outcome == "approved" and review_state(conn) == "approved"
    assert draft(conn, cache, approved, FakeChecks(conn, cache)).outcome == "drafted"


def test_candidate_needs_the_application_recorded(conn, cache, built, fake_gmail) -> None:
    doc = load_doc(built)
    doc["header"]["offer_type"] = "candidate"
    doc["email"].pop("subject")
    save_doc(built, doc)
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'candidate' WHERE signal_id = ?", (SIG1,))
    R.render_company(conn, QID, teardowns=built["td"], sender_file=built["sender"],
                     site=built["site"])
    assert approve(conn, built).outcome == "approved"
    r = draft(conn, cache, built, FakeChecks(conn, cache))
    assert r.outcome == "refused" and "apply through the company's own form" in r.detail
    with conn:  # what te queue applied (Step 75) will record
        conn.execute("INSERT INTO touches (company_id, touch_no, channel, applied) "
                     "VALUES (?, 1, 'email', 1)", (QID,))
    r = draft(conn, cache, built, FakeChecks(conn, cache))
    assert r.outcome == "drafted", r
    assert fake_gmail.message()["Subject"].startswith("Applied for Founding Sales")
    t = touches(conn)
    assert len(t) == 1 and (t[0]["applied"], t[0]["status"]) == (1, "drafted")


def test_draft_refuses_unapproved(conn, cache, built, fake_gmail) -> None:
    r = draft(conn, cache, built, FakeChecks(conn, cache))
    assert r.outcome == "refused" and "status built" in r.detail


def test_dry_run_makes_no_request_no_draft_no_write(conn, cache, approved, fake_gmail) -> None:
    res = CliRunner().invoke(main, ["queue", "draft", "--company", QID, "--dry-run",
                                    "--dir", str(approved["td"]), "--sender",
                                    str(approved["sender"]), "--site-dir",
                                    str(approved["site"]), "--db", dbf(conn)])
    assert res.exit_code == 0, res.output
    assert f"{QID}: dry_run" in res.output and "would re-fetch fresh: pricing, product, " \
                                               "job_post" in res.output
    assert "Hi {first_name}," in res.output
    no_contact_values(res.output)
    assert fake_gmail.created == [] and touches(conn) == []


def test_cli_draft_needs_a_selection(db_file) -> None:
    res = CliRunner().invoke(main, ["queue", "draft", "--db", str(db_file)])
    assert res.exit_code != 0 and "--company or --pilot" in res.output


def test_cli_draft_pilot_runs_the_live_checks_object(conn, cache, approved, fake_gmail,
                                                     monkeypatch) -> None:
    made: list[FakeChecks] = []

    def live(cache_):
        made.append(FakeChecks(conn, cache))
        return made[-1]

    monkeypatch.setattr(Q, "LiveChecks", live)
    args = ["queue", "draft", "--pilot", "--dir", str(approved["td"]), "--sender",
            str(approved["sender"]), "--site-dir", str(approved["site"]), "--db", dbf(conn)]
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    assert f"{QID}: drafted: Gmail draft r-draft-1" in res.output
    no_contact_values(res.output)
    assert made[0].closed
    res = CliRunner().invoke(main, args)
    assert "already drafted" in res.output and len(fake_gmail.created) == 1


def test_pace_warning(conn, approved) -> None:
    assert Q.pace_warning(conn, [QID], pilot=True, day=TODAY) is None
    with conn:
        for n in range(5):
            cid = f"other-{n}"
            conn.execute("INSERT INTO companies (company_id, name, status) VALUES (?, ?, "
                         "'approved')", (cid, f"Other {n}"))
            conn.execute("INSERT INTO touches (company_id, touch_no, channel, drafted_at, "
                         "status) VALUES (?, 1, 'email', ?, 'drafted')",
                         (cid, f"{TODAY.isoformat()}T04:00:00Z"))
    w = Q.pace_warning(conn, [QID], pilot=True, day=TODAY)
    assert w is not None and "daily pace of 5" in w
    assert Q.pace_warning(conn, [QID], pilot=False, day=TODAY) is None
    assert Q.pace_warning(conn, [QID], pilot=True, day=TODAY + timedelta(days=1)) is None


# -- sent, done ------------------------------------------------------------------------------


def test_sent_records_the_send_and_moves_to_sent(conn, cache, approved, fake_gmail) -> None:
    assert Q.sent(conn, QID).outcome == "refused"  # nothing drafted yet
    assert draft(conn, cache, approved, FakeChecks(conn, cache)).outcome == "drafted"
    res = CliRunner().invoke(main, ["queue", "sent", QID, "--db", dbf(conn)])
    assert res.exit_code == 0 and f"{QID}: sent" in res.output, res.output
    t = touches(conn)[0]
    assert t["status"] == "sent" and t["sent_at"] and status(conn) == "sent"
    assert Q.sent(conn, QID).outcome == "unchanged"


def test_sent_refuses_a_time_before_the_draft_or_in_the_future(conn, cache, approved,
                                                               fake_gmail) -> None:
    draft(conn, cache, approved, FakeChecks(conn, cache))
    assert Q.sent(conn, QID, at=datetime(2020, 1, 1, tzinfo=UTC)).outcome == "refused"
    future = datetime.now(UTC) + timedelta(days=1)
    assert Q.sent(conn, QID, at=future).outcome == "refused"
    assert status(conn) == "approved"


def test_done_records_review_minutes(conn, built) -> None:
    res = CliRunner().invoke(main, ["queue", "done", QID, "--minutes", "35", "--db", dbf(conn)])
    assert res.exit_code == 0 and "35 review minutes" in res.output, res.output
    assert conn.execute("SELECT review_minutes FROM teardowns").fetchone()[0] == 35
    assert Q.done(conn, QID, 35).outcome == "unchanged"
    r = Q.done(conn, QID, 28.5)
    assert r.outcome == "recorded" and "(was 35)" in r.detail
    res = CliRunner().invoke(main, ["queue", "done", QID, "--minutes", "0", "--db", dbf(conn)])
    assert res.exit_code != 0


def test_done_refuses_without_a_teardown(conn, ready) -> None:
    assert Q.done(conn, QID, 10).outcome == "refused"


# -- the live checks, without a network -------------------------------------------------------


class _Guard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def fetch(self, url: str, kind: str, *, fresh: bool = False):
        from engine.guard.http import FetchResult

        self.calls.append((url, kind, fresh))
        body = json.dumps({"jobs": [{"id": 1, "title": "SDR",
                                     "updated_at": "2026-11-10"}]}).encode()
        return FetchResult(url=url, kind=kind, outcome="ok", status=200, final_url=url,
                           body=body, sha256=None, cache_path=None,
                           content_type="application/json", fetched_at=datetime.now(UTC),
                           from_cache=False)


def test_live_checks_poll_the_board_fresh(cache) -> None:  # noqa: F811
    g = _Guard()

    class RT:
        guard = g

        async def aclose(self) -> None:
            pass

    checks = Q.LiveChecks(cache, make_runtime=lambda c, b: RT())
    res = asyncio.run(checks.poll_board("greenhouse", BOARD))
    assert [s.signal_id for s in res.signals] == ["gh-quorvane-1"]
    assert g.calls and all(fresh for _, _, fresh in g.calls)
    asyncio.run(checks.aclose())


# -- Gmail: drafts only ------------------------------------------------------------------------


def test_gmail_scope_is_exactly_compose_and_only_create_draft_is_public() -> None:
    assert gmail.SCOPE == "https://www.googleapis.com/auth/gmail.compose"
    assert gmail._SCOPES == [gmail.SCOPE]
    assert gmail.__all__ == ["SCOPE", "GmailError", "create_draft"]
    public = {n for n in dir(gmail) if not n.startswith("_") and callable(getattr(gmail, n))
              and getattr(getattr(gmail, n), "__module__", "") == gmail.__name__}
    assert public == {"GmailError", "create_draft"}


def test_gmail_create_draft_builds_one_plain_text_message(fake_gmail) -> None:
    draft_id = gmail.create_draft("ilsa@velmora.example", "Notes", "Hi Ilsa,\n\nOne link.")
    assert draft_id == "r-draft-1"
    msg = fake_gmail.message()
    assert msg["To"] == "ilsa@velmora.example" and msg["Subject"] == "Notes"
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert list(msg.iter_attachments()) == []


@pytest.mark.parametrize("to, subject, body", [
    ("ilsa@velmora.example, other@velmora.example", "Notes", "x"),
    ("ilsa@velmora.example", "two\nlines", "x"),
    ("ilsa@velmora.example", "Notes", "  "),
])
def test_gmail_refuses_a_malformed_message(fake_gmail, to, subject, body) -> None:
    with pytest.raises(gmail.GmailError) as e:
        gmail.create_draft(to, subject, body)
    assert "velmora" not in str(e.value) and fake_gmail.created == []


def test_gmail_errors_carry_no_message_content(monkeypatch) -> None:
    class Boom:
        def users(self):
            return self

        def drafts(self):
            return self

        def create(self, **kw):
            return self

        def execute(self):
            raise RuntimeError("ilsa@velmora.example rejected")

    monkeypatch.setattr(gmail, "_credentials", lambda c, t: object())
    monkeypatch.setattr(gmail, "_build_service", lambda creds: Boom())
    with pytest.raises(gmail.GmailError) as e:
        gmail.create_draft("ilsa@velmora.example", "Notes", "Hi")
    assert "velmora" not in str(e.value) and "RuntimeError" in str(e.value)


def test_gmail_token_scopes(tmp_path) -> None:
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"scopes": [gmail.SCOPE]}), encoding="utf-8")
    assert gmail._token_scopes_ok(p)
    for scopes in ([gmail.SCOPE, "https://www.googleapis.com/auth/gmail.send"],
                   ["https://mail.google.com/"], None):
        p.write_text(json.dumps({"scopes": scopes}), encoding="utf-8")
        assert not gmail._token_scopes_ok(p)
    with pytest.raises(gmail.GmailError, match="gmail.compose"):
        gmail._credentials(tmp_path / "client.json", p)


SEND_PATTERNS = {
    "messages().send": re.compile(r"messages\s*\(\s*\)\s*\.\s*send\b"),
    "drafts().send": re.compile(r"drafts\s*\(\s*\)\s*\.\s*send\b"),
    ".send( on any object": re.compile(r"\.\s*send\s*\("),
    "send_message": re.compile(r"\bsend_message\b"),
    "sendmail": re.compile(r"\bsendmail\b"),
    "smtplib": re.compile(r"\bsmtplib\b"),
    "a scope wider than gmail.compose": re.compile(
        r"auth/gmail\.(?!compose\b)\w+|mail\.google\.com/?[\"']"),
}


def test_no_code_path_sends_email() -> None:
    hits = []
    for p in sorted((ROOT / "engine").rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        for what, rx in SEND_PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{p.relative_to(ROOT)}:{line}: {what}")
    assert hits == []


def test_the_scan_would_catch_a_send() -> None:
    bad = ('svc.users().messages().send(userId="me", body=b).execute()\n'
           'svc.users().drafts().send(userId="me", body=b)\nimport smtplib\nsvc.send(x)\n')
    found = {what for what, rx in SEND_PATTERNS.items() if rx.search(bad)}
    assert found >= {"messages().send", "drafts().send", ".send( on any object", "smtplib"}
