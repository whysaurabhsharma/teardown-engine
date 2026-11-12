import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from engine import db
from engine.models import Company


@pytest.fixture(autouse=True)
def _no_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default database at tmp_path so no test can reach data/."""
    monkeypatch.setenv(db.DB_ENV_VAR, str(tmp_path / "default" / "engine.db"))


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path, _ = db.init_db(tmp_path / "data" / "engine.db")
    return path


@pytest.fixture
def conn(db_file: Path) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_file)
    yield c
    c.close()


def add_company(conn: sqlite3.Connection, company_id: str = "zentrico-io", **kw) -> str:
    """Insert an invented company."""
    fields = {"company_id": company_id, "name": "Zentrico", "domain": f"{company_id}.example"}
    fields.update(kw)
    with conn:
        db.insert(conn, Company(**fields))
    return company_id
