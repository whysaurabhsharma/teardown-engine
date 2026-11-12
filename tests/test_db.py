import re
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from engine import db
from engine.cli import main
from engine.guard import backup as bk
from tests.conftest import add_company


def _copy_migrations(src: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.glob("*.sql"):
        (dest / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _objects(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )
    return {(t, n, re.sub(r"\s+", " ", s or "").strip()) for t, n, s in rows}


# --- init ----------------------------------------------------------------------

def test_init_creates_every_table_at_latest_version(tmp_path: Path) -> None:
    path, applied = db.init_db(tmp_path / "engine.db")
    assert path.exists()
    assert [m.version for m in applied] == list(range(1, db.latest_version() + 1))
    conn = db.connect(path)
    assert db.user_version(conn) == db.latest_version()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("signals", "companies", "artifacts", "facts", "contacts", "gold",
              "diagnoses", "teardowns", "touches", "llm_calls", "suppressions"):
        assert t in names
    conn.close()


def test_init_is_idempotent(db_file: Path) -> None:
    _, applied = db.init_db(db_file)
    assert applied == []


def test_schema_sql_matches_migrations(conn: sqlite3.Connection) -> None:
    ref = sqlite3.connect(":memory:")
    ref.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _objects(ref) == _objects(conn)
    ref.close()


def test_connect_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseError):
        db.connect(tmp_path / "nope.db")


def test_default_path_honours_env(tmp_path: Path) -> None:
    assert db.db_path() == tmp_path / "default" / "engine.db"


# --- migrate -------------------------------------------------------------------

def test_migrate_takes_a_backup(db_file: Path, tmp_path: Path) -> None:
    migs = _copy_migrations(db.MIGRATIONS_DIR, tmp_path / "migs")
    n = db.latest_version() + 1
    (migs / f"{n:04d}_add_note.sql").write_text(
        "ALTER TABLE companies ADD COLUMN note TEXT;", encoding="utf-8"
    )
    conn = db.connect(db_file)
    add_company(conn)
    conn.close()

    applied, backup_path = db.migrate(db_file, migrations_dir=migs)

    assert [m.version for m in applied] == [n]
    assert backup_path is not None and backup_path.exists()
    assert backup_path.parent == db_file.parent / "backups"
    # The backup holds the pre-migration database.
    old = sqlite3.connect(backup_path)
    assert db.user_version(old) == n - 1
    assert old.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
    old.close()
    new = db.connect(db_file)
    assert db.user_version(new) == n
    assert "note" in {r["name"] for r in new.execute("PRAGMA table_info(companies)")}
    new.close()


def test_migrate_up_to_date_changes_nothing(db_file: Path) -> None:
    applied, backup_path = db.migrate(db_file)
    assert applied == [] and backup_path is None
    assert bk.list_backups(db_file.parent / "backups") == []


def test_migrate_dry_run_touches_nothing(db_file: Path, tmp_path: Path) -> None:
    migs = _copy_migrations(db.MIGRATIONS_DIR, tmp_path / "migs")
    n = db.latest_version() + 1
    (migs / f"{n:04d}_x.sql").write_text("CREATE TABLE x (a);", encoding="utf-8")
    applied, backup_path = db.migrate(db_file, migrations_dir=migs, dry_run=True)
    assert [m.version for m in applied] == [n] and backup_path is None
    conn = db.connect(db_file)
    assert db.user_version(conn) == n - 1
    conn.close()


def test_failed_migration_rolls_back(db_file: Path, tmp_path: Path) -> None:
    migs = _copy_migrations(db.MIGRATIONS_DIR, tmp_path / "migs")
    n = db.latest_version() + 1
    (migs / f"{n:04d}_bad.sql").write_text(
        "CREATE TABLE half (a);\nTHIS IS NOT SQL;", encoding="utf-8"
    )
    with pytest.raises(sqlite3.Error):
        db.migrate(db_file, migrations_dir=migs)
    conn = db.connect(db_file)
    assert db.user_version(conn) == n - 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half" not in tables
    conn.close()


def test_migration_numbering_gap_rejected(tmp_path: Path) -> None:
    migs = _copy_migrations(db.MIGRATIONS_DIR, tmp_path / "migs")
    (migs / f"{db.latest_version() + 2:04d}_skip.sql").write_text("", encoding="utf-8")
    with pytest.raises(db.DatabaseError):
        db.list_migrations(migs)


def test_database_ahead_of_code_rejected(db_file: Path) -> None:
    conn = db.connect(db_file)
    conn.execute(f"PRAGMA user_version = {db.latest_version() + 5}")
    conn.commit()
    with pytest.raises(db.DatabaseError):
        db.pending(conn)
    conn.close()


# --- backup --------------------------------------------------------------------

def test_backup_keeps_newest_14(db_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "bk"
    made = [bk.backup(db_file, out) for _ in range(17)]
    kept = bk.list_backups(out)
    assert len(kept) == 14
    assert kept == made[-14:]


def test_backup_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(db.DatabaseError):
        bk.backup(tmp_path / "nope.db")


def test_prune_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me", encoding="utf-8")
    for i in range(3):
        (tmp_path / f"engine-2026010{i}T000000000000Z.db").write_bytes(b"")
    deleted = bk.prune(tmp_path, keep=1)
    assert len(deleted) == 2
    assert (tmp_path / "notes.txt").exists()


# --- CLI -----------------------------------------------------------------------

def test_cli_db_init_status_backup_migrate(tmp_path: Path) -> None:
    path = tmp_path / "data" / "engine.db"
    r = CliRunner()

    res = r.invoke(main, ["db", "init", "--db", str(path)])
    assert res.exit_code == 0, res.output
    assert "created" in res.output and path.exists()
    res = r.invoke(main, ["db", "init", "--db", str(path)])
    assert res.exit_code == 0 and "already initialised" in res.output

    conn = db.connect(path)
    add_company(conn, "zentrico-io")
    add_company(conn, "quillmark-dev", status="qualified")
    conn.close()

    res = r.invoke(main, ["status", "--db", str(path)])
    assert res.exit_code == 0, res.output
    lines = dict(line.split() for line in res.output.splitlines())
    assert lines["sourced"] == "1" and lines["qualified"] == "1"
    assert lines["dropped"] == "0" and lines["total"] == "2"

    res = r.invoke(main, ["backup", "--db", str(path)])
    assert res.exit_code == 0, res.output
    assert len(bk.list_backups(path.parent / "backups")) == 1

    res = r.invoke(main, ["db", "migrate", "--db", str(path)])
    assert res.exit_code == 0 and "up to date" in res.output


def test_cli_status_without_db_fails(tmp_path: Path) -> None:
    res = CliRunner().invoke(main, ["status", "--db", str(tmp_path / "none.db")])
    assert res.exit_code == 1
    assert "te db init" in res.output
