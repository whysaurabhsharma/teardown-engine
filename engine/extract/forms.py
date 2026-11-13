"""The demo path (docs/PLAN.md Step 37): demo_form_fields and has_booking_step.

demo_form_fields
----------------
The form on the demo_form page (the page behind the homepage's primary CTA)
with the most visible fields, outside nav and footer; with no <form>, the
visible fields outside any form. Visible means not hidden, submit, button,
reset or image (extract_text.NON_FIELD_INPUTS).
Value {"count": n, "fields": [name, label or placeholder of each]}. The quote
is the first field's label when the page text shows it, else its name as the
structure stores it.
- demo_form not_found: "none" (found False, not_found).
- seen, no fields at all: "no_form" (found False, absent). A form drawn by
  script after load is not in the captured HTML; this says what the page
  showed, not that no form exists.

has_booking_step
----------------
True when the demo_form page carries a scheduling tool (patterns.yaml category
scheduling) in a script, iframe, link or form action, or when the homepage's
primary CTA itself points at a booking host. What happens after a form is
submitted cannot be seen from outside, so False means "no scheduling step on
the page", found False.
- demo_form not_found and no booking CTA: False (found False, not_found).
"""

from __future__ import annotations

from typing import Any

from engine.extract import cta as C
from engine.extract.base import (
    Art,
    Bundle,
    FactOut,
    absent,
    best_quote,
    exact_text_quote,
    not_found,
    structure_quote,
)
from engine.extract.stack import tools_in

KIND = "demo_form"


def _fields(art: Art) -> list[dict[str, Any]]:
    s = art.structure or {}
    forms = [f for f in s.get("forms") or [] if f.get("region") not in ("nav", "footer")]
    best = max(forms, key=lambda f: (f.get("field_count") or 0, -f["index"]), default=None)
    if best is not None and (best.get("field_count") or 0) > 0:
        return [x for x in best.get("fields") or [] if x.get("visible")]
    return [x for x in s.get("loose_fields") or [] if x.get("visible")]


def _field_label(x: dict[str, Any]) -> str | None:
    for k in ("name", "label", "placeholder", "aria_label", "id"):
        if x.get(k):
            return str(x[k])
    return None


def demo_form_fields(b: Bundle) -> list[FactOut]:
    st = b.state(KIND)
    if st == "not_found":
        return [not_found("demo_form_fields", "none", KIND)]
    if st != "ok":
        return []
    art = b.arts[KIND]
    fields = _fields(art)
    if not fields:
        return [absent("demo_form_fields", "no_form", KIND, "form field")]
    value = {"count": len(fields), "fields": [_field_label(x) or x.get("type") for x in fields]}
    for x in fields:
        if x.get("label"):
            f = exact_text_quote("demo_form_fields", value, art, x["label"])
            if f is not None:
                return [f]
    for x in fields:
        lab = _field_label(x)
        if lab:
            return [best_quote("demo_form_fields", value, art, lab)]
    return [absent("demo_form_fields", "no_form", KIND, "named form field")]


def has_booking_step(b: Bundle) -> list[FactOut]:
    st = b.state(KIND)
    if st == "ok":
        hits = tools_in(b, KIND, "scheduling")
        if hits:
            return [structure_quote("has_booking_step", True, b.arts[KIND], hits[0][1])]
    link = C.cta_link(b)
    if link is not None and C._booking(link["url"]) and link.get("href"):
        return [structure_quote("has_booking_step", True, b.arts[C.KIND], link["href"])]
    if st == "ok":
        return [absent("has_booking_step", False, KIND, "scheduling embed or link")]
    if st == "not_found":
        return [not_found("has_booking_step", False, KIND)]
    return []


EXTRACTORS = (
    ("demo_form_fields", demo_form_fields, (KIND,)),
    ("has_booking_step", has_booking_step, (KIND,)),
)
