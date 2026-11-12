"""`te verify` (and `make verify`): the checks that must pass before a commit.

    tests       the test suite passes (sys.executable -m pytest -q)
    schema      a database built from migrations sits at the newest migration
                number, and engine/schema.sql describes the same schema
    tracked     git tracks nothing under data/, site/ or debug/, and no .env,
                .env.* or keys.txt anywhere
    domains     no company domain from the database appears in a tracked file

Each check returns (name, ok, detail). Details name files and domains only,
never file contents.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engine import db as dbmod

FORBIDDEN_DIRS = ("data/", "site/", "debug/")
FORBIDDEN_NAMES = ("keys.txt", ".env")
MAX_LISTED = 10
_SCHEMA_HEADER_VERSION = re.compile(r"\(version (\d+)\)")


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    more = len(items) - MAX_LISTED
    return shown + (f" (+{more} more)" if more > 0 else "")


def _git_ls_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


# --- checks ------------------------------------------------------------------------

def check_tests(root: Path) -> CheckResult:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    summary = lines[-1] if lines else f"pytest exited {proc.returncode}"
    return CheckResult("tests", proc.returncode == 0, summary)


def _objects(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )
    return {(t, n, re.sub(r"\s+", " ", s or "").strip()) for t, n, s in rows}


def check_schema(
    migrations_dir: Path | None = None, schema_path: Path | None = None
) -> CheckResult:
    schema_path = schema_path or dbmod.SCHEMA_PATH
    try:
        newest = dbmod.latest_version(migrations_dir)
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = dbmod.init_db(Path(tmp) / "verify.db", migrations_dir)
            conn = dbmod.connect(path)
            try:
                built_version = dbmod.user_version(conn)
                built = _objects(conn)
            finally:
                conn.close()
        schema_sql = schema_path.read_text(encoding="utf-8")
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            described = _objects(ref)
        finally:
            ref.close()
    except (dbmod.DatabaseError, sqlite3.Error, OSError) as e:
        return CheckResult("schema", False, f"error: {e}")

    if built_version != newest:
        return CheckResult(
            "schema", False, f"built database at version {built_version}, newest migration {newest}"
        )
    m = _SCHEMA_HEADER_VERSION.search(schema_sql)
    if m and int(m.group(1)) != newest:
        return CheckResult(
            "schema", False, f"{schema_path.name} header says version {m.group(1)}, newest migration {newest}"
        )
    if built != described:
        diff = sorted({n for _, n, _ in built ^ described})
        return CheckResult(
            "schema", False, f"{schema_path.name} differs from migrations in: {_listed(diff)}"
        )
    return CheckResult("schema", True, f"version {newest}; {schema_path.name} matches")


def forbidden_tracked(paths: list[str]) -> list[str]:
    bad = []
    for p in paths:
        low = p.replace("\\", "/").lower()
        base = low.rsplit("/", 1)[-1]
        if (
            any(low.startswith(d) for d in FORBIDDEN_DIRS)
            or base in FORBIDDEN_NAMES
            or base.startswith(".env.")
        ):
            bad.append(p)
    return bad


def check_tracked(root: Path) -> CheckResult:
    try:
        paths = _git_ls_files(root)
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("tracked", False, f"git ls-files failed: {e}")
    bad = forbidden_tracked(paths)
    if bad:
        return CheckResult("tracked", False, f"must not be tracked: {_listed(bad)}")
    return CheckResult("tracked", True, f"{len(paths)} tracked files; none under data/, site/, debug/; no .env or keys.txt")


def normalise_domain(domain: str) -> str:
    d = domain.strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return d[4:] if d.startswith("www.") else d


def _domain_pattern(domain: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9-])" + re.escape(domain) + r"(?![a-z0-9-])")


def company_domains(db_file: Path) -> list[str]:
    conn = dbmod.connect(db_file)
    try:
        rows = conn.execute("SELECT domain FROM companies WHERE domain IS NOT NULL").fetchall()
    finally:
        conn.close()
    return sorted({normalise_domain(r[0]) for r in rows if r[0] and normalise_domain(r[0])})


def check_domains(root: Path, db_file: Path | None = None) -> CheckResult:
    path = dbmod.db_path(db_file)
    if not path.exists():
        return CheckResult("domains", True, "no database, skipped")
    try:
        domains = company_domains(path)
        files = _git_ls_files(root)
    except (dbmod.DatabaseError, sqlite3.Error) as e:
        return CheckResult("domains", False, f"could not read companies: {e}")
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("domains", False, f"git ls-files failed: {e}")
    if not domains:
        return CheckResult("domains", True, f"no company domains in database; {len(files)} files")
    patterns = [(d, _domain_pattern(d)) for d in domains]
    hits = []
    for f in files:
        fp = root / f
        if not fp.is_file():
            continue
        text = fp.read_bytes().decode("utf-8", errors="ignore").lower()
        hits.extend(f"{f}: {d}" for d, p in patterns if p.search(text))
    if hits:
        return CheckResult("domains", False, f"company domain in tracked file: {_listed(hits)}")
    return CheckResult("domains", True, f"{len(domains)} domains, {len(files)} tracked files, no match")


def run_all(
    root: Path, db_file: Path | None = None, skip_tests: bool = False
) -> list[CheckResult]:
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("tests", lambda: check_tests(root)),
        ("schema", check_schema),
        ("tracked", lambda: check_tracked(root)),
        ("domains", lambda: check_domains(root, db_file)),
    ]
    results = []
    for name, fn in checks:
        if name == "tests" and skip_tests:
            results.append(CheckResult("tests", True, "skipped (--skip-tests)"))
            continue
        try:
            results.append(fn())
        except Exception as e:  # a crashing check is a failing check
            results.append(CheckResult(name, False, f"error: {type(e).__name__}: {e}"))
    return results


def format_result(r: CheckResult, width: int = 7) -> str:
    return f"{r.name:<{width}}  {'OK' if r.ok else 'FAIL':<4}  {r.detail}"
