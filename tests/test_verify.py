"""te verify: schema, tracked-file and domain checks. Temporary git repos only."""

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db, verify
from engine.cli import main
from tests.conftest import add_company


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "engine").mkdir(parents=True)
    (root / "engine" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "README.md").write_text("Example repo about invented.example\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


def _track(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(root, "add", "-f", rel)


# --- schema --------------------------------------------------------------------------

def test_schema_check_passes_on_this_repo() -> None:
    r = verify.check_schema()
    assert r.ok, r.detail
    assert f"version {db.latest_version()}" in r.detail


def test_schema_check_fails_when_schema_sql_drifts(tmp_path: Path) -> None:
    drifted = tmp_path / "schema.sql"
    drifted.write_text(
        db.SCHEMA_PATH.read_text(encoding="utf-8") + "\nCREATE TABLE extra (x TEXT);\n",
        encoding="utf-8",
    )
    r = verify.check_schema(schema_path=drifted)
    assert not r.ok and "extra" in r.detail


def test_schema_check_fails_on_stale_header(tmp_path: Path) -> None:
    stale = tmp_path / "schema.sql"
    stale.write_text(
        db.SCHEMA_PATH.read_text(encoding="utf-8").replace(
            f"(version {db.latest_version()})", "(version 1)"),
        encoding="utf-8",
    )
    r = verify.check_schema(schema_path=stale)
    assert not r.ok and "header" in r.detail


# --- tracked -------------------------------------------------------------------------

def test_tracked_check_passes_on_a_clean_repo(repo: Path) -> None:
    assert verify.check_tracked(repo).ok


@pytest.mark.parametrize("rel", [
    "data/engine.db", "site/index.html", "debug/zentrico-io/record.json",
    ".env", "sub/.env", ".env.local", "keys.txt", "Data/x.txt",
])
def test_tracked_check_fails_on_forbidden_paths(repo: Path, rel: str) -> None:
    _track(repo, rel)
    r = verify.check_tracked(repo)
    assert not r.ok and rel in r.detail


def test_tracked_check_allows_lookalikes(repo: Path) -> None:
    for rel in ("engine/data_utils.py", "docs/site_notes.md", "env.py", "tests/debug_test.py"):
        _track(repo, rel)
    assert verify.check_tracked(repo).ok


def test_tracked_check_fails_outside_git(tmp_path: Path) -> None:
    r = verify.check_tracked(tmp_path / "no-repo-here")
    assert not r.ok


# --- domains -------------------------------------------------------------------------

def test_domains_check_skips_without_a_database(repo: Path, tmp_path: Path) -> None:
    r = verify.check_domains(repo, tmp_path / "missing.db")
    assert r.ok and r.detail == "no database, skipped"


def test_domains_check_passes_when_no_domain_is_tracked(repo: Path, conn, db_file: Path) -> None:
    add_company(conn, "zentrico-io", domain="zentrico.example")
    r = verify.check_domains(repo, db_file)
    assert r.ok, r.detail


@pytest.mark.parametrize("text", [
    "see zentrico.example for more", "HTTPS://WWW.ZENTRICO.EXAMPLE/pricing",
    "mail@zentrico.example", "app.zentrico.example",
])
def test_domains_check_fails_on_a_tracked_domain(repo: Path, conn, db_file: Path, text: str) -> None:
    add_company(conn, "zentrico-io", domain="www.Zentrico.example")
    _track(repo, "notes/leak.md", f"line one\n{text}\n")
    r = verify.check_domains(repo, db_file)
    assert not r.ok
    assert "notes/leak.md: zentrico.example" in r.detail
    assert "line one" not in r.detail  # names the file, never its contents


def test_domains_check_is_whole_label(repo: Path, conn, db_file: Path) -> None:
    add_company(conn, "zentrico-io", domain="zentrico.example")
    _track(repo, "notes/ok.md", "notzentrico.example and zentrico.examples\n")
    assert verify.check_domains(repo, db_file).ok


def test_untracked_files_are_ignored(repo: Path, conn, db_file: Path) -> None:
    add_company(conn, "zentrico-io", domain="zentrico.example")
    (repo / "scratch.txt").write_text("zentrico.example\n", encoding="utf-8")
    assert verify.check_domains(repo, db_file).ok


@pytest.mark.parametrize("raw,norm", [
    ("Zentrico.Example", "zentrico.example"),
    ("https://www.zentrico.example/", "zentrico.example"),
    ("zentrico.example:443", "zentrico.example"),
])
def test_normalise_domain(raw: str, norm: str) -> None:
    assert verify.normalise_domain(raw) == norm


# --- command -------------------------------------------------------------------------

def test_verify_command_ok(repo: Path, tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["verify", "--skip-tests", "--root", str(repo),
                                       "--db", str(tmp_path / "missing.db")])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert [ln.split()[0] for ln in lines] == ["tests", "schema", "tracked", "domains"]
    assert all(ln.split()[1] == "OK" for ln in lines)


def test_verify_command_fails_with_a_line_per_check(repo: Path, conn, db_file: Path) -> None:
    add_company(conn, "zentrico-io", domain="zentrico.example")
    _track(repo, "data/leak.txt", "zentrico.example\n")
    result = CliRunner().invoke(main, ["verify", "--skip-tests", "--root", str(repo),
                                       "--db", str(db_file)])
    assert result.exit_code == 1
    status = {ln.split()[0]: ln.split()[1] for ln in result.output.strip().splitlines()}
    assert status == {"tests": "OK", "schema": "OK", "tracked": "FAIL", "domains": "FAIL"}


def test_tests_check_reports_failure(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
    r = verify.check_tests(root)
    assert not r.ok and "failed" in r.detail
