"""SQLite system of record: connection, init and numbered migrations.

The schema version lives in PRAGMA user_version. Migrations are files named
NNNN_name.sql in engine/migrations/, applied in order, each in one transaction
that also sets user_version. `migrate` takes a backup before applying anything.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

DEFAULT_DB_PATH = Path("data") / "engine.db"
DB_ENV_VAR = "TE_DB"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class DatabaseError(RuntimeError):
    """A database file is missing, malformed or ahead of the code."""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


def db_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the database path: explicit argument, then $TE_DB, then data/engine.db."""
    if path is not None:
        return Path(path)
    env = os.environ.get(DB_ENV_VAR)
    return Path(env) if env else DEFAULT_DB_PATH


def connect(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open an existing database with foreign keys on and rows as sqlite3.Row."""
    p = db_path(path)
    if not p.exists():
        raise DatabaseError(f"no database at {p}; run `te db init`")
    return _open(p)


def _open(p: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def list_migrations(migrations_dir: Path | None = None) -> list[Migration]:
    """Numbered migrations in order. Numbers must run 1, 2, 3... with no gaps."""
    d = migrations_dir or MIGRATIONS_DIR
    found: list[Migration] = []
    for f in d.iterdir():
        if f.suffix != ".sql":
            continue
        m = _MIGRATION_RE.match(f.name)
        if not m:
            raise DatabaseError(f"badly named migration: {f.name}")
        found.append(Migration(int(m.group(1)), f))
    found.sort(key=lambda m: m.version)
    for expected, mig in enumerate(found, start=1):
        if mig.version != expected:
            raise DatabaseError(
                f"migrations must be numbered 1..N without gaps; found {mig.name}"
            )
    return found


def latest_version(migrations_dir: Path | None = None) -> int:
    migs = list_migrations(migrations_dir)
    return migs[-1].version if migs else 0


def pending(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[Migration]:
    current = user_version(conn)
    migs = list_migrations(migrations_dir)
    if migs and current > migs[-1].version:
        raise DatabaseError(
            f"database is at version {current}, newer than the code's {migs[-1].version}"
        )
    return [m for m in migs if m.version > current]


def _apply(conn: sqlite3.Connection, mig: Migration) -> None:
    sql = mig.path.read_text(encoding="utf-8")
    # executescript commits any open transaction first; wrap the file and the
    # version bump in one explicit transaction so a failure leaves nothing behind.
    try:
        conn.executescript(
            f"BEGIN;\n{sql}\n;PRAGMA user_version = {mig.version};\nCOMMIT;"
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        raise


def init_db(
    path: str | os.PathLike[str] | None = None, migrations_dir: Path | None = None
) -> tuple[Path, list[Migration]]:
    """Create the database and apply every migration.

    Idempotent: on an existing, initialised database it applies nothing and
    returns an empty list (use `migrate` to bring it up to date).
    """
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    conn = _open(p)
    try:
        if existed and user_version(conn) > 0:
            return p, []
        applied = pending(conn, migrations_dir)
        for mig in applied:
            _apply(conn, mig)
        return p, applied
    finally:
        conn.close()


def migrate(
    path: str | os.PathLike[str] | None = None,
    migrations_dir: Path | None = None,
    backup_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[list[Migration], Path | None]:
    """Apply pending migrations after taking a backup.

    Returns (applied migrations, backup path). Nothing pending means no backup
    and no change. With dry_run, returns what would be applied and touches nothing.
    """
    from engine.guard.backup import backup

    p = db_path(path)
    conn = connect(p)
    try:
        todo = pending(conn, migrations_dir)
    finally:
        conn.close()
    if not todo or dry_run:
        return todo, None
    backup_path = backup(p, backup_dir)
    conn = connect(p)
    try:
        for mig in todo:
            _apply(conn, mig)
    finally:
        conn.close()
    return todo, backup_path


def insert(conn: sqlite3.Connection, row: BaseModel) -> int:
    """Insert a validated row model into its table; returns lastrowid.

    The model class names its table in TABLE and its JSON columns in JSON_FIELDS.
    Fields left as None are omitted so column defaults apply.
    """
    table: str = getattr(row, "TABLE")
    json_fields: frozenset[str] = getattr(row, "JSON_FIELDS", frozenset())
    data = {k: v for k, v in row.model_dump(mode="json").items() if v is not None}
    for key in json_fields & data.keys():
        data[key] = json.dumps(data[key], ensure_ascii=False)
    for key, value in data.items():
        if isinstance(value, bool):
            data[key] = int(value)
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
    return int(cur.lastrowid or 0)
