"""The sender's details for the teardown page (docs/PLAN.md Step 52).

Where they come from: one small YAML file the user fills, `data/sender.yaml`
(gitignored with the rest of data/; override with $TE_SENDER or --sender). Not
.env: these are page text, not secrets, and they need lists and long lines.

    te teardown init-sender [--force]

writes a skeleton. It pre-fills only what protocol/OFFER.md states (the
sender's name from its version line, the one-line identity and the `adjacent`
offer sentence), read at that moment; nothing else from OFFER.md is read. The
user fills the rest: three lines about the sender, two links, the postal
address. A field still reading TODO fails `te teardown check`.

Fields:
  name            the sender's name (page header; email sign-off in Step 54)
  identity        one line on who the sender is (email footer in Step 54)
  about           section 8: exactly three lines on the sender
  links           section 8: exactly two {label, url} links (http or https)
  offer.candidate section 8 for a `candidate` signal; may use {role}, {company}
  offer.adjacent  section 8 for an `adjacent` signal; may use {role}, {company}
  postal_address  footer of every page (CONDUCT.md: every email and page)
  opt_out         footer: one sentence on how to opt out
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

SENDER_ENV_VAR = "TE_SENDER"
DEFAULT_SENDER_PATH = Path("data") / "sender.yaml"
OFFER_PATH = Path(__file__).resolve().parents[2] / "protocol" / "OFFER.md"
SENDER_VERSION = "sender-1"

TODO = "TODO"
DEFAULT_CANDIDATE = ("I have applied for the {role} role at {company}. These notes are the "
                     "work I would start with.")
DEFAULT_OPT_OUT = 'Not relevant? Reply "no" to my email and I will not write again.'


class SenderError(RuntimeError):
    """The sender file is missing or malformed."""


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    url: str


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: str
    adjacent: str


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = SENDER_VERSION
    name: str
    identity: str
    about: list[str]
    links: list[Link]
    offer: Offer
    postal_address: str
    opt_out: str = DEFAULT_OPT_OUT

    def offer_line(self, offer_type: str, *, role: str, company: str) -> str:
        text = self.offer.candidate if offer_type == "candidate" else self.offer.adjacent
        return text.replace("{role}", role).replace("{company}", company)


def sender_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(SENDER_ENV_VAR)
    return Path(env) if env else DEFAULT_SENDER_PATH


def load(path: str | os.PathLike[str] | None = None) -> Sender:
    p = sender_path(path)
    if not p.exists():
        raise SenderError(f"no sender file at {p}; run `te teardown init-sender` and fill it")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return Sender.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise SenderError(f"{p}: {e}") from e


def problems(s: Sender) -> list[str]:
    """What is unfilled or out of shape. Word limits are checked with the page."""
    out: list[str] = []
    texts = [s.name, s.identity, *s.about, *(lk.label for lk in s.links),
             *(lk.url for lk in s.links), s.offer.candidate, s.offer.adjacent,
             s.postal_address, s.opt_out]
    if any(TODO in t for t in texts):
        out.append("a field still reads TODO")
    if any(not t.strip() for t in texts):
        out.append("a field is blank")
    if len(s.about) != 3:
        out.append(f"about has {len(s.about)} lines; section 8 takes exactly three")
    if len(s.links) != 2:
        out.append(f"links has {len(s.links)} entries; section 8 takes exactly two")
    for lk in s.links:
        if not re.match(r"^https?://\S+$", lk.url) and TODO not in lk.url:
            out.append(f"link {lk.label!r} is not an http(s) URL")
    return out


# -- the skeleton ----------------------------------------------------------------------


def _offer_text() -> str:
    try:
        return OFFER_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _section(text: str, heading: str) -> str:
    """The body of one '## ' section of OFFER.md, up to the next '## ' or '---'."""
    m = re.search(rf"^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |^---|\Z)", text,
                  re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def from_offer(text: str | None = None) -> dict[str, str]:
    """name, identity and the adjacent offer line as OFFER.md states them; missing
    parts are TODO. Reads only those three places."""
    text = _offer_text() if text is None else text
    out = {"name": TODO, "identity": TODO, "adjacent": TODO}
    m = re.search(r"^Version [^\n]*·\s*([^\n·]+?)\s*$", text, re.MULTILINE)
    if m:
        out["name"] = m.group(1).strip()
    body = _section(text, "Who I am, in one line")
    paras = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if paras:
        out["identity"] = " ".join(paras[0].split())
    body = _section(text, "Offer type 2")
    quote = [ln.lstrip("> ").strip() for ln in body.splitlines() if ln.startswith(">")]
    if quote:
        out["adjacent"] = " ".join(" ".join(quote).split())
    return out


def _q(s: str) -> str:
    import json

    return json.dumps(s, ensure_ascii=False)


def skeleton(offer_md: str | None = None) -> str:
    o = from_offer(offer_md)
    return f"""# data/sender.yaml: who sends the teardowns (te teardown init-sender).
# Gitignored. Fill every TODO; `te teardown check` fails while one is left.
# Section 8 (about + offer + link labels) must stay within 60 words, with no
# superlatives about yourself (protocol/TEARDOWN_SPEC.md v1.2).
version: {SENDER_VERSION}
name: {_q(o['name'])}            # page header; email sign-off
identity: {_q(o['identity'])}    # one line; the email footer (Step 54)
about:                            # section 8: exactly three short lines
  - {_q(o['identity'] if o['identity'] != TODO else TODO + ': line 1')}
  - "TODO: line 2"
  - "TODO: line 3"
links:                            # section 8: exactly two
  - {{label: "TODO", url: "TODO: https://..."}}
  - {{label: "TODO", url: "TODO: https://..."}}
offer:                            # {{role}} and {{company}} are filled from the teardown header
  candidate: {_q(DEFAULT_CANDIDATE)}
  adjacent: {_q(o['adjacent'])}
postal_address: "TODO: the postal address printed in every page footer"
opt_out: {_q(DEFAULT_OPT_OUT)}
"""


def init(path: str | os.PathLike[str] | None = None, *, force: bool = False) -> Path:
    p = sender_path(path)
    if p.exists() and not force:
        raise SenderError(f"{p} exists; pass --force to overwrite it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(skeleton(), encoding="utf-8")
    return p
