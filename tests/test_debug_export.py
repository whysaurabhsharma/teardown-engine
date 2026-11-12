"""te debug export: redacted record, no contacts, idempotent. Invented data only."""

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.debug import REDACTED, RAW_TEXT_NOTE, ExportError, export_company
from engine.guard.cache import Cache
from engine.models import (
    Artifact,
    Claim,
    Contact,
    Diagnosis,
    Fact,
    Signal,
    Teardown,
    Touch,
)
from tests.conftest import add_company

WHEN = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
CONTACT_STRINGS = (
    "Ilsa Marrowfield", "Ilsa", "Marrowfield", "ilsa@zentrico.example",
    "Head of Revenue Operations", "https://zentrico.example/team/ilsa-m",
)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def record(conn: sqlite3.Connection, cache: Cache) -> str:
    cid = add_company(conn)
    add_company(conn, "quillbay-co", name="Quillbay")
    body = (
        "<h1>Team</h1><p>Ilsa Marrowfield, Head of Revenue Operations, "
        "ilsa@zentrico.example. Ilsandra and Marrowfields stay.</p>"
        "<p>Ask Bo the VP. Odran Pell works elsewhere.</p>"
        "<p>caf\xe9</p>"
    ).encode("utf-8") + b"\xff"
    sha = cache.put_body(body)
    with conn:
        db.insert(conn, Contact(
            company_id=cid, name="Ilsa Marrowfield", title="Head of Revenue Operations",
            email="ilsa@zentrico.example", source_url="https://zentrico.example/team/ilsa-m",
            channel="email",
        ))
        db.insert(conn, Contact(company_id=cid, name="Bo", title="VP"))  # under 3 chars
        db.insert(conn, Contact(company_id="quillbay-co", name="Odran Pell"))
        db.insert(conn, Signal(
            signal_id="gh-1", source="greenhouse", company_name_raw="Zentrico",
            title="Founding GTM Engineer", first_seen=date(2026, 1, 2), company_id=cid,
        ))
        db.insert(conn, Artifact(
            company_id=cid, kind="team", url="https://zentrico.example/team",
            outcome="ok", fetched_at=WHEN, text_sha256=sha, cache_path=str(cache.body_path(sha)),
        ))
        db.insert(conn, Artifact(
            company_id=cid, kind="pricing", url="https://zentrico.example/pricing",
            outcome="ok", fetched_at=WHEN, text_sha256="0" * 64,
        ))
        db.insert(conn, Artifact(
            company_id=cid, kind="careers", url="https://zentrico.example/careers",
            outcome="blocked", fetched_at=WHEN,
        ))
        db.insert(conn, Fact(
            company_id=cid, fact="revops_owner", value="Ilsa Marrowfield",
            artifact_kind="team", quote="ILSA MARROWFIELD, Head of Revenue Operations",
            char_start=14, extractor_version="x1",
        ))
        db.insert(conn, Diagnosis(
            company_id=cid, prompt_version="j1", attempted=True, gap_type="G2",
            confidence=0.7, need="A CRM owner; Marrowfield runs it alone.",
            cannot_see="Internal tooling.",
            claims=[Claim(text="Ops sits with Ilsa", artifact_kind="team", found=True,
                          url="https://zentrico.example/team", quote="Ilsa Marrowfield")],
        ))
        db.insert(conn, Teardown(
            company_id=cid, page_slug="zentrico-x", email_subject="For {first_name}",
            email_body="Hi Ilsa, the teardown is attached.",
        ))
        db.insert(conn, Touch(
            company_id=cid, touch_no=1, channel="email", sent_at=WHEN, hubspot_deal_id="D-77",
        ))
    return cid


def _all_text(dest: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(dest.rglob("*")) if p.is_file())


