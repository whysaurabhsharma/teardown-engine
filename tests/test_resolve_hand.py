"""Step 30: Phase 0 companies entered by hand (engine/resolve/hand.py, `te company add`).

Companies are invented. No contact fields exist on this path. No network, no model.
"""

from __future__ import annotations

import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.resolve import hand
from tests.conftest import add_company

TODAY = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: object, **k: object) -> None:
        raise AssertionError("hand add must not open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def entry(**kw: object) -> hand.HandEntry:
    base: dict[str, object] = dict(
        company="Quillmark", domain="https://www.quillmark.example/", segment="in_saas",
        role="Founding GTM Engineer", role_url="https://quillmark.example/careers/gtm",
        offer="candidate", sent_on=date(2026, 11, 3))
    base.update(kw)
    return hand.HandEntry(**base)  # type: ignore[arg-type]


def counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # type: ignore[return-value]
                 for t in ("companies", "signals", "touches", "contacts"))


def test_add_creates_company_signal_touch_at_sent(conn: sqlite3.Connection) -> None:
    res = hand.add(conn, entry(reply="positive", reply_on=date(2026, 11, 5)), today=TODAY)
    assert res.created and res.company_id == "quillmark-example"
    c = conn.execute("SELECT * FROM companies").fetchone()
    assert (c["domain"], c["status"], c["hand"], c["pilot"], c["segment"]) == (
        "quillmark.example", "sent", 1, 0, "in_saas")
    assert c["best_signal_id"] == res.signal_id
    s = conn.execute("SELECT * FROM signals").fetchone()
    assert (s["source"], s["company_id"], s["role_family"], s["offer_type"]) == (
        "manual", "quillmark-example", "gtm_engineer", "candidate")
    t = conn.execute("SELECT * FROM touches").fetchone()
    assert (t["touch_no"], t["channel"], t["sent_at"], t["outcome"], t["reply_at"]) == (
        1, "email", "2026-11-03T00:00:00Z", "replied_positive", "2026-11-05T00:00:00Z")
    assert counts(conn) == (1, 1, 1, 0)  # nothing in contacts


def test_add_is_idempotent_by_domain(conn: sqlite3.Connection) -> None:
    hand.add(conn, entry(), today=TODAY)
    before = [tuple(r) for r in conn.execute("SELECT * FROM touches")]
    res = hand.add(conn, entry(domain="quillmark.example"), today=TODAY)
    assert not res.created and res.changes == [] and res.warnings == []
    assert counts(conn) == (1, 1, 1, 0)
    assert [tuple(r) for r in conn.execute("SELECT * FROM touches")] == before


def test_reply_added_later_fills_touch(conn: sqlite3.Connection) -> None:
    hand.add(conn, entry(), today=TODAY)
    res = hand.add(conn, entry(reply="negative"), today=TODAY)
    assert res.changes == ["touch 1 outcome"]
    assert conn.execute("SELECT outcome FROM touches").fetchone()[0] == "replied_negative"
    res = hand.add(conn, entry(reply="positive"), today=TODAY)
    assert res.changes == [] and "unchanged" in res.warnings[0]
    assert conn.execute("SELECT status FROM companies").fetchone()[0] == "sent"


def test_reply_types_are_step_77s(conn: sqlite3.Connection) -> None:
    assert hand.REPLIES == ("positive", "question", "referral", "not_now", "negative", "optout")
    with pytest.raises(hand.HandError, match="reply must be one of"):
        hand.add(conn, entry(reply="neutral"), today=TODAY)


def test_optout_suppresses_the_domain_once(conn: sqlite3.Connection) -> None:
    res = hand.add(conn, entry(reply="optout"), today=TODAY)
    assert "suppression quillmark.example (optout)" in res.changes
    assert conn.execute("SELECT outcome FROM touches").fetchone()[0] == "replied_optout"
    rows = [tuple(r) for r in conn.execute("SELECT email_or_domain, reason FROM suppressions")]
    assert rows == [("quillmark.example", "optout")]
    again = hand.add(conn, entry(reply="optout"), today=TODAY)
    assert again.changes == []
    assert conn.execute("SELECT count(*) FROM suppressions").fetchone()[0] == 1
    assert counts(conn)[3] == 0  # nothing in contacts


def test_other_replies_do_not_suppress(conn: sqlite3.Connection) -> None:
    hand.add(conn, entry(reply="not_now"), today=TODAY)
    assert conn.execute("SELECT count(*) FROM suppressions").fetchone()[0] == 0


def test_pipeline_company_refused_without_adopt(conn: sqlite3.Connection) -> None:
    add_company(conn, "quillmark-example", name="Quillmark", domain="quillmark.example",
                status="qualified", segment="in_saas")
    with pytest.raises(hand.HandError, match="--adopt"):
        hand.add(conn, entry(), today=TODAY)
    res = hand.add(conn, entry(adopt=True), today=TODAY)
    c = conn.execute("SELECT status, hand FROM companies").fetchone()
    assert (c["status"], c["hand"]) == ("sent", 1)
    assert "status qualified -> sent" in res.changes


def test_adopt_refuses_pilot_and_dropped(conn: sqlite3.Connection) -> None:
    add_company(conn, "quillmark-example", name="Quillmark", domain="quillmark.example",
                status="qualified", segment="in_saas", pilot=True)
    with pytest.raises(hand.HandError, match="pilot"):
        hand.add(conn, entry(adopt=True), today=TODAY)


def test_unplaceable_title_needs_family(conn: sqlite3.Connection) -> None:
    with pytest.raises(hand.HandError, match="--family"):
        hand.add(conn, entry(role="Quillmark Wrangler"), today=TODAY)
    res = hand.add(conn, entry(role="Quillmark Wrangler", family="growth"), today=TODAY)
    assert res.role_family == "growth"


def test_offer_mismatch_warns_and_keeps_typed(conn: sqlite3.Connection) -> None:
    res = hand.add(conn, entry(role="Account Executive", offer="candidate"), today=TODAY)
    assert any("adjacent" in w for w in res.warnings)
    assert conn.execute("SELECT offer_type FROM signals").fetchone()[0] == "candidate"


def test_bad_inputs(conn: sqlite3.Connection) -> None:
    with pytest.raises(hand.HandError):
        hand.add(conn, entry(domain="not a domain"), today=TODAY)
    with pytest.raises(hand.HandError):
        hand.add(conn, entry(role_url="ftp://x"), today=TODAY)
    with pytest.raises(hand.HandError):
        hand.add(conn, entry(reply_on=date(2026, 11, 4)), today=TODAY)
    assert counts(conn) == (0, 0, 0, 0)


def test_cli_company_add(db_file: Path) -> None:
    args = ["company", "add", "--hand", "--company", "Quillmark", "--domain",
            "quillmark.example", "--segment", "us_india_team", "--role", "SDR",
            "--role-url", "https://jobs.lever.co/quillmark/1", "--offer", "adjacent",
            "--sent-on", "2026-11-03", "--reply", "question", "--db", str(db_file)]
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output
    assert "created quillmark-example" in r.output
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0 and "unchanged quillmark-example" in r.output
    r = CliRunner().invoke(main, [a for a in args if a != "--hand"])
    assert r.exit_code == 1 and "--hand" in r.output
    conn = db.connect(db_file)
    assert counts(conn) == (1, 1, 1, 0)
    conn.close()


def test_cli_has_no_contact_options() -> None:
    r = CliRunner().invoke(main, ["company", "add", "--help"])
    for opt in ("--contact", "--recipient", "--email", "--person", "--name", "--title"):
        assert opt not in r.output
