"""`te verify` (and `make verify`): the checks that must pass before a commit.

    tests       the test suite passes (sys.executable -m pytest -q)
    schema      a database built from migrations sits at the newest migration
                number, and engine/schema.sql describes the same schema
    tracked     git tracks nothing under data/, site/ or debug/, and no .env,
                .env.* or keys.txt anywhere
    domains     no company domain from the database appears in a tracked file
                (protocol/HAND_NOTES.md excepted: it is the known item below)
    history     (Step 87) no commit on any ref ever touched data/, site/ or
                debug/ (`git log --all --stat -- data site debug` is empty)
    secrets     no commit ever added a .env, .env.* or keys.txt, or a line
                that looks like a live key (SECRET_PATTERNS)
    leaks       no value from engine/sensitive.py (company name or domain,
                contact name, email or title, a reply note) is in a tracked
                file or in any line any commit ever added. Domains, emails
                and notes fail; names and titles are warnings unless --public
                (a company name can be an everyday word)
    known       protocol/HAND_NOTES.md names a real company by the owner's
                choice: reported as a known item to scrub before publishing
                (Step 90); fails only with --public, and then for as long as
                any commit holds the file (scrubbing means a clean history)

--public (before the repo goes public) turns every warning into a failure.
Without a database (CI), the checks that need one are skipped.

Each check returns (name, ok, detail, warnings). Details name files and
domains only, never file contents. The Step 87 checks name a commit, a path,
a line and the kind of value, never the value.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections import defaultdict
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine import db as dbmod

FORBIDDEN_DIRS = ("data/", "site/", "debug/")
FORBIDDEN_NAMES = ("keys.txt", ".env")
MAX_LISTED = 10
_SCHEMA_HEADER_VERSION = re.compile(r"\(version (\d+)\)")


HAND_NOTES = "protocol/HAND_NOTES.md"
HISTORY_DIRS = ("data", "site", "debug")
# Lines that look like a live credential. Test fixtures use obvious fakes that
# do not fit these shapes (e.g. a HubSpot token that is not a UUID).
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("hubspot_token", re.compile(r"\bpat-(?:na|eu)\d-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                 r"[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("oauth_client_secret", re.compile(r'"client_secret"\s*:\s*"[^"]{10,}"')),
)
_COMMIT_MARK = "\x01"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    warnings: tuple[str, ...] = ()


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
        if _is_known(f) or not fp.is_file():  # the known item: see check_known
            continue
        text = fp.read_bytes().decode("utf-8", errors="ignore").lower()
        hits.extend(f"{f}: {d}" for d, p in patterns if p.search(text))
    if hits:
        return CheckResult("domains", False, f"company domain in tracked file: {_listed(hits)}")
    return CheckResult("domains", True, f"{len(domains)} domains, {len(files)} tracked files, no match")


# --- Step 87: the history ---------------------------------------------------------

def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout
    return out.decode("utf-8", errors="replace")


def _short(sha: str) -> str:
    return sha[:10]


def check_history_dirs(root: Path) -> CheckResult:
    """`git log --all --stat -- data site debug` must print nothing (case-insensitive)."""
    try:
        out = _git(root, "log", "--all", "--format=%H", "--",
                   *(f":(icase){d}" for d in HISTORY_DIRS))
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("history", False, f"git log failed: {e}")
    shas = [ln for ln in out.split() if ln]
    if shas:
        return CheckResult("history", False, f"{len(shas)} commit(s) touched data/, site/ or "
                           f"debug/: {_listed([_short(x) for x in shas])}")
    return CheckResult("history", True, "no commit on any ref touched data/, site/ or debug/")


def _history_paths(root: Path) -> list[tuple[str, str]]:
    """(commit, path) for every path any commit on any ref added or changed."""
    out = _git(root, "log", "--all", "--diff-merges=first-parent", f"--format={_COMMIT_MARK}%H",
               "--name-only", "--no-renames")
    pairs, sha = [], ""
    for ln in out.splitlines():
        if ln.startswith(_COMMIT_MARK):
            sha = ln[1:].strip()
        elif ln.strip():
            pairs.append((sha, ln.strip()))
    return pairs


_HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


def added_lines(root: Path) -> Iterator[tuple[str, str, int, str]]:
    """(commit, path, line_no, text) for every line any commit on any ref added.
    Every line a file ever held was added by some commit, so this covers the
    whole history (root commits included; merges against their first parent)."""
    proc = subprocess.Popen(
        ["git", "-c", "core.quotepath=false", "log", "--all", "-p", "--no-color",
         "--no-ext-diff", "--no-textconv", "--no-renames", "--diff-merges=first-parent",
         "--unified=0", f"--format={_COMMIT_MARK}%H"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    sha, path, line_no = "", "", 0
    try:
        for raw in proc.stdout:
            ln = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if ln.startswith(_COMMIT_MARK):
                sha, path = ln[1:].strip(), ""
            elif ln.startswith("+++ "):
                target = ln[4:]
                path = "" if target == "/dev/null" else (
                    target[2:] if target.startswith("b/") else target)
            elif ln.startswith("@@"):
                m = _HUNK.match(ln)
                line_no = int(m.group(1)) if m else 0
            elif ln.startswith("+") and path:
                yield sha, path, line_no, ln[1:]
                line_no += 1
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, "git log -p")


def check_secrets(root: Path) -> CheckResult:
    try:
        paths = _history_paths(root)
        bad = sorted({f"{_short(c)} {p} env_or_keys_file" for c, p in paths
                      if forbidden_tracked([p])
                      and not any(p.lower().startswith(d + "/") for d in HISTORY_DIRS)})
        bad += sorted({f"{_short(c)} {p}:{n} {kind}" for c, p, n, text in added_lines(root)
                       for kind, rx in SECRET_PATTERNS if rx.search(text)})
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("secrets", False, f"git log failed: {e}")
    if bad:
        return CheckResult("secrets", False, f"in history: {_listed(bad)}")
    return CheckResult("secrets", True, f"no .env, keys.txt or key-shaped line in "
                       f"{len({c for c, _ in paths})} commits")


def _is_known(path: str) -> bool:
    return path.replace("\\", "/") == HAND_NOTES


HARD_KINDS = frozenset({"domain", "contact_email", "note"})


def scan_leaks(root: Path, terms: Any) -> tuple[list[str], list[str], list[str]]:
    """(failures, warnings, known) as 'where kind' strings, never the value.
    terms: an engine.sensitive.Terms."""
    fail: set[str] = set()
    warn: set[str] = set()
    known: set[str] = set()

    def put(where: str, path: str, kinds: set[str]) -> None:
        for k in kinds:
            item = f"{where} {k}"
            if _is_known(path):
                known.add(item)
            elif k in HARD_KINDS:
                fail.add(item)
            else:
                warn.add(item)

    for f in _git_ls_files(root):
        fp = root / f
        if not fp.is_file():
            continue
        text = fp.read_bytes().decode("utf-8", errors="ignore")
        by_line: dict[int, set[str]] = defaultdict(set)
        for h in terms.scan(text):
            by_line[h.line].add(h.kind)
        for n, kinds in by_line.items():
            put(f"worktree {f}:{n}", f, kinds)
    for c, p, n, text in added_lines(root):
        kinds = terms.kinds_in(text)
        if kinds:
            put(f"{_short(c)} {p}:{n}", p, kinds)
    return sorted(fail), sorted(warn), sorted(known)


def check_leaks(root: Path, db_file: Path | None = None, public: bool = False) -> CheckResult:
    from engine import sensitive

    path = dbmod.db_path(db_file)
    if not path.exists():
        return CheckResult("leaks", True, "no database, skipped")
    try:
        conn = dbmod.connect(path)
        try:
            terms = sensitive.terms(conn)
        finally:
            conn.close()
    except (dbmod.DatabaseError, sqlite3.Error) as e:
        return CheckResult("leaks", False, f"could not read the database: {e}")
    if not terms:
        return CheckResult("leaks", True, "no sensitive values in the database")
    try:
        fail, warn, known = scan_leaks(root, terms)
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("leaks", False, f"git failed: {e}")
    extra = (f"; {len(known)} hit(s) in {HAND_NOTES} (see known)" if known else "")
    if public:
        fail, warn = sorted(fail + warn), []
    if fail:
        return CheckResult("leaks", False, f"{len(fail)} hit(s) (commit or worktree, path:line, "
                           f"kind): {_listed(fail)}{extra}", tuple(warn[:MAX_LISTED]))
    if warn:
        return CheckResult("leaks", True, f"{len(terms)} values; {len(warn)} name/title hit(s) "
                           f"to review (fail with --public){extra}", tuple(warn[:MAX_LISTED]))
    return CheckResult("leaks", True, f"{len(terms)} values; tracked files and every line "
                       f"added in history clean{extra}")


def check_known(root: Path, public: bool = False) -> CheckResult:
    try:
        commits = [x for x in _git(root, "log", "--all", "--format=%H", "--",
                                   HAND_NOTES).split() if x]
        tracked = HAND_NOTES in _git_ls_files(root)
    except (subprocess.CalledProcessError, OSError) as e:
        return CheckResult("known", False, f"git failed: {e}")
    if not commits and not tracked:
        return CheckResult("known", True, f"{HAND_NOTES} is not in the repo or its history")
    msg = (f"{HAND_NOTES} names a real company (known item): tracked "
           f"{'yes' if tracked else 'no'}, in {len(commits)} commit(s); scrub it and "
           "publish from a history without it before Step 90")
    if public:
        return CheckResult("known", False, msg)
    return CheckResult("known", True, "1 known item (fails with --public)", (msg,))


def run_all(
    root: Path, db_file: Path | None = None, skip_tests: bool = False, public: bool = False
) -> list[CheckResult]:
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("tests", lambda: check_tests(root)),
        ("schema", check_schema),
        ("tracked", lambda: check_tracked(root)),
        ("domains", lambda: check_domains(root, db_file)),
        ("history", lambda: check_history_dirs(root)),
        ("secrets", lambda: check_secrets(root)),
        ("leaks", lambda: check_leaks(root, db_file, public)),
        ("known", lambda: check_known(root, public)),
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
    state = "FAIL" if not r.ok else ("WARN" if r.warnings else "OK")
    lines = [f"{r.name:<{width}}  {state:<4}  {r.detail}"]
    lines += [f"{'':<{width}}        - {w}" for w in r.warnings]
    return "\n".join(lines)
