"""Step 88: `te demo` runs the whole pipeline on three invented companies, offline.

Every non-loopback socket connect fails the test; the demo folder is under
tmp_path; the working directory is tmp_path, so a stray write to data/ or
site/ would show up there.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.demo import run as D
from engine.demo.sites import COMPANIES
from engine.llm import gateway

LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = socket.socket.connect

    def connect(self, address):  # type: ignore[no-untyped-def]
        if isinstance(address, tuple) and str(address[0]) not in LOOPBACK:
            raise AssertionError(f"the demo tried to reach {address[0]}")
        return real(self, address)

    def create_connection(address, *a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError(f"the demo tried to reach {address[0]}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.chdir(tmp_path)


def test_demo_runs_end_to_end_offline_and_renders_a_teardown(offline, tmp_path) -> None:
    providers = dict(gateway.PROVIDERS)
    before_env = {k: os.environ.get(k) for k in ("SENDER_EMAIL", db.DB_ENV_VAR, "TE_SITE_DIR")}
    lines: list[str] = []
    res = D.run(tmp_path / "demo", echo=lines.append)

    assert res.teardown is not None and res.teardown.is_file()
    assert res.teardown.is_relative_to(tmp_path / "demo" / "site")
    html = res.teardown.read_text(encoding="utf-8")
    assert "Vantorell: notes for your Founding Sales Development Representative hire" in html
    assert "Set up HubSpot properties and pipelines as the first sales hire." in html
    assert "1 Example Lane, Pune 411001" in html

    assert res.statuses == {"vantorell-example": "replied", "corvessa-example": "dropped",
                            "ambrisk-example": "dropped"}
    conn = db.connect(tmp_path / "demo" / "engine.db")
    try:
        drops = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT company_id, drop_reason, drop_stage FROM companies WHERE status = 'dropped'")}
        assert drops == {"corvessa-example": ("no_gap", "judge"),
                         "ambrisk-example": ("no_site_evidence", "capture")}
        calls = dict(conn.execute("SELECT stage, COUNT(*) FROM llm_calls GROUP BY stage"))
        assert calls == {"judge": 2, "verify": 3, "draft": 1}
        writers = {r[0] for r in conn.execute("SELECT DISTINCT provider FROM llm_calls "
                                              "WHERE stage IN ('judge', 'draft')")}
        verifiers = {r[0] for r in conn.execute("SELECT DISTINCT provider FROM llm_calls "
                                                "WHERE stage = 'verify'")}
        assert writers == {"claude"} and verifiers == {"gemini"}
        # robots.txt said no for ambrisk: none of its own pages was fetched
        kinds = dict(conn.execute("SELECT kind, outcome FROM artifacts "
                                  "WHERE company_id = 'ambrisk-example'"))
        assert kinds["homepage"] == "robots_denied"
    finally:
        conn.close()

    text = "\n".join(lines)
    for stage in ("signal", "qualify", "capture", "extract", "judge", "verify", "slice",
                  "draft", "render", "simulate"):
        assert f"\n{stage}" in "\n" + text, stage
    assert "DEMO DATA" in text
    assert any("reply rate" in m and "1/1" in m for m in res.metrics)

    # nothing outside the demo folder; the process is as it was
    assert {p.name for p in tmp_path.iterdir()} <= {"default", "demo"}
    assert not (tmp_path / "data").exists() and not (tmp_path / "site").exists()
    assert dict(gateway.PROVIDERS) == providers
    assert {k: os.environ.get(k) for k in before_env} == before_env


def test_demo_companies_are_invented() -> None:
    for c in COMPANIES:
        assert c.domain.endswith(".example")
        for path in c.pages:
            assert path.startswith("/")


def test_demo_refuses_a_folder_with_files(tmp_path) -> None:
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(D.DemoError, match="not empty"):
        D.run(tmp_path)


def test_cli_demo_prints_teardown_and_metrics(offline, tmp_path) -> None:
    out = tmp_path / "cli-demo"
    r = CliRunner().invoke(main, ["demo", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "teardown: " in r.output and str(out / "site") in r.output
    assert "te metrics on the demo database (DEMO DATA" in r.output
    assert "network connections: 0" in r.output
