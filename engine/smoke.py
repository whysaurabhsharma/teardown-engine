"""Smoke test: check every external provider the engine depends on.

Each check runs in its own daemon thread with a hard timeout and never raises.
No API keys are used and .env is not read.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

HTTP_TIMEOUT = 10.0  # seconds, per request
CHECK_TIMEOUT = 30.0  # seconds, hard ceiling per check
USER_AGENT = "teardown-engine-smoke/0.1"
DATA_DIR = Path("data")

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/leverdemo?mode=json&limit=1"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/ashby"
PUBLIC_PAGE_URL = "https://example.com/"

RATE_HEADERS = (
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "ratelimit-policy",
    "retry-after",
)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _one_line(text: object, limit: int = 160) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _rate_info(headers: httpx.Headers) -> str:
    found = [f"{h}={headers[h]}" for h in RATE_HEADERS if h in headers]
    return "; ".join(found) if found else "no rate limit reported"


def _get(url: str) -> httpx.Response:
    with httpx.Client(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = client.get(url)
    resp.raise_for_status()
    return resp


# --- checks: each returns a detail string on success, raises on failure ---


def check_claude() -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("`claude` not found on PATH")
    proc = subprocess.run(
        [exe, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    out = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"exit {proc.returncode}: {out or 'no output'}")
    if not out:
        raise RuntimeError("no output from `claude --version`")
    return f"{out.splitlines()[0]}; no rate limit reported (subscription limits not exposed)"


def check_greenhouse() -> str:
    resp = _get(GREENHOUSE_URL)
    jobs = resp.json().get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("response has no 'jobs' list")
    return f"{len(jobs)} jobs on test board; {_rate_info(resp.headers)}"


def check_lever() -> str:
    resp = _get(LEVER_URL)
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("response is not a list of postings")
    return f"{len(data)} posting(s) on test board; {_rate_info(resp.headers)}"


def check_ashby() -> str:
    resp = _get(ASHBY_URL)
    jobs = resp.json().get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError("response has no 'jobs' list")
    return f"{len(jobs)} jobs on test board; {_rate_info(resp.headers)}"


def check_https() -> str:
    resp = _get(PUBLIC_PAGE_URL)
    if not resp.text:
        raise RuntimeError("empty body")
    return f"HTTP {resp.status_code}, {len(resp.content)} bytes; {_rate_info(resp.headers)}"


def check_playwright() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(timeout=15_000)
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                raise RuntimeError(
                    "chromium not installed; run `uv run playwright install chromium`"
                ) from None
            raise
        try:
            page = browser.new_page()
            page.goto("about:blank", timeout=10_000)
            version = browser.version
        finally:
            browser.close()
    return f"chromium {version} opened about:blank"


def check_sqlite() -> str:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f".smoke-{os.getpid()}-{uuid.uuid4().hex[:8]}.db"
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            conn.commit()
            (value,) = conn.execute("SELECT x FROM t").fetchone()
        finally:
            conn.close()
        if value != 42:
            raise RuntimeError(f"read back {value!r}, expected 42")
    finally:
        path.unlink(missing_ok=True)
    if path.exists():
        raise RuntimeError(f"could not delete {path}")
    return f"sqlite {sqlite3.sqlite_version}; created and dropped a db in {DATA_DIR}/"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("claude-cli", check_claude),
    ("greenhouse", check_greenhouse),
    ("lever", check_lever),
    ("ashby", check_ashby),
    ("https", check_https),
    ("playwright", check_playwright),
    ("sqlite", check_sqlite),
]


def run_check(name: str, fn: Callable[[], str], timeout: float = CHECK_TIMEOUT) -> Result:
    """Run one check with a hard timeout. Never raises."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["detail"] = fn()
        except BaseException as exc:  # noqa: BLE001 - a check must never raise
            box["error"] = exc

    try:
        t = threading.Thread(target=target, name=f"smoke-{name}", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return Result(name, False, f"timed out after {timeout:g}s")
        if "error" in box:
            exc = box["error"]
            return Result(name, False, _one_line(f"{type(exc).__name__}: {exc}"))
        return Result(name, True, _one_line(box.get("detail", "")))
    except Exception as exc:  # noqa: BLE001
        return Result(name, False, _one_line(f"{type(exc).__name__}: {exc}"))


def run_all(
    checks: list[tuple[str, Callable[[], str]]] | None = None,
    timeout: float = CHECK_TIMEOUT,
) -> list[Result]:
    return [run_check(name, fn, timeout) for name, fn in (checks or CHECKS)]


def format_result(r: Result, width: int) -> str:
    return f"{r.name:<{width}}  {'OK' if r.ok else 'FAIL':<4}  {r.detail}"
