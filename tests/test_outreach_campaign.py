"""Steps 67, 76, 77, 78, 80, 82: replies, meetings, closes, metrics, HubSpot sync
and the prep sheet.

Invented companies and people only (Quillfern, Brasswick, Tallowmere and the
rest; the recipients are made up). HubSpot is faked with httpx.MockTransport;
no real request is made. Metrics and prep run with sockets blocked.
"""

from __future__ import annotations

import json
import socket
from datetime import date

import httpx
import pytest
from click.testing import CliRunner

from engine import states
from engine.cli import main
from engine.guard import hubspot as HS
from engine.judge.judge import PROMPT_VERSION
from engine.outreach import draft as Q
from engine.outreach import hubspot_sync as SY
from engine.outreach import metrics as MX
from engine.outreach import prep as P
from engine.outreach import replies as RP
from tests.conftest import add_company

TODAY = date(2026, 11, 14)
TOKEN = "pat-na1-0000-fake-token-for-tests"

NAMES = {"a1": "Quillfern", "a2": "Brasswick", "a3": "Tallowmere", "a4": "Ondrelle",
         "h1": "Pemberlyn", "d1": "Corvisant", "d2": "Mistlecroft", "d3": "Hallowgate",
         "q1": "Fennerby"}
# invented recipients: (name, title, email, channel)
PEOPLE = {
    "a1": ("Ilsa Marwick", "Co-founder & CEO", "ilsa@a1.example", "email"),
    "a2": ("Doran Petch", "VP Sales", "doran@a2.example", "email"),
    "a3": ("Wren Allory", None, None, "linkedin"),
    "a4": ("Tamsin Grue", "Sales Manager", "tamsin@a4.example", "email"),
}


