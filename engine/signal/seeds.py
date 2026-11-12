"""The hand-kept seed list for the Indian SaaS path (docs/PLAN.md Step 23).

Format. `data/seeds.csv` is a UTF-8 CSV (an Excel BOM is fine) whose header
row names at least the four columns `company, site, careers_url, source`, in
any order; extra columns are ignored. One row per company. `company` is the
name as you would write it, `site` is the company's home page and
`careers_url` the page that lists its open roles, both full http(s) URLs
(e.g. https://zentrico.example and https://zentrico.example/careers).
`source` says where you found the company (a public list, funding news, a
community, your network) and may be blank. Blank rows are skipped. Two rows
for the same site (compared by host, ignoring case and a leading "www.") are
an error, as is a careers_url on LinkedIn or Naukri, which code never reads
(protocol/CONDUCT.md). The loader reports every problem with its line number
and loads nothing until the file is clean. docs/seeds.example.csv shows the
shape with invented companies; the real file lives under data/ and is never
committed.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SEEDS = Path("data") / "seeds.csv"
REQUIRED_COLUMNS = ("company", "site", "careers_url", "source")
FORBIDDEN_HOSTS = ("linkedin.com", "naukri.com")


class SeedsError(ValueError):
    """The seed file is missing columns or has bad rows; every problem is listed."""

    def __init__(self, path: str | Path, problems: list[str]):
        self.problems = problems
        super().__init__(f"{path}: {len(problems)} problem(s)\n" + "\n".join(
            f"  {p}" for p in problems))


@dataclass(frozen=True)
class Seed:
    company: str
    site: str
    careers_url: str
    source: str
    line: int = 0

    @property
    def domain(self) -> str:
        return site_domain(self.site)


def site_domain(url: str) -> str:
    """Host of a URL, lower-cased, without a leading 'www.'."""
    host = (urlsplit(url.strip()).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _url_problem(value: str) -> str | None:
    if not value:
        return "is blank"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"is not an http(s) URL: {value!r}"
    if any(c.isspace() for c in value):
        return f"contains whitespace: {value!r}"
    return None


def _forbidden(url: str) -> bool:
    host = site_domain(url)
    return any(host == h or host.endswith("." + h) for h in FORBIDDEN_HOSTS)


def parse_seeds(lines: list[str], path: str | Path = "<seeds>") -> list[Seed]:
    """Validate and parse seed CSV text (already split into lines)."""
    reader = csv.DictReader(lines)
    header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise SeedsError(path, [f"header is missing column(s): {', '.join(missing)} "
                                f"(need {', '.join(REQUIRED_COLUMNS)})"])
    reader.fieldnames = header
    problems: list[str] = []
    seeds: list[Seed] = []
    first_line_of: dict[str, int] = {}
    for row in reader:
        line = reader.line_num
        values = {c: (row.get(c) or "").strip() for c in REQUIRED_COLUMNS}
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        bad = False
        if not values["company"]:
            problems.append(f"line {line}: company is blank")
            bad = True
        for col in ("site", "careers_url"):
            p = _url_problem(values[col])
            if p:
                problems.append(f"line {line}: {col} {p}")
                bad = True
        if not bad and _forbidden(values["careers_url"]):
            problems.append(f"line {line}: careers_url is on LinkedIn or Naukri, which code "
                            "never reads; add those roles by hand with `te signal add`")
            bad = True
        if bad:
            continue
        domain = site_domain(values["site"])
        if domain in first_line_of:
            problems.append(f"line {line}: duplicate site {domain} "
                            f"(first on line {first_line_of[domain]})")
            continue
        first_line_of[domain] = line
        seeds.append(Seed(line=line, **values))
    if problems:
        raise SeedsError(path, problems)
    return seeds


def load_seeds(path: str | Path = DEFAULT_SEEDS) -> list[Seed]:
    """Read and validate a seed file. Raises FileNotFoundError or SeedsError."""
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_seeds(text.splitlines(keepends=True), path)
