"""DNS facts (docs/PLAN.md Step 37; protocol/HAND_NOTES.md "What the extractors
must learn"; protocol/GAPS.md G6). Built first: the one hand teardown rested
on public DNS alone.

Read from the dns artifact's extracted text, one rendered line per answer
(engine/capture/extract_text.py dns_lines):

    TXT example.com "v=spf1 include:_spf.google.com -all"
    MX example.com 1 aspmx.l.google.com
    CNAME em.example.com u123.wl.sendgrid.net
    NODATA TXT _dmarc.example.com
    NXDOMAIN em.example.com
    FAILED TXT sg.example.com: timeout after 3s

Every DNS fact's quote is one of those lines verbatim (quote_source text), or,
for "no sending subdomain resolves" and "no DKIM selector resolves", the
contiguous block of NXDOMAIN/NODATA lines that shows it.

A name is seen when it has at least one answer, NXDOMAIN or NODATA and no
FAILED line; a FAILED lookup is unseen, so no absence is written from it.

| Fact               | Value                                                      |
| ------------------ | ---------------------------------------------------------- |
| email_provider     | google, microsoft, other; none (found False) on no MX      |
| spf_senders        | {"senders": [...], "ips": [...], "all": "-all", "tools": [...], "records": n}; none (found False) |
| dmarc_policy       | {"p", "sp", "pct", "rua", "ruf"}; none (found False)       |
| sending_subdomains | one row per resolving subdomain ("em"); none (found False) |
| dkim_selectors     | one row per resolving selector ("google"); none (found False) |
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.capture import bundle as B
from engine.extract.base import Art, Bundle, FactOut, absent, host_matches, patterns

KIND = "dns"


@dataclass(frozen=True)
class Line:
    text: str
    start: int
    status: str  # answer | nodata | nxdomain | failed
    rtype: str
    name: str
    value: str  # TXT unquoted; MX "prio host"; CNAME/A target


_ANSWER = re.compile(r"^([A-Z0-9]+) (\S+) (.*)$")


def parse(text: str) -> list[Line]:
    out: list[Line] = []
    pos = 0
    for raw in text.split("\n"):
        start, pos = pos, pos + len(raw) + 1
        parts = raw.split(" ")
        if parts[0] == "NXDOMAIN" and len(parts) == 2:
            out.append(Line(raw, start, "nxdomain", "", parts[1].lower(), ""))
        elif parts[0] == "NODATA" and len(parts) == 3:
            out.append(Line(raw, start, "nodata", parts[1], parts[2].lower(), ""))
        elif parts[0] == "FAILED" and len(parts) >= 3:
            out.append(Line(raw, start, "failed", parts[1], parts[2].rstrip(":").lower(), ""))
        elif (m := _ANSWER.match(raw)) is not None:
            rtype, name, value = m.groups()
            if rtype == "TXT" and len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            out.append(Line(raw, start, "answer", rtype, name.lower(), value))
    return out


def domain_of(b: Bundle, art: Art) -> str:
    d = ((art.structure or {}).get("dns") or {}).get("domain") or ""
    if not d and art.url.startswith("dns:"):
        d = art.url[4:]
    return (d or b.domain or "").strip().lower().rstrip(".")


def _for(lines: list[Line], name: str, rtype: str | None = None) -> list[Line]:
    """Lines about name; NXDOMAIN applies to every type."""
    return [x for x in lines if x.name == name
            and (rtype is None or x.status == "nxdomain" or x.rtype == rtype)]


def _name_state(lines: list[Line], name: str, rtype: str | None = None) -> str:
    """resolves | absent | unseen."""
    mine = _for(lines, name, rtype)
    if any(x.status == "answer" for x in mine):
        return "resolves"
    if not mine or any(x.status == "failed" for x in mine):
        return "unseen"
    return "absent"


def _fo(fact: str, value, line: Line, *, found: bool = True) -> FactOut:
    return FactOut(fact, value, KIND, line.text, "text", found, line.start)


def _absence_line(fact: str, lines: list[Line], name: str, rtype: str) -> FactOut | None:
    """'none' quoting the NODATA/NXDOMAIN line, or None if the lookup failed."""
    mine = _for(lines, name, rtype)
    if not mine or any(x.status == "failed" for x in mine):
        return None
    neg = next((x for x in mine if x.status in ("nodata", "nxdomain")), None)
    return _fo(fact, "none", neg, found=False) if neg else None


def _setup(b: Bundle) -> tuple[Art, str, list[Line]] | None:
    if b.state(KIND) != "ok":
        return None
    art = b.arts[KIND]
    return art, domain_of(b, art), parse(art.text or "")


def _tool_for(kind: str, value: str) -> str | None:
    for t in patterns()["tools"]:
        if any(host_matches(value, p) for p in t.get(kind) or []):
            return t["id"]
    return None


# -- facts -------------------------------------------------------------------------------


def email_provider(b: Bundle) -> list[FactOut]:
    got = _setup(b)
    if got is None:
        return []
    _, d, lines = got
    answers = [x for x in _for(lines, d, "MX") if x.status == "answer"]
    if not answers:
        f = _absence_line("email_provider", lines, d, "MX")
        return [f] if f else []

    def prio(x: Line) -> tuple[int, int]:
        p = x.value.split()[0] if x.value.split() else ""
        return (int(p) if p.isdigit() else 1 << 16, x.start)

    best = min(answers, key=prio)
    host = (best.value.split()[-1] if best.value.split() else "").lower().rstrip(".")
    if not host:  # null MX ("0 .")
        return [_fo("email_provider", "none", best, found=False)]
    if host_matches(host, "google.com") or host_matches(host, "googlemail.com"):
        v = "google"
    elif host_matches(host, "outlook.com"):
        v = "microsoft"
    else:
        v = "other"
    return [_fo("email_provider", v, best)]


_SENDER_MECH = re.compile(r"^(?:include:|a$|a:|a/|mx$|mx:|mx/|exists:|redirect=|ptr)", re.I)


def parse_spf(record: str) -> dict:
    senders, ips = [], []
    qualifier_all = None
    for tok in record.split()[1:]:
        low = tok.lower()
        bare = low.lstrip("+-~?")
        if bare == "all":
            qualifier_all = tok if tok[0] in "+-~?" else "+all"
        elif bare.startswith(("ip4:", "ip6:")):
            ips.append(tok)
        elif _SENDER_MECH.match(bare):
            senders.append(tok)
    tools = []
    for s in senders:
        if s.lower().lstrip("+-~?").startswith("include:"):
            t = _tool_for("spf_includes", s.split(":", 1)[1])
            if t and t not in tools:
                tools.append(t)
    return {"senders": senders, "ips": ips, "all": qualifier_all, "tools": tools}


def spf_senders(b: Bundle) -> list[FactOut]:
    got = _setup(b)
    if got is None:
        return []
    _, d, lines = got
    root_txt = _for(lines, d, "TXT")
    spf = [x for x in root_txt if x.status == "answer" and x.value.lower().startswith("v=spf1")]
    if spf:
        value = {**parse_spf(spf[0].value), "records": len(spf)}
        return [_fo("spf_senders", value, spf[0])]
    if any(x.status == "answer" for x in root_txt):
        if any(x.status == "failed" for x in root_txt):
            return []
        return [absent("spf_senders", "none", KIND, "v=spf1 record")]
    f = _absence_line("spf_senders", lines, d, "TXT")
    return [f] if f else []


def parse_dmarc(record: str) -> dict:
    tags: dict[str, str] = {}
    for part in record.split(";"):
        k, sep, v = part.partition("=")
        if sep:
            tags.setdefault(k.strip().lower(), v.strip())
    pct = tags.get("pct")
    return {"p": (tags.get("p") or "").lower() or None,
            "sp": (tags.get("sp") or "").lower() or None,
            "pct": int(pct) if pct and pct.isdigit() else None,
            "rua": bool(tags.get("rua")), "ruf": bool(tags.get("ruf"))}


def dmarc_policy(b: Bundle) -> list[FactOut]:
    got = _setup(b)
    if got is None:
        return []
    _, d, lines = got
    name = f"_dmarc.{d}"
    mine = _for(lines, name, "TXT")
    rec = [x for x in mine if x.status == "answer" and x.value.lower().startswith("v=dmarc1")]
    if rec:
        return [_fo("dmarc_policy", parse_dmarc(rec[0].value), rec[0])]
    if any(x.status == "answer" for x in mine):
        if any(x.status == "failed" for x in mine):
            return []
        return [absent("dmarc_policy", "none", KIND, "v=DMARC1 record")]
    f = _absence_line("dmarc_policy", lines, name, "TXT")
    return [f] if f else []


def _resolving(fact: str, lines: list[Line], names: list[tuple[str, str]],
               rtype: str | None) -> list[FactOut]:
    """One fact per label whose name resolves; 'none' quoting the block of
    negative lines when every name was seen and none resolves."""
    out: list[FactOut] = []
    states = {label: _name_state(lines, name, rtype) for label, name in names}
    for label, name in names:
        if states[label] == "resolves":
            first = next(x for x in _for(lines, name, rtype) if x.status == "answer")
            out.append(_fo(fact, label, first))
    if out or any(s == "unseen" for s in states.values()):
        return out
    wanted = {name for _, name in names}
    block = [x for x in lines if x.name in wanted]
    lo, hi = block[0], block[-1]
    between = [x for x in lines if lo.start <= x.start <= hi.start]
    if between != block:  # the negative lines are not contiguous: quote nothing
        return []
    text = "\n".join(x.text for x in block)
    return [FactOut(fact, "none", KIND, text, "text", False, lo.start)]


def sending_subdomains(b: Bundle) -> list[FactOut]:
    got = _setup(b)
    if got is None:
        return []
    _, d, lines = got
    names = [(s, f"{s}.{d}") for s in B.SENDING_SUBDOMAINS]
    return _resolving("sending_subdomains", lines, names, None)


def dkim_selectors(b: Bundle) -> list[FactOut]:
    got = _setup(b)
    if got is None:
        return []
    _, d, lines = got
    names = [(s, f"{s}._domainkey.{d}") for s in B.DKIM_SELECTORS]
    return _resolving("dkim_selectors", lines, names, "TXT")


EXTRACTORS = (
    ("email_provider", email_provider, (KIND,)),
    ("spf_senders", spf_senders, (KIND,)),
    ("dmarc_policy", dmarc_policy, (KIND,)),
    ("sending_subdomains", sending_subdomains, (KIND,)),
    ("dkim_selectors", dkim_selectors, (KIND,)),
)
