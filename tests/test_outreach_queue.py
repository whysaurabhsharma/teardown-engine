"""Step 66 (the review queue, te queue open / report), Step 71 (follow-ups by hand),
Step 74 (bounces and the stop), Step 75 (te queue applied).

Invented companies and people only: Quorvane and its invented recipient from
tests/test_outreach_draft.py, plus invented companies Velto and Marrowfield.
Gmail is fully faked; the send-day recheck runs on the fake Checks object; the
review server is driven through QueueApp.handle and, once, over a real loopback
socket bound to 127.0.0.1.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import pytest
from click.testing import CliRunner

from engine import db, states
from engine.build import drafter as D
from engine.build import quality as QL
from engine.build import render as R
from engine.cli import main
from engine.models import Contact, Review
from engine.outreach import applied as A
from engine.outreach import bounces as B
from engine.outreach import draft as Q
from engine.outreach import followups as F
from engine.outreach import review as RV
from tests.conftest import add_company
from tests.test_extract_facts import QID, Q_DOMAIN, bundles, cache  # noqa: F401 (fixtures)
from tests.test_judge import (  # noqa: F401 (fixtures)
    JOB_Q,
    PRICING,
    PRICING_Q,
    extracted,
    no_net,
    split,
)
from tests.test_judge_verify import judged, providers  # noqa: F401 (fixtures)
from tests.test_outreach_draft import (  # noqa: F401 (fixtures)
    ADDRESS,
    SIG1,
    SLICE,
    FakeChecks,
    approve,
    approved,
    built,
    dbf,
    draft,
    fake_gmail,
    loopback_only,
    no_contact_values,
    review_state,
    status,
    touches,
)
from tests.test_teardown import (  # noqa: F401 (fixtures)
    _fresh_model_budget,
    dirs,
    load_doc,
    ready,
    save_doc,
    verified,
)

D0 = date(2026, 10, 1)  # touch 1 sent; every date here is in the past
TOKEN = "t0ken-for-tests"
PORT = 8765
HDRS = {"Host": f"127.0.0.1:{PORT}"}


def at(d: date, hour: int = 9) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def make_candidate(conn, d) -> None:
    doc = load_doc(d)
    doc["header"]["offer_type"] = "candidate"
    doc["email"].pop("subject")
    save_doc(d, doc)
    with conn:
        conn.execute("UPDATE signals SET offer_type = 'candidate' WHERE signal_id = ?", (SIG1,))
    R.render_company(conn, QID, teardowns=d["td"], sender_file=d["sender"], site=d["site"])


def sent_touch1(conn, day: date = D0, channel: str = "email") -> None:
    with conn:
        conn.execute("INSERT INTO touches (company_id, touch_no, channel, draft_id, drafted_at, "
                     "sent_at, status) VALUES (?, 1, ?, 'r-draft-1', ?, ?, 'sent')",
                     (QID, channel, Q.stamp(at(day, 3)), Q.stamp(at(day, 4))))
    states.advance(QID, "sent", conn)


# -- Step 75: te queue applied ---------------------------------------------------------------


def test_applied_unblocks_the_candidate_draft(conn, cache, built, fake_gmail) -> None:  # noqa: F811
    make_candidate(conn, built)
    assert approve(conn, built).outcome == "approved"
    r = draft(conn, cache, built, FakeChecks(conn, cache))
    assert r.outcome == "refused" and "apply through the company's own form" in r.detail
    res = CliRunner().invoke(main, ["queue", "applied", QID, "--on", "2026-10-01", "--note",
                                    "applied via the careers form", "--db", dbf(conn)])
    assert res.exit_code == 0 and f"{QID}: applied" in res.output, res.output
    t = touches(conn)
    assert len(t) == 1
    assert (t[0]["applied"], t[0]["applied_on"], t[0]["status"], t[0]["draft_id"]) == (
        1, "2026-10-01", None, None)
    assert A.applied(conn, QID, on=date(2026, 10, 1)).outcome == "unchanged"
    r = draft(conn, cache, built, FakeChecks(conn, cache))
    assert r.outcome == "drafted", r
    t = touches(conn)
    assert len(t) == 1 and (t[0]["applied"], t[0]["applied_on"], t[0]["status"]) == (
        1, "2026-10-01", "drafted")
    assert fake_gmail.message()["Subject"].startswith("Applied for")


def test_applied_is_refused_for_an_adjacent_signal(conn, built) -> None:
    res = CliRunner().invoke(main, ["queue", "applied", QID, "--db", dbf(conn)])
    assert res.exit_code == 1
    assert "not a candidate role" in res.output and "refused" in res.output
    assert touches(conn) == []


def test_applied_refuses_a_future_date(conn, built) -> None:
    make_candidate(conn, built)
    r = A.applied(conn, QID, on=date(2026, 11, 20), today=date(2026, 11, 17))
    assert r.outcome == "refused" and "future" in r.detail and touches(conn) == []


# -- Step 74: bounces and the stop -----------------------------------------------------------


def other_sent(conn, cid: str, name: str, day: date = D0) -> None:
    add_company(conn, cid, name=name, domain=f"{cid}.example")
    with conn:
        conn.execute("INSERT INTO touches (company_id, touch_no, channel, sent_at, status) "
                     "VALUES (?, 1, 'email', ?, 'sent')", (cid, Q.stamp(at(day))))


def test_bounce_suppresses_the_address_and_names_nothing(conn, approved) -> None:
    sent_touch1(conn)
    res = CliRunner().invoke(main, ["queue", "bounce", QID, "--on", "2026-10-02", "--db",
                                    dbf(conn)])
    assert res.exit_code == 0 and f"{QID}: bounced" in res.output, res.output
    no_contact_values(res.output)
    t = touches(conn)[0]
    assert (t["outcome"], t["bounced_on"]) == ("bounced", "2026-10-02")
    sup = conn.execute("SELECT email_or_domain, reason FROM suppressions").fetchall()
    assert [(r[0], r[1]) for r in sup] == [(ADDRESS, "bounce")]
    assert B.active_stop(conn) is None  # one bounce does not stop sending
    assert B.bounce(conn, QID, on=date(2026, 10, 2)).outcome == "unchanged"


def test_bounce_needs_a_sent_email(conn, approved) -> None:
    assert B.bounce(conn, QID).outcome == "refused"
    sent_touch1(conn, channel="linkedin")
    r = B.bounce(conn, QID, on=date(2026, 10, 2))
    assert r.outcome == "refused" and "only an email bounces" in r.detail


def test_two_bounces_in_one_day_stop_drafting_until_resume(conn, cache, approved,  # noqa: F811
                                                           fake_gmail) -> None:
    other_sent(conn, "velto-example", "Velto")
    other_sent(conn, "marrowfield-example", "Marrowfield")
    day = date(2026, 10, 3)
    assert B.bounce(conn, "velto-example", on=day).outcome == "bounced"
    other_day = B.bounce(conn, "marrowfield-example", on=day + timedelta(days=1))
    assert other_day.outcome == "bounced" and B.active_stop(conn) is None
    with conn:  # put the second bounce on the same day
        conn.execute("UPDATE touches SET outcome = NULL, bounced_on = NULL WHERE company_id = ?",
                     ("marrowfield-example",))
    r = B.bounce(conn, "marrowfield-example", on=day)
    assert r.outcome == "bounced" and any("sending is stopped" in x for x in r.lines)
    stop = B.active_stop(conn)
    assert stop is not None and stop["stopped_on"] == day.isoformat()

    r = draft(conn, cache, approved, FakeChecks(conn, cache))
    assert r.outcome == "refused" and "sending is stopped" in r.detail
    assert fake_gmail.created == [] and touches(conn) == []

    with pytest.raises(Q.QueueError):
        B.resume(conn, "  ")
    res = CliRunner().invoke(main, ["queue", "resume", "--reason", "addresses re-verified",
                                    "--db", dbf(conn)])
    assert res.exit_code == 0 and "resumed" in res.output, res.output
    assert B.active_stop(conn) is None
    assert B.resume(conn, "again").outcome == "unchanged"
    assert draft(conn, cache, approved, FakeChecks(conn, cache)).outcome == "drafted"


# -- Step 71: follow-ups ---------------------------------------------------------------------


def listing(conn, d, today: date) -> F.Listing:
    return F.followups(conn, today, teardowns=d["td"], sender_file=d["sender"])


def secondary(conn, gaps: list[str]) -> None:
    with conn:
        conn.execute("UPDATE diagnoses SET secondary = ? WHERE company_id = ?",
                     (json.dumps(gaps), QID))


def email_claims(d, claims: list[int]) -> None:
    doc = load_doc(d)
    doc["email"]["claims"] = claims
    save_doc(d, doc)


def test_touch2_is_due_from_day_4_only(conn, approved) -> None:
    sent_touch1(conn)
    res = listing(conn, approved, D0 + timedelta(days=3))
    assert res.due == [] and res.upcoming == [(QID, 2, D0 + timedelta(days=4))]
    res = listing(conn, approved, D0 + timedelta(days=4))
    assert [(f.company_id, f.touch_no, f.day) for f in res.due] == [(QID, 2, 4)]


def test_touch2_without_an_evidenced_secondary_gap_asks_the_person(conn, approved) -> None:
    # the fixture's secondary gap is G5; its only passing claim of a G5 kind (the job
    # post, [0]) is one the touch-1 email cited, so nothing new is evidenced
    sent_touch1(conn)
    f = listing(conn, approved, D0 + timedelta(days=4)).due[0]
    assert f.fallback and f.text == F.FALLBACK_2 and f.claims == []
    # absence alone never names a gap: G1 with only the not-found product claim left
    secondary(conn, ["G1"])
    f = listing(conn, approved, D0 + timedelta(days=4)).due[0]
    assert f.fallback and f.text == F.FALLBACK_2


def test_touch2_text_traces_to_passing_claims_of_the_secondary_gap(conn, approved) -> None:
    from engine.build import teardown as T

    sent_touch1(conn)
    secondary(conn, ["G1"])
    email_claims(approved, [1])  # the email cited the pricing claim only
    f = listing(conn, approved, D0 + timedelta(days=4)).due[0]
    assert not f.fallback and f.gap == "G1" and f.claims == [0, 2] and f.problems == []
    ctx = T.load_context(conn, QID)
    for i in f.claims:  # each sentence is a passing claim's own text, of a G1 kind
        assert T.claim_problem(ctx, i) is None
        assert ctx.claims[i].artifact_kind in F.GAP_KINDS["G1"]
        assert ctx.claims[i].text in f.text
    body = f.text.split("\n\n")[1]
    assert body == " ".join(ctx.claims[i].text for i in f.claims)
    assert len(T.sentences(body)) == 2
    assert f.text.startswith("Hi {first_name},\n\n")
    assert f.text.endswith('Not relevant? Reply "no" and I will not write again.')
    assert "Avery Example" in f.text and Q.links_in(f.text) == []


def test_followups_cli_fills_the_name_on_a_terminal_only(conn, approved, monkeypatch) -> None:
    sent_touch1(conn)
    args = ["queue", "followups", "--today", (D0 + timedelta(days=5)).isoformat(), "--dir",
            str(approved["td"]), "--sender", str(approved["sender"]), "--db", dbf(conn)]
    secondary(conn, ["G1"])
    email_claims(approved, [1])
    res = CliRunner().invoke(main, args)
    assert res.exit_code == 0, res.output
    assert f"{QID}: touch 2 due (day 5" in res.output and "Hi {first_name}," in res.output
    assert f"te queue followup-sent {QID} --touch 2" in res.output
    no_contact_values(res.output)
    monkeypatch.setattr(F, "is_terminal", lambda: True)
    res = CliRunner().invoke(main, args)
    assert "Hi Ilsa," in res.output and "Marrow" not in res.output


def test_followup_sent_then_touch3_on_day_9_then_no_more(conn, approved) -> None:
    sent_touch1(conn)
    r = F.followup_sent(conn, QID, 2, at=at(D0 + timedelta(days=3)))
    assert r.outcome == "refused" and "not due until" in r.detail
    assert F.followup_sent(conn, QID, 3, at=at(D0 + timedelta(days=9))).outcome == "refused"
    res = CliRunner().invoke(main, ["queue", "followup-sent", QID, "--touch", "2", "--at",
                                    Q.stamp(at(D0 + timedelta(days=4))), "--db", dbf(conn)])
    assert res.exit_code == 0 and "touch 2 (email-reply) sent" in res.output, res.output
    t2 = [t for t in touches(conn) if t["touch_no"] == 2][0]
    assert (t2["channel"], t2["status"], t2["draft_id"]) == ("email-reply", "sent", None)
    assert F.followup_sent(conn, QID, 2, at=at(D0 + timedelta(days=4))).outcome == "unchanged"

    assert listing(conn, approved, D0 + timedelta(days=8)).upcoming == [
        (QID, 3, D0 + timedelta(days=9))]
    f = listing(conn, approved, D0 + timedelta(days=9)).due[0]
    assert f.touch_no == 3 and not f.fallback
    line = f.text.split("\n\n")[1]
    assert SLICE in line and "as a file" in line and "last note" in line
    assert Q.links_in(f.text) == []

    r = F.followup_sent(conn, QID, 3, at=at(D0 + timedelta(days=9)))
    assert r.outcome == "sent" and status(conn) == "sent"
    res = listing(conn, approved, D0 + timedelta(days=30))
    assert res.due == [] and res.skipped and "3 touches sent" in res.skipped[0][1]
    r = F.followup_sent(conn, QID, 4, at=at(D0 + timedelta(days=12)))
    assert r.outcome == "refused" and "no fourth message" in r.detail
    assert len(touches(conn)) == 3


def test_after_touch3_the_company_stays_sent_and_can_still_reply(conn, approved) -> None:
    sent_touch1(conn)
    F.followup_sent(conn, QID, 2, at=at(D0 + timedelta(days=4)))
    assert F.followup_sent(conn, QID, 3, at=at(D0 + timedelta(days=9))).outcome == "sent"
    assert status(conn) == "sent"
    assert states.advance(QID, "replied", conn) is True
    assert status(conn) == "replied"
    res = listing(conn, approved, D0 + timedelta(days=20))
    assert res.due == [] and "status replied" in res.skipped[0][1]

def test_a_late_touch2_pushes_touch3(conn, approved) -> None:
    sent_touch1(conn)
    F.followup_sent(conn, QID, 2, at=at(D0 + timedelta(days=7)))
    assert listing(conn, approved, D0 + timedelta(days=9)).upcoming == [
        (QID, 3, D0 + timedelta(days=12))]


@pytest.mark.parametrize("how, why", [
    ("suppressed", "suppressed"), ("replied", "a reply"), ("bounced", "bounced"),
    ("dropped", "dropped"),
])
def test_followups_skip_suppressed_replied_bounced_dropped(conn, approved, how, why) -> None:
    sent_touch1(conn)
    with conn:
        if how == "suppressed":
            conn.execute("INSERT INTO suppressions (email_or_domain, reason) VALUES (?, 'no')",
                         (Q_DOMAIN,))
        elif how == "replied":
            conn.execute("UPDATE touches SET reply_at = ? WHERE company_id = ?",
                         (Q.stamp(at(D0 + timedelta(days=1))), QID))
        elif how == "bounced":
            conn.execute("UPDATE touches SET outcome = 'bounced', bounced_on = ? WHERE "
                         "company_id = ?", ((D0 + timedelta(days=1)).isoformat(), QID))
    if how == "dropped":
        states.drop(QID, "conflict", "review_queue", conn)
    res = listing(conn, approved, D0 + timedelta(days=4))
    assert res.due == [] and len(res.skipped) == 1 and why in res.skipped[0][1]
    r = F.followup_sent(conn, QID, 2, at=at(D0 + timedelta(days=4)))
    assert r.outcome == "refused" and len(touches(conn)) == 1


def test_secondary_observation_needs_two_claims_and_a_found_one() -> None:
    from types import SimpleNamespace

    from engine.models import Claim

    def claim(kind, found, text):
        return Claim(text=text, artifact_kind=kind, found=found, url="https://x.example/",
                     quote="q" if found else None)

    ctx = SimpleNamespace(
        diagnosis={"attempted": 1, "gap_type": "G3", "secondary": '["G2"]'},
        claims=[claim("pricing", False, "No pricing page was found."),
                claim("demo_form", False, "No demo form was found.")])
    import engine.build.teardown as T

    orig = T.claim_problem
    try:
        T.claim_problem = lambda c, i: None
        assert F.secondary_observation(ctx, set()) is None  # absence only
        ctx.claims.append(claim("homepage", True, "The homepage says book a demo."))
        obs = F.secondary_observation(ctx, set())
        assert obs is not None and obs.gap == "G2" and 2 in obs.claims and len(obs.claims) == 2
        ctx.claims[2] = claim("homepage", True, "This is the best tool. Ever.")
        assert F.secondary_observation(ctx, set()) is None  # two sentences and a superlative
    finally:
        T.claim_problem = orig


# -- Step 66: the review queue ---------------------------------------------------------------


def app_for(conn, d, **kw) -> RV.QueueApp:
    path = dbf(conn)
    return RV.QueueApp(lambda: db.connect(path), token=TOKEN, port=PORT, teardowns=d["td"],
                       sender_file=d["sender"], site=d["site"], **kw)


def post(app, cid: str, form: dict, headers: dict | None = None) -> RV.Response:
    return app.handle("POST", f"/{TOKEN}/c/{cid}", headers or HDRS,
                      urlencode(form).encode())


def reviews(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM reviews ORDER BY review_id")]


def test_queue_pages_need_the_token_and_the_local_host(conn, built) -> None:
    app = app_for(conn, built)
    assert app.handle("GET", "/", HDRS).status == 404
    assert app.handle("GET", "/wrong-token/", HDRS).status == 404
    assert app.handle("GET", f"/{TOKEN}/", {"Host": f"evil.example:{PORT}"}).status == 403
    r = app.handle("GET", f"/{TOKEN}/", HDRS)
    assert r.status == 200 and QID.encode() in r.body


def test_company_page_shows_evidence_quality_and_the_email(conn, built) -> None:
    QL.run_company(conn, QID, directory=built["td"], sender_file=built["sender"])
    app = app_for(conn, built)
    r = app.handle("GET", f"/{TOKEN}/c/{QID}", HDRS)
    assert r.status == 200
    page = r.body.decode()
    assert "Hi {first_name}," in page  # the email preview, name unfilled
    assert JOB_Q in page and PRICING_Q in page and PRICING in page  # claims, quotes, links
    assert "Quality gates" in page and "te teardown check passes" in page
    assert f'src="/{TOKEN}/page/{QID}/"' in page and "Facts" in page and "Live links" in page
    assert 'rel="noopener noreferrer"' in page
    no_contact_values(page)
    framed = app.handle("GET", f"/{TOKEN}/page/{QID}/", HDRS)
    assert framed.status == 200 and b"teardown.css" in framed.body
    assert app.handle("GET", f"/{TOKEN}/page/{QID}/teardown.css", HDRS).status == 200


def test_post_without_the_token_is_refused(conn, built) -> None:
    app = app_for(conn, built)
    assert post(app, QID, {"action": "approve"}).status == 403
    assert post(app, QID, {"action": "approve", "token": "nope"}).status == 403
    bad_origin = {**HDRS, "Origin": "https://evil.example"}
    assert post(app, QID, {"action": "approve", "token": TOKEN}, bad_origin).status == 403
    assert status(conn) == "built" and reviews(conn) == []


def test_approve_from_the_queue_records_minutes(conn, built) -> None:
    clock = iter([100.0, 100.0 + 13 * 60])
    app = app_for(conn, built, clock=lambda: next(clock))
    app.handle("GET", f"/{TOKEN}/c/{QID}", HDRS)  # opened
    r = post(app, QID, {"action": "approve", "token": TOKEN, "note": "light edits"})
    assert r.status == 200 and b"approved" in r.body, r.body
    assert status(conn) == "approved" and review_state(conn) == "approved"
    rv = reviews(conn)
    assert [(x["decision"], x["minutes"], x["note"]) for x in rv] == [
        ("approve", 13.0, "light edits")]
    assert conn.execute("SELECT review_minutes FROM teardowns").fetchone()[0] == 13.0


def test_kill_holds_the_teardown_until_the_file_changes(conn, built) -> None:
    app = app_for(conn, built)
    r = post(app, QID, {"action": "kill", "token": TOKEN, "reason": "page_copy",
                        "note": "section 3 reads as a grade", "minutes": "22"})
    assert r.status == 200 and b"killed" in r.body, r.body
    assert status(conn) == "built" and review_state(conn) == RV.KILLED
    rv = reviews(conn)[0]
    assert (rv["decision"], rv["reason"], rv["minutes"]) == ("kill", "page_copy", 22.0)
    assert rv["file_sha256"] == RV.file_sha(QID, built["td"])
    r = approve(conn, built)
    assert r.outcome == "refused" and "killed in review (page_copy)" in r.detail
    doc = load_doc(built)
    doc["why_now"] = doc["why_now"].replace("now", "this quarter")
    save_doc(built, doc)
    assert post(app, QID, {"action": "render", "token": TOKEN}).status == 200
    assert approve(conn, built).outcome == "approved"


def test_kill_needs_a_known_reason(conn, built) -> None:
    app = app_for(conn, built)
    assert post(app, QID, {"action": "kill", "token": TOKEN, "reason": "meh"}).status == 409
    assert post(app, QID, {"action": "kill", "token": TOKEN, "reason": "other"}).status == 409
    assert reviews(conn) == [] and review_state(conn) == "pending"


def test_kill_with_an_icp_reason_drops_the_company(conn, built) -> None:
    app = app_for(conn, built)
    r = post(app, QID, {"action": "kill", "token": TOKEN, "reason": "conflict"})
    assert r.status == 200 and b"dropped" in r.body
    row = conn.execute("SELECT status, drop_reason, drop_stage FROM companies WHERE "
                       "company_id = ?", (QID,)).fetchone()
    assert tuple(row) == ("dropped", "conflict", "review_queue")


def test_edited_records_the_sections_changed_since_the_draft(conn, built, monkeypatch) -> None:
    doc = load_doc(built)
    original = {s: doc.get(s) for s in D.SECTIONS}
    doc["drafted"] = {"by": D.PROMPT_VERSION, "key": "d-1.0:test",
                      "sections": {s: D.section_hash(doc.get(s)) for s in D.SECTIONS}}
    save_doc(built, doc)
    app = app_for(conn, built)
    r = post(app, QID, {"action": "edited", "token": TOKEN})
    assert b"unchanged" in r.body and reviews(conn) == []
    doc["why_now"] = doc["why_now"].replace("now", "this quarter")
    save_doc(built, doc)
    r = post(app, QID, {"action": "edited", "token": TOKEN})  # the drafter's text is not cached
    assert r.status == 200 and b"why_now" in r.body
    assert json.loads(reviews(conn)[-1]["edited"]) == {"why_now": None}
    monkeypatch.setattr(RV, "_drafted_blocks", lambda conn, cid, raw: original)
    post(app, QID, {"action": "edited", "token": TOKEN, "note": "softer"})
    e = json.loads(reviews(conn)[-1]["edited"])
    assert list(e) == ["why_now"] and 0 < e["why_now"] < 0.5


def test_report_aggregates_kills_and_edits(conn) -> None:
    for cid, name in (("aldermoor-example", "Aldermoor"), ("brisket-example", "Brisketly"),
                      ("cindral-example", "Cindral"), ("dunhallow-example", "Dunhallow")):
        add_company(conn, cid, name=name, domain=f"{cid}.example")
    rows = [
        Review(company_id="aldermoor-example", decision="approve", edited={"why_now": 0.1},
               minutes=20, version="t"),
        Review(company_id="brisket-example", decision="kill", reason="page_copy",
               note="too long", edited={"need": 0.6, "why_now": 0.5}, minutes=30, version="t"),
        Review(company_id="cindral-example", decision="kill", reason="page_copy", minutes=10,
               version="t"),
        Review(company_id="dunhallow-example", decision="edited", edited={"email": None},
               version="t"),
    ]
    with conn:
        for r in rows:
            db.insert(conn, r)
    res = CliRunner().invoke(main, ["queue", "report", "--db", dbf(conn)])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "decided: 3 companies (1 approved, 2 killed); 1 with edits" in out
    assert "page_copy: 2  fix in: drafter" in out and "brisket-example: too long" in out
    sections = out[out.index("most-edited"):out.index("average")]
    assert sections.splitlines()[1].strip() == "why_now: 2; 30%"
    assert "average review minutes: 20.0 (3 timed)" in out
    assert "Gate 5.3: 1/3 passed review with light edits" in out


def test_open_dry_run_lists_the_queue(conn, built) -> None:
    res = CliRunner().invoke(main, ["queue", "open", "--dry-run", "--db", dbf(conn)])
    assert res.exit_code == 0 and QID in res.output and "127.0.0.1" in res.output


def test_server_binds_loopback_and_checks_the_token_over_a_socket(db_file) -> None:
    app = RV.QueueApp(lambda: db.connect(db_file), token=TOKEN)
    httpd = RV.make_server(app, 0)
    assert httpd.server_address[0] == "127.0.0.1" and app.port == httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def call(method: str, path: str, body: str | None = None) -> tuple[int, dict]:
            c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
            hdrs = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
            c.request(method, path, body=body, headers=hdrs)
            resp = c.getresponse()
            resp.read()
            out = (resp.status, dict(resp.getheaders()))
            c.close()
            return out

        st, h = call("GET", f"/{TOKEN}/")
        assert st == 200 and h["Referrer-Policy"] == "no-referrer"
        assert call("GET", "/")[0] == 404
        assert call("POST", f"/{TOKEN}/c/{QID}", "action=approve")[0] == 403
        assert call("POST", f"/{TOKEN}/c/{QID}", f"action=nothing&token={TOKEN}")[0] == 409
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_module_never_binds_all_interfaces() -> None:
    from pathlib import Path

    src = Path(RV.__file__).read_text(encoding="utf-8")
    assert RV.HOST == "127.0.0.1"
    assert "LocalServer((HOST, port)" in src  # the one place a server is made
    assert '"0.0.0.0"' not in src and "'0.0.0.0'" not in src
    assert "((\"\"" not in src and "(('', " not in src  # no empty host either


def test_contact_values_never_reach_a_queue_page(conn, approved) -> None:
    with conn:
        db.insert(conn, Contact(company_id=QID, name="Odile Fenwright", title="Founder",
                                source_url=f"https://{Q_DOMAIN}/about", channel="email",
                                email="odile@fenwright.example", email_confidence=0.9,
                                confidence="high", email_source="manual"))
    app = app_for(conn, approved)
    page = app.handle("GET", f"/{TOKEN}/c/{QID}", HDRS).body.decode()
    for v in ("Odile", "Fenwright", "fenwright.example"):
        assert v not in page
    no_contact_values(page)