def test_export_writes_every_section(conn: sqlite3.Connection, record: str, cache: Cache,
                                     tmp_path: Path) -> None:
    dest = export_company(conn, record, tmp_path / "debug", cache)
    assert dest == tmp_path / "debug" / record
    data = json.loads((dest / "record.json").read_text(encoding="utf-8"))
    assert data["company"]["company_id"] == record
    assert [s["signal_id"] for s in data["signals"]] == ["gh-1"]
    assert len(data["facts"]) == 1
    assert data["diagnoses"][0]["claims"][0]["artifact_kind"] == "team"
    assert data["teardowns"][0]["email_subject"] == "For {first_name}"
    assert len(data["touches"]) == 1
    assert "contacts" not in data

    team, pricing, careers = data["artifacts"]
    assert team["text_file"] == f"artifacts/{team['artifact_id']}.txt"
    assert team["text_note"] == RAW_TEXT_NOTE
    assert "cache_path" not in team
    assert pricing["text_file"] is None and pricing["text_note"] == "body not in cache"
    assert careers["text_file"] is None and careers["text_note"] == "no body recorded"
    text = (dest / team["text_file"]).read_text(encoding="utf-8")
    assert "<h1>Team</h1>" in text and "caf\xe9" in text and "�" in text


def test_export_removes_every_contact_value(conn: sqlite3.Connection, record: str,
                                            cache: Cache, tmp_path: Path) -> None:
    dest = export_company(conn, record, tmp_path / "debug", cache)
    everything = _all_text(dest)
    low = everything.lower()
    for s in CONTACT_STRINGS:
        assert s.lower() not in low.replace("ilsandra", "").replace("marrowfields", "")
    assert REDACTED in everything
    # whole-word only: longer words that contain a name part survive
    assert "Ilsandra" in everything and "Marrowfields" in everything
    # contact-derived touch field dropped; channel and other fields kept
    data = json.loads((dest / "record.json").read_text(encoding="utf-8"))
    assert "hubspot_deal_id" not in data["touches"][0]
    assert data["touches"][0]["channel"] == "email"
    assert data["teardowns"][0]["email_body"] == f"Hi {REDACTED}, the teardown is attached."
    assert data["facts"][0]["value"] == REDACTED
    assert data["facts"][0]["quote"] == f"{REDACTED}, {REDACTED}"


def test_export_is_scoped_to_the_company(conn: sqlite3.Connection, record: str,
                                         cache: Cache, tmp_path: Path) -> None:
    # Another company's contacts are not used for this export, and its rows are absent.
    dest = export_company(conn, record, tmp_path / "debug", cache)
    data = json.loads((dest / "record.json").read_text(encoding="utf-8"))
    assert "quillbay-co" not in json.dumps(data)
    text = _all_text(dest)
    assert "Odran Pell" in text  # another company's contact is not this export's concern
    assert "Ask Bo the VP." in text  # values under 3 chars are left, as in the gateway guard


def test_export_is_idempotent(conn: sqlite3.Connection, record: str, cache: Cache,
                              tmp_path: Path) -> None:
    out = tmp_path / "debug"
    dest = export_company(conn, record, out, cache)
    first = {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    (dest / "artifacts" / "stale.txt").write_text("old", encoding="utf-8")
    export_company(conn, record, out, cache)
    second = {p.relative_to(dest): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    assert first == second


def test_unknown_company_and_unsafe_id_are_refused(conn: sqlite3.Connection,
                                                   tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        export_company(conn, "nope-co", tmp_path / "debug")
    with pytest.raises(ExportError):
        export_company(conn, "../escape", tmp_path / "debug")
    assert not (tmp_path / "debug").exists()


def test_cli_debug_export(db_file: Path, record: str, cache: Cache, tmp_path: Path) -> None:
    out = tmp_path / "dbg"
    args = ["debug", "export", record, "--db", str(db_file), "--out", str(out),
            "--cache-dir", str(cache.root)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert (out / record / "record.json").exists()
    for s in CONTACT_STRINGS:
        assert s.lower() not in result.output.lower()

    missing = CliRunner().invoke(main, ["debug", "export", "nope-co", "--db", str(db_file),
                                        "--out", str(out)])
    assert missing.exit_code == 1
