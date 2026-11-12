import time

from click.testing import CliRunner

from engine import smoke
from engine.cli import main


def _ok() -> str:
    return "fine"


def _boom() -> str:
    raise ValueError("bad\nthing")


def _hang() -> str:
    time.sleep(5)
    return "never"


def test_run_check_ok() -> None:
    r = smoke.run_check("a", _ok)
    assert r.ok and r.detail == "fine"


def test_run_check_exception_is_one_line_fail() -> None:
    r = smoke.run_check("b", _boom)
    assert not r.ok
    assert r.detail == "ValueError: bad thing"


def test_run_check_timeout() -> None:
    r = smoke.run_check("c", _hang, timeout=0.2)
    assert not r.ok
    assert "timed out" in r.detail


def test_cli_exit_codes(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "CHECKS", [("a", _ok), ("b", _ok)])
    result = CliRunner().invoke(main, ["smoke"])
    assert result.exit_code == 0
    assert len(result.output.splitlines()) == 2

    monkeypatch.setattr(smoke, "CHECKS", [("a", _ok), ("b", _boom)])
    result = CliRunner().invoke(main, ["smoke"])
    assert result.exit_code == 1
    assert "b  FAIL" in result.output


def test_sqlite_check_leaves_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path / "data")
    r = smoke.run_check("sqlite", smoke.check_sqlite)
    assert r.ok, r.detail
    assert list((tmp_path / "data").iterdir()) == []
