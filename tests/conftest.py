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


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests off data/ (cache, teardowns, sender), site/ and .env; no test starts
    with a SENDER_EMAIL or a TEARDOWN_BASE_URL."""
    monkeypatch.setenv("TE_CACHE_DIR", str(tmp_path / "default" / "cache"))
    monkeypatch.setenv("TE_NO_DOTENV", "1")
    monkeypatch.setenv("TE_TEARDOWNS_DIR", str(tmp_path / "default" / "teardowns"))
    monkeypatch.setenv("TE_SENDER", str(tmp_path / "default" / "sender.yaml"))
    monkeypatch.setenv("TE_SITE_DIR", str(tmp_path / "default" / "site"))
    monkeypatch.delenv("TEARDOWN_BASE_URL", raising=False)
    monkeypatch.delenv("SENDER_EMAIL", raising=False)


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


@pytest.fixture
def server() -> Iterator["LocalServer"]:
    """A local HTTP server on 127.0.0.1 with a permissive robots.txt."""
    import threading

    from localserver import LocalServer, _QuietServer, make_handler

    s = LocalServer()
    s.route("/robots.txt", body="User-agent: *\nDisallow: /private\n")
    httpd = _QuietServer(("127.0.0.1", 0), make_handler(s))
    s.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield s
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> str:
    """An invented contact address for the User-Agent."""
    email = "research@zentrico.example"
    monkeypatch.setenv("SENDER_EMAIL", email)
    return email