@pytest.fixture
def no_net(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# -- seeding -------------------------------------------------------------------------------


def signal(conn, cid: str, offer: str) -> None:
    with conn:
        conn.execute("INSERT INTO signals (signal_id, source, company_name_raw, title, "
                     "role_family, offer_type, first_seen, url, company_id) VALUES "
                     "(?, 'manual', ?, 'Founding SDR', 'sdr', ?, '2026-11-01', ?, ?)",
                     (f"sig-{cid}", NAMES[cid], offer, f"https://{cid}.example/jobs/1", cid))
        conn.execute("UPDATE companies SET best_signal_id = ? WHERE company_id = ?",
                     (f"sig-{cid}", cid))


def diagnosis(conn, cid: str, gap: str | None) -> None:
    claims = [
        {"text": "The pricing page routes to a demo form.", "artifact_kind": "pricing",
         "found": True, "url": f"https://{cid}.example/pricing",
         "quote": "Talk to our team for a custom quote"},
        {"text": "No customers page is linked from the homepage.",
         "artifact_kind": "customers", "found": False, "url": f"https://{cid}.example/"},
    ]
    with conn:
        if gap is None:
            conn.execute("INSERT INTO diagnoses (company_id, prompt_version, attempted, "
                         "abstain_reason) VALUES (?, ?, 0, 'thin: one artifact')",
                         (cid, PROMPT_VERSION))
        else:
            conn.execute("INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, "
                         "confidence, claims, need, secondary, cannot_see) VALUES "
                         "(?, ?, 1, ?, 0.7, ?, 'Target lists built from scratch.', '[\"G4\"]', "
                         "'Actual pipeline volume.')",
                         (cid, PROMPT_VERSION, gap, json.dumps(claims)))


def contact(conn, cid: str) -> None:
    name, title, email, channel = PEOPLE[cid]
    with conn:
        conn.execute("INSERT INTO contacts (company_id, name, title, email, channel, confidence) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (cid, name, title, email, channel, "high" if email else None))


def teardown(conn, cid: str, kind: str, minutes: float | None) -> None:
    with conn:
        conn.execute("INSERT INTO teardowns (company_id, slice_kind, page_slug, review_state, "
                     "review_minutes) VALUES (?, ?, ?, 'approved', ?)",
                     (cid, kind, f"{cid}-k3x9q2zw", minutes))


def slice_rows(conn, cid: str, kind: str, n: int = 3) -> None:
    rows = [{"account": f"Account {i}", "why": f"Opened an office {i}",
             "url": f"https://acct{i}.example/news", "quote": f"We opened office number {i}"}
            for i in range(n)]
    with conn:
        conn.execute("INSERT INTO slices (company_id, kind, builder_version, outcome, title, "
                     "columns, rows, selected) VALUES (?, ?, 'b-1', 'ok', 'Ten accounts', ?, ?, 1)",
                     (cid, kind, json.dumps(["account", "why"]), json.dumps(rows)))


def touch(conn, cid: str, no: int, channel: str, sent: str, **kw) -> None:
    cols = {"company_id": cid, "touch_no": no, "channel": channel, "sent_at": sent,
            "status": "sent", **kw}
    with conn:
        conn.execute(f"INSERT INTO touches ({', '.join(cols)}) VALUES "
                     f"({', '.join('?' for _ in cols)})", tuple(cols.values()))


def sent_company(conn, cid: str, segment: str, offer: str, gap: str | None, kind: str | None,
                 minutes: float | None, channel: str = "email", hand: bool = False) -> None:
    add_company(conn, cid, name=NAMES[cid], domain=f"{cid}.example", segment=segment,
                status="sent", hand=hand)
    signal(conn, cid, offer)
    if gap:
        diagnosis(conn, cid, gap)
    if cid in PEOPLE:
        contact(conn, cid)
    if kind:
        teardown(conn, cid, kind, minutes)
        slice_rows(conn, cid, kind)
    touch(conn, cid, 1, channel, "2026-11-03T04:30:00Z")


@pytest.fixture
def campaign(conn):
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    sent_company(conn, "a2", "in_saas", "candidate", "G2", "role_funnel", 20)
    sent_company(conn, "a3", "us_india_team", "adjacent", "G1", "funnel_compare", 25,
                 channel="linkedin")
    sent_company(conn, "a4", "us_india_team", "adjacent", "G3", "icp_accounts", 15)
    # a Phase 0 hand row: sent_at and a reply outcome, no status, no diagnosis
    add_company(conn, "h1", name=NAMES["h1"], domain="h1.example", segment="in_saas",
                status="sent", hand=True)
    signal(conn, "h1", "adjacent")
    with conn:
        conn.execute("INSERT INTO touches (company_id, touch_no, channel, sent_at, outcome) "
                     "VALUES ('h1', 1, 'email', '2026-10-20T04:00:00Z', 'replied_question')")
    add_company(conn, "d1", name=NAMES["d1"], domain="d1.example")
    diagnosis(conn, "d1", None)
    states.drop("d1", "no_gap", "judge", conn)
    add_company(conn, "d2", name=NAMES["d2"], domain="d2.example")
    states.drop("d2", "no_site_evidence", "capture", conn)
    add_company(conn, "d3", name=NAMES["d3"], domain="d3.example")
    states.drop("d3", "staffing_or_agency", "qualify", conn)
    add_company(conn, "q1", name=NAMES["q1"], domain="q1.example", status="qualified")
    # a4 bounced
    with conn:
        conn.execute("UPDATE touches SET outcome = 'bounced', bounced_on = '2026-11-03' "
                     "WHERE company_id = 'a4'")
        conn.execute("INSERT INTO suppressions (email_or_domain, reason) VALUES "
                     "('tamsin@a4.example', 'bounce')")
        conn.execute("INSERT INTO llm_calls (stage, company_id, prompt_version, provider, model, "
                     "cost_usd) VALUES ('judge', 'a1', 'j-1.0', 'claude', 'm', 0.5)")
    assert RP.reply(conn, "a1", "positive", note="Keen to talk next week", on=date(2026, 11, 5),
                    today=TODAY).outcome == "replied"
    assert RP.reply(conn, "a2", "not_now", note="Hiring is on hold", on=date(2026, 11, 6),
                    today=TODAY).outcome == "replied"
    assert RP.meeting(conn, "a1", held=True, on=date(2026, 11, 10), minutes=30,
                      today=TODAY).outcome == "meeting"
    return conn


def status(conn, cid: str) -> str:
    return conn.execute("SELECT status FROM companies WHERE company_id = ?",
                        (cid,)).fetchone()[0]


# -- replies, meetings, closes -------------------------------------------------------------


@pytest.mark.parametrize("kind", ["positive", "question", "referral", "not_now", "negative"])
def test_reply_records_outcome_and_advances(conn, kind) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    r = RP.reply(conn, "a1", kind, note="Read it", on=date(2026, 11, 5), today=TODAY)
    assert r.outcome == "replied", r.detail
    t = conn.execute("SELECT outcome, reply_at FROM touches WHERE company_id = 'a1'").fetchone()
    assert t["outcome"] == f"replied_{kind}" and t["reply_at"] == "2026-11-05T00:00:00Z"
    assert status(conn, "a1") == "replied"
    row = conn.execute("SELECT type, optout, note FROM replies").fetchone()
    assert tuple(row) == (kind, 0, "Read it")
    again = RP.reply(conn, "a1", kind, note="Read it", on=date(2026, 11, 5), today=TODAY)
    assert again.outcome == "unchanged"
    assert conn.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0] == 0


def test_reply_goes_on_the_latest_sent_touch(conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    touch(conn, "a1", 2, "email-reply", "2026-11-07T04:30:00Z")
    RP.reply(conn, "a1", "question", on=date(2026, 11, 8), today=TODAY)
    rows = {r["touch_no"]: r["outcome"] for r in conn.execute("SELECT * FROM touches")}
    assert rows == {1: None, 2: "replied_question"}


def test_reply_refusals(conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    assert RP.reply(conn, "a1", "positive", on=date(2026, 11, 20), today=TODAY).outcome == \
        "refused"  # future
    assert RP.reply(conn, "a1", "positive", on=date(2026, 11, 1), today=TODAY).outcome == \
        "refused"  # before the send
    r = RP.reply(conn, "a1", "positive", note="Ilsa liked it", today=TODAY)
    assert r.outcome == "refused" and "Ilsa" not in r.detail  # a contact value in the note
    assert RP.reply(conn, "a1", "positive", optout=True, today=TODAY).outcome == "refused"
    add_company(conn, "q1", name=NAMES["q1"], domain="q1.example", status="qualified")
    assert RP.reply(conn, "q1", "positive", today=TODAY).outcome == "refused"  # no sent touch
    assert status(conn, "a1") == "sent"


@pytest.mark.parametrize("kind,flag", [("optout", False), ("negative", True)])
def test_optout_suppresses_address_and_domain_and_refuses_drafts(conn, kind, flag) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    r = RP.reply(conn, "a1", kind, optout=flag, note="Asked to stop", today=TODAY)
    assert r.outcome == "replied"
    sup = {row[0]: row[1] for row in conn.execute("SELECT * FROM suppressions")}
    assert sup == {"ilsa@a1.example": "optout", "a1.example": "optout"}
    text = "\n".join([r.detail, *r.lines])
    assert "ilsa" not in text.lower() and "Ilsa" not in text
    assert any("a1-k3x9q2zw" in line and "delete" in line for line in r.lines)
    c = Q.company_row(conn, "a1")
    assert Q.suppression_problem(conn, c, Q.recipient(conn, "a1")) is not None
    assert conn.execute("SELECT optout FROM replies").fetchone()[0] == 1
    # the same day again changes nothing
    assert RP.reply(conn, "a1", kind, optout=flag, note="Asked to stop",
                    today=TODAY).outcome == "unchanged"


def test_optout_never_suppresses_a_free_mail_domain(conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    with conn:
        conn.execute("UPDATE contacts SET email = 'ilsa.m@gmail.com'")
    RP.reply(conn, "a1", "optout", today=TODAY)
    sup = {row[0] for row in conn.execute("SELECT email_or_domain FROM suppressions")}
    assert sup == {"ilsa.m@gmail.com", "a1.example"}


def test_meeting_and_close_transitions(conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    assert RP.meeting(conn, "a1", held=False, today=TODAY).outcome == "refused"
    r = RP.meeting(conn, "a1", held=True, on=date(2026, 11, 10), minutes=25, today=TODAY)
    assert r.outcome == "meeting" and status(conn, "a1") == "meeting"
    assert RP.meeting(conn, "a1", held=True, on=date(2026, 11, 10),
                      today=TODAY).outcome == "unchanged"
    c = RP.close(conn, "a1", "hiring_process", "Second round on Friday", on=date(2026, 11, 12),
                 today=TODAY)
    assert c.outcome == "closed" and status(conn, "a1") == "closed"
    assert RP.close(conn, "a1", "hiring_process", "Second round on Friday",
                    on=date(2026, 11, 12), today=TODAY).outcome == "unchanged"
    c2 = RP.close(conn, "a1", "paid_project", "Scope sent", on=date(2026, 11, 13), today=TODAY)
    assert c2.outcome == "closed" and "was hiring_process" in c2.detail
    assert tuple(conn.execute("SELECT outcome, next_step FROM closes").fetchone()) == \
        ("paid_project", "Scope sent")
    assert RP.close(conn, "a1", "won", "x", today=TODAY).outcome == "refused"
    assert RP.meeting(conn, "a1", held=True, today=TODAY).outcome == "refused"  # closed


def test_dropped_company_is_refused(conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    states.drop("a1", "conflict", "review", conn)
    for r in (RP.reply(conn, "a1", "positive", today=TODAY),
              RP.meeting(conn, "a1", held=True, today=TODAY),
              RP.close(conn, "a1", "other", "none", today=TODAY)):
        assert r.outcome == "refused" and "dropped" in r.detail


def test_reply_cli(db_file, conn) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    res = CliRunner().invoke(main, ["reply", "a1", "--type", "optout", "--note", "Stop please",
                                    "--db", str(db_file)])
    assert res.exit_code == 0, res.output
    assert "a1-k3x9q2zw" in res.output and "ilsa" not in res.output.lower()
    res = CliRunner().invoke(main, ["meeting", "a1", "--db", str(db_file)])
    assert res.exit_code == 1 and "--held" in res.output


# -- metrics -------------------------------------------------------------------------------


def test_metrics_on_an_invented_campaign(campaign, no_net) -> None:
    m = MX.compute(campaign)
    f = {row["stage"]: row for row in m["funnel"]}
    assert {k: v["reached"] for k, v in f.items()} == {
        "qualified": 8, "diagnosed": 5, "sent": 5, "replied": 3, "meeting": 1}
    assert f["qualified"]["dropped_here"] == {"no_gap": 1, "no_site_evidence": 1}
    assert f["diagnosed"]["dropped_here"] == {}
    assert m["dropped_before_qualified"] == {"staffing_or_agency": 1}
    assert m["drop_rate"]["overall"] == {"n": 2, "of": 8, "rate": 0.25}
    assert m["judge_drop_rate"] == {"n": 1, "of": 5, "rate": 0.2}
    o = m["overall"]
    assert (o["sent"], o["bounced"], o["delivered"]) == (5, 1, 4)
    assert o["reply_rate"] == {"n": 3, "of": 4, "rate": 0.75}
    assert o["positive_reply_rate"] == {"n": 2, "of": 4, "rate": 0.5}
    assert o["meeting_rate"] == {"n": 1, "of": 4, "rate": 0.25}
    assert (o["meetings_held"], o["review_hours"], o["hours_per_meeting"]) == (1, 1.5, 1.5)
    assert m["sends_by_made_by"] == {"engine": 4, "hand": 1}
    assert m["minutes_per_teardown"] == {
        "engine": {"teardowns": 4, "mean": 22.5, "median": 22.5},
        "hand": {"teardowns": 0, "mean": None, "median": None}, "hand_not_recorded": 1}
    assert m["model_spend_usd"] == {"total": 0.5, "per_meeting": 0.5}
    assert m["bounces"] == {"touches": 1, "companies": 1, "sending_stops": 0}
    assert m["optouts"] == {"companies": 0, "suppressions_by_reason": {"bounce": 1}}
    assert m["replies_by_type"]["positive"] == 1 and m["replies_by_type"]["question"] == 1
    assert m["replies_by_type"]["not_now"] == 1


def _split(m, dim):
    return {k: (b["sent"], b["delivered"], b["reply_rate"]["n"], b["positive_reply_rate"]["n"],
                b["meeting_rate"]["n"], b["hours_per_meeting"]) for k, b in m["by"][dim].items()}


def test_metrics_splits(campaign, no_net) -> None:
    m = MX.compute(campaign)
    assert _split(m, "segment") == {"in_saas": (3, 3, 3, 2, 1, 0.83),
                                    "us_india_team": (2, 1, 0, 0, 0, None)}
    assert _split(m, "offer_type") == {"adjacent": (4, 3, 2, 2, 1, 1.17),
                                       "candidate": (1, 1, 1, 0, 0, None)}
    assert _split(m, "gap") == {"G1": (2, 2, 1, 1, 1, 0.92), "G2": (1, 1, 1, 0, 0, None),
                                "G3": (1, 0, 0, 0, 0, None), "(none)": (1, 1, 1, 1, 0, None)}
    assert _split(m, "slice") == {"funnel_compare": (1, 1, 0, 0, 0, None),
                                  "icp_accounts": (2, 1, 1, 1, 1, 0.75),
                                  "role_funnel": (1, 1, 1, 0, 0, None),
                                  "(none)": (1, 1, 1, 1, 0, None)}
    assert _split(m, "role") == {"founder_ceo": (1, 1, 1, 1, 1, 0.5),
                                 "gtm_leader": (1, 1, 1, 0, 0, None),
                                 "manager": (1, 0, 0, 0, 0, None),
                                 "(none)": (2, 2, 1, 1, 0, None)}
    assert _split(m, "channel") == {"email": (4, 3, 3, 2, 1, 1.08),
                                    "linkedin": (1, 1, 0, 0, 0, None)}
    assert _split(m, "made_by") == {"engine": (4, 3, 2, 1, 1, 1.5),
                                    "hand": (1, 1, 1, 1, 0, None)}
    only = MX.compute(campaign, by="segment")
    assert list(only["by"]) == ["segment"]


def test_metrics_cli_is_aggregate_deterministic_and_offline(db_file, campaign, no_net) -> None:
    run = lambda *a: CliRunner().invoke(main, ["metrics", *a, "--db", str(db_file)])  # noqa: E731
    first, second = run(), run()
    assert first.exit_code == 0, first.output
    assert first.output == second.output
    for cid, name in NAMES.items():
        assert name not in first.output
        assert f"{cid}.example" not in first.output
    assert "reply rate           3/4 (75.0%)" in first.output
    assert "hours per meeting    1.5" in first.output
    js = run("--json")
    assert json.loads(js.output)["overall"]["delivered"] == 4
    assert all(n not in js.output for n in NAMES.values())
    assert all(f'"{cid}"' not in js.output for cid in NAMES)
    detail = run("--detail")  # CliRunner is not a terminal
    assert detail.exit_code == 1 and "terminal only" in detail.output


def test_metrics_detail_lists_ids_only(campaign, no_net) -> None:
    m = MX.compute(campaign, detail=True)
    f = {row["stage"]: row for row in m["funnel"]}
    assert f["meeting"]["company_ids"] == ["a1"]
    assert m["dropped_before_qualified_ids"] == ["d3"]
    assert all(n not in json.dumps(m) for n in NAMES.values())


def test_metrics_on_an_empty_database(conn, no_net) -> None:
    m = MX.compute(conn)
    assert m["overall"]["reply_rate"] == {"n": 0, "of": 0, "rate": None}
    assert "(no sends)" in "\n".join(MX.format_text(m))


def test_role_bucket() -> None:
    assert MX.role_bucket("Founder") == "founder_ceo"
    assert MX.role_bucket("Head of Growth") == "gtm_leader"
    assert MX.role_bucket("SDR Team Lead") == "manager"
    assert MX.role_bucket("Account Executive") == "other"
    assert MX.role_bucket(None) == MX.NONE


# -- HubSpot -------------------------------------------------------------------------------


class FakeHubSpot:
    """A HubSpot that keeps records in memory and answers through MockTransport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.records: dict[str, dict[str, dict]] = {"companies": {}, "contacts": {},
                                                    "deals": {}}
        self.assocs: set[tuple[str, str, str, str]] = set()
        self.next_id = 100
        self.status_override: int | None = None
        self.auth: set[str] = set()

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        self.auth.add(req.headers.get("authorization", ""))
        if self.status_override:
            return httpx.Response(self.status_override,
                                  json={"category": "INVALID_AUTHENTICATION",
                                        "message": f"echo {TOKEN} ilsa@a1.example"})
        parts = req.url.path.strip("/").split("/")
        if req.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json={"results": [{
                "id": "777", "label": SY.PIPELINE_LABEL,
                "stages": [{"id": f"s{i}", "label": lab} for i, lab in enumerate(SY.STAGES)]}]})
        if parts[:3] == ["crm", "v3", "objects"]:
            obj = parts[3]
            if len(parts) == 5 and parts[4] == "search":
                flt = body["filterGroups"][0]["filters"][0]
                hits = [rid for rid, props in self.records[obj].items()
                        if props.get(flt["propertyName"]) == flt["value"]]
                return httpx.Response(200, json={"results": [{"id": h} for h in hits[:1]]})
            if len(parts) == 4 and req.method == "POST":
                self.next_id += 1
                rid = str(self.next_id)
                self.records[obj][rid] = dict(body["properties"])
                return httpx.Response(201, json={"id": rid})
            if len(parts) == 5 and req.method == "PATCH":
                self.records[obj][parts[4]].update(body["properties"])
                return httpx.Response(200, json={"id": parts[4]})
        if parts[:3] == ["crm", "v4", "objects"] and req.method == "PUT":
            self.assocs.add((parts[3], parts[4], parts[7], parts[8]))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"category": "OBJECT_NOT_FOUND"})


@pytest.fixture
def hubspot(monkeypatch):
    fake = FakeHubSpot()
    monkeypatch.setattr(HS, "transport", httpx.MockTransport(fake.handler))
    return fake


def client() -> HS.Client:
    return HS.Client(TOKEN, sleep=lambda s: None)


def test_not_configured_without_the_token(monkeypatch, db_file, campaign) -> None:
    monkeypatch.delenv(HS.TOKEN_ENV, raising=False)
    with pytest.raises(HS.NotConfigured):
        HS.Client.from_env()
    res = CliRunner().invoke(main, ["sync", "--db", str(db_file)])
    assert res.exit_code == 1 and "HUBSPOT_TOKEN" in res.output
    dry = CliRunner().invoke(main, ["sync", "--dry-run", "--db", str(db_file)])
    assert dry.exit_code == 0, dry.output
    assert "a1: Meeting held:" in dry.output and "would be upserted" in dry.output
    assert "ilsa" not in dry.output.lower() and "Quillfern" not in dry.output


def test_sync_upserts_and_is_idempotent(hubspot, campaign) -> None:
    ids = SY.select(campaign)
    assert ids == ["a1", "a2", "a3", "a4", "h1"]
    results = SY.sync(campaign, client(), ids)
    assert hubspot.auth == {f"Bearer {TOKEN}"}
    deals = {p["te_company_id"]: p for p in hubspot.records["deals"].values()}
    assert {k: v["dealstage"] for k, v in deals.items()} == {
        "a1": "s3", "a2": "s2", "a3": "s1", "a4": "s1", "h1": "s2"}
    assert deals["a1"]["pipeline"] == "777" and deals["a1"]["made_by"] == "engine"
    assert deals["a1"]["gap_type"] == "G1" and deals["h1"]["made_by"] == "hand"
    assert {p["domain"] for p in hubspot.records["companies"].values()} == {
        f"{c}.example" for c in ids}
    # contacts: a3 has no address, a4's address bounced (suppressed)
    assert sorted(p["email"] for p in hubspot.records["contacts"].values()) == [
        "doran@a2.example", "ilsa@a1.example"]
    assert len(hubspot.assocs) == 5 + 2 * 2
    out = "\n".join(a for r in results for a in r.actions)
    assert "ilsa" not in out.lower() and "Quillfern" not in out
    stored = campaign.execute("SELECT COUNT(*) FROM hubspot_sync").fetchone()[0]
    assert stored == 5 + 2 + 5 + 9
    deal_a1 = campaign.execute("SELECT hubspot_id FROM hubspot_sync WHERE object = 'deal' AND "
                               "te_key = 'a1'").fetchone()[0]
    assert campaign.execute("SELECT hubspot_deal_id FROM touches WHERE company_id = 'a1'"
                            ).fetchone()[0] == deal_a1
    # a re-run changes nothing and sends nothing
    n = len(hubspot.calls)
    again = SY.sync(campaign, client(), ids)
    assert len(hubspot.calls) == n and not any(r.changed for r in again)
    # a stage change sends only that deal's update
    RP.close(campaign, "a1", "paid_project", "Scope sent", on=date(2026, 11, 13), today=TODAY)
    SY.sync(campaign, client(), ids)
    new = hubspot.calls[n:]
    assert [c[0] for c in new] == ["GET", "PATCH"] and deals["a1"]["dealstage"] == "s5"


def test_sync_finds_existing_records_instead_of_duplicating(hubspot, campaign) -> None:
    SY.sync(campaign, client(), ["a1"])
    counts = {k: len(v) for k, v in hubspot.records.items()}
    with campaign:
        campaign.execute("DELETE FROM hubspot_sync")
    r = SY.sync_company(campaign, client(), "a1")
    assert {k: len(v) for k, v in hubspot.records.items()} == counts
    assert "company updated" in r.actions and "deal updated" in r.actions


def test_hubspot_errors_never_carry_the_token_or_a_body(hubspot, campaign) -> None:
    hubspot.status_override = 401
    c = client()
    with pytest.raises(HS.HubSpotError) as e:
        SY.sync(campaign, c, ["a1"])
    msg = str(e.value)
    assert "HTTP 401" in msg and TOKEN not in msg and "ilsa" not in msg
    assert TOKEN not in repr(c)


def test_missing_pipeline_is_named(hubspot, campaign, monkeypatch) -> None:
    def no_pipeline(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json={"results": []})
        return hubspot.handler(req)

    monkeypatch.setattr(HS, "transport", httpx.MockTransport(no_pipeline))
    with pytest.raises(HS.HubSpotError, match="Teardown outreach"):
        SY.sync(campaign, client(), ["a1"])


@pytest.mark.parametrize("inp,stage", [
    (SY.StageInput("approved", False, frozenset(), None, 0, None), "Queued"),
    (SY.StageInput("sent", True, frozenset(), "bounced", 0, None), "Sent"),
    (SY.StageInput("replied", True, frozenset({"question"}), "replied_question", 0, None),
     "Replied"),
    (SY.StageInput("replied", True, frozenset({"negative"}), "replied_negative", 0, None),
     "Closed"),
    (SY.StageInput("replied", True, frozenset({"optout"}), "replied_optout", 0, None), "Closed"),
    (SY.StageInput("meeting", True, frozenset({"positive"}), "replied_positive", 1, None),
     "Meeting held"),
    (SY.StageInput("closed", True, frozenset(), None, 1, "hiring_process"), "In process"),
    (SY.StageInput("closed", True, frozenset(), None, 1, "paid_project"), "Won"),
    (SY.StageInput("closed", True, frozenset(), None, 1, "no_fit"), "Closed"),
    (SY.StageInput("closed", True, frozenset(), None, 0, None), "Closed"),
])
def test_stage_mapping(inp, stage) -> None:
    assert SY.stage_for(inp) == stage
    assert stage in SY.STAGES


# -- prep ----------------------------------------------------------------------------------


def test_prep_sheet(conn, tmp_path, no_net) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    with conn:
        conn.execute("DELETE FROM slices")
    slice_rows(conn, "a1", "icp_accounts", n=12)
    with conn:
        conn.execute("INSERT INTO facts (company_id, fact, value, artifact_kind, quote, "
                     "char_start, extractor_version) VALUES ('a1', 'pricing_model', "
                     "'contact_sales', 'pricing', 'Talk to our team for a custom quote', 10, "
                     "'ex-1.0')")
    RP.reply(conn, "a1", "positive", note="Wants the account list", on=date(2026, 11, 5),
             today=TODAY)
    RP.meeting(conn, "a1", held=True, on=date(2026, 11, 10), today=TODAY)
    with conn:  # a note typed around the checks still never reaches the page
        conn.execute("UPDATE meetings SET note = 'Ilsa Marwick asked about pricing'")
    path = P.write(conn, "a1", out_dir=tmp_path / "prep", today=TODAY)
    text = path.read_text(encoding="utf-8")
    assert path.name == "a1.md"
    for section in ("## The diagnosis and its evidence", "## Secondary gaps not used",
                    "## The full proof slice", "## Open questions (section 7)",
                    "## The facts", "## Touches and replies"):
        assert section in text
    assert "{first_name} (founder_ceo)" in text
    assert "Talk to our team for a custom quote" in text and "- G4" in text
    assert all(f"Account {i}" in text for i in range(12))  # every row, not only ten
    assert "Actual pipeline volume." in text and "Wants the account list" in text
    for v in ("Ilsa", "Marwick", "ilsa@a1.example", "Co-founder"):
        assert v not in text
    assert "[redacted] asked about pricing" in text
    assert P.write(conn, "a1", out_dir=tmp_path / "prep", today=TODAY).read_text(
        encoding="utf-8") == text


def test_prep_cli(db_file, conn, tmp_path) -> None:
    sent_company(conn, "a1", "in_saas", "adjacent", "G1", "icp_accounts", 30)
    res = CliRunner().invoke(main, ["prep", "a1", "--out-dir", str(tmp_path / "p"),
                                    "--db", str(db_file)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "p" / "a1.md").exists()
    bad = CliRunner().invoke(main, ["prep", "nope", "--out-dir", str(tmp_path / "p"),
                                    "--db", str(db_file)])
    assert bad.exit_code == 1
