"""Raw response cache.

Bodies are stored byte for byte under <cache>/<sha256 of body>; parsed output is
never stored as the source. A small index under <cache>/urls/<sha256 of url>.json
records the latest fetch of each URL (status, outcome, body hash, when), so a
repeat fetch of the same URL on the same UTC day is answered without a request.

Derived output (Step 35's extracted text and structure) lives apart from the
raw bodies, under <cache>/<namespace>/<sha256 of its bytes>, so a raw body is
never confused with something parsed from it. Derived files can always be
rebuilt from the raw bodies with no request.

The cache directory resolves like the database: explicit argument, then
$TE_CACHE_DIR, then data/cache.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel

from engine.models import Outcome

DEFAULT_CACHE_DIR = Path("data") / "cache"
CACHE_ENV_VAR = "TE_CACHE_DIR"
INDEX_DIR = "urls"
DERIVED_NAMESPACES = ("text", "structure")  # extracted text; links/forms JSON (Step 35)


def cache_dir(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(CACHE_ENV_VAR)
    return Path(env) if env else DEFAULT_CACHE_DIR


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CacheEntry(BaseModel):
    """The latest fetch of one URL."""

    url: str
    final_url: str
    kind: str
    status: int | None
    outcome: Outcome
    sha256: str | None  # body hash; None when there was no response body
    content_type: str | None = None
    truncated: bool = False
    detail: str = ""
    fetched_at: datetime  # UTC

    def same_day(self, day: date) -> bool:
        return self.fetched_at.astimezone(UTC).date() == day


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Cache:
    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = cache_dir(root)

    def body_path(self, sha: str) -> Path:
        return self.root / sha

    def _index_path(self, url: str) -> Path:
        return self.root / INDEX_DIR / f"{sha256_hex(url.encode('utf-8'))}.json"

    def put_body(self, body: bytes) -> str:
        sha = sha256_hex(body)
        p = self.body_path(sha)
        if not p.exists():
            _atomic_write(p, body)
        return sha

    def get_body(self, sha: str) -> bytes | None:
        p = self.body_path(sha)
        return p.read_bytes() if p.exists() else None

    def derived_path(self, namespace: str, sha: str) -> Path:
        if namespace not in DERIVED_NAMESPACES:
            raise ValueError(f"unknown derived namespace: {namespace!r}")
        return self.root / namespace / sha

    def put_derived(self, namespace: str, data: bytes) -> str:
        sha = sha256_hex(data)
        p = self.derived_path(namespace, sha)
        if not p.exists():
            _atomic_write(p, data)
        return sha

    def get_derived(self, namespace: str, sha: str) -> bytes | None:
        """The derived bytes, or None if missing or not matching their hash."""
        p = self.derived_path(namespace, sha)
        if not p.is_file():
            return None
        data = p.read_bytes()
        return data if sha256_hex(data) == sha else None

    def record(self, entry: CacheEntry) -> None:
        _atomic_write(self._index_path(entry.url), entry.model_dump_json().encode("utf-8"))

    def latest(self, url: str) -> CacheEntry | None:
        p = self._index_path(url)
        if not p.exists():
            return None
        return CacheEntry.model_validate_json(p.read_bytes())

    def lookup(self, url: str, day: date) -> CacheEntry | None:
        """The entry for url if it was fetched on `day` (UTC) and its body is intact."""
        entry = self.latest(url)
        if entry is None or not entry.same_day(day):
            return None
        if entry.sha256 is not None and not self.body_path(entry.sha256).exists():
            return None
        return entry
