"""`te report anonymise-check PATH...` (docs/PLAN.md Step 84).

Scans files (a directory is walked) for any value engine/sensitive.py takes
from the database: company names and domains, contact names, emails and
titles, and reply, meeting and close notes. Prints file:line and the kind of
value, never the value, and fails when anything is found. The rule it
enforces: no company names, no people, no quotes from replies, unless a
company gave written permission (and then that file is not for this check).

Also run on the report's own outputs by `te report all`, so the numbers and
figures are checked, not only built, to be aggregate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from engine.sensitive import Terms

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


@dataclass(frozen=True)
class FileHit:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}"


def files_in(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file()
                          and not SKIP_DIRS.intersection(f.relative_to(p).parts))
        else:
            out.append(p)
    return out


def scan_text(label: str, text: str, terms: Terms) -> list[FileHit]:
    return [FileHit(label, h.line, h.kind) for h in terms.scan(text)]


def scan_files(paths: Iterable[Path], terms: Terms) -> list[FileHit]:
    hits: list[FileHit] = []
    for f in files_in(paths):
        text = f.read_bytes().decode("utf-8", errors="ignore")
        hits += scan_text(str(f), text, terms)
    return hits
