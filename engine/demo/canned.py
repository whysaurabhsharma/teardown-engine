"""Canned model outputs for the demo: a judge, a verifier and a drafter that
answer from this file, the same way every run, with no key and no network.

The fake providers stand where gateway.PROVIDERS["claude"] (judge, drafter)
and ["gemini"] (verifier) stand in a real run, so every call still goes
through llm.gateway.call: the contacts guard, the held-out guard, the schema
check and the llm_calls ledger all run as they do for real. The verifier is a
different family (google) from the judge (anthropic), as CLAUDE.md requires.

Which answer to give is read from the prompt's "Company: <name>" line (the
company block both the judge and the drafter prompts carry). The demo
contacts never reach a prompt: the gateway would refuse the call.
"""

from __future__ import annotations

import re
from typing import Any

from engine.llm.base import ProviderError, ProviderResult

V_HOME = "https://vantorell.example/"
V_PRICING = "https://vantorell.example/pricing"
V_JOB = "https://job-boards.greenhouse.io/vantorell/jobs/7001"
V_JOB_Q = "Set up HubSpot properties and pipelines as the first sales hire."
V_PRICING_Q = "Talk to our team for a custom quote"


def _claim(kind: str, url: str, quote: str | None, text: str) -> dict[str, Any]:
    return {"text": text, "artifact_kind": kind, "found": quote is not None, "url": url,
            "quote": quote}


JUDGE: dict[str, dict[str, Any]] = {
    "Vantorell": {
        "attempted": True, "gap_type": "G3", "confidence": 0.9,
        "need": "Tooling and process around the first sales hire.",
        "claims": [
            _claim("job_post", V_JOB, V_JOB_Q,
                   "The post asks the first sales hire to set up the CRM pipeline."),
            _claim("pricing", V_PRICING, V_PRICING_Q,
                   "The larger plan routes buyers to a custom quote."),
            _claim("product", V_HOME, None, "No product page is linked from the site."),
        ],
        "secondary": ["G5"], "abstain_reason": None,
        "cannot_see": "Whether a CRM is already set up.",
    },
    "Corvessa": {
        "attempted": False, "gap_type": None, "confidence": 0.8, "need": None,
        "claims": [], "secondary": [],
        "abstain_reason": ("no_evidence: the Account Executive joins an SDR team with a "
                           "documented playbook, and pricing, trial and product pages agree"),
        "cannot_see": None,
    },
}

VERDICT = {"verdict": "supported", "reason": "The quoted line says what the claim says."}

DRAFT: dict[str, dict[str, Any]] = {
    "Vantorell": {
        "section3": [
            {"sentence": "The Vantorell job post asks the first sales hire to set up HubSpot "
                         "properties and pipelines.", "claim_indices": [0]},
            {"sentence": "The Vantorell pricing page routes larger buyers to a custom quote, "
                         "and no product page is linked from the site.",
             "claim_indices": [1, 2]},
        ],
        "section4": ("Vantorell is hiring its founding SDR now, so the CRM and pipeline work "
                     "lands on that hire in week one."),
        "section5": ["A HubSpot pipeline with stages that match the demo form.",
                     "An account list of clinic chains, scored by location count.",
                     "A weekly report of replies by segment."],
        "section7": ("This is an outside view from public pages. I cannot see whether a CRM "
                     "is already set up."),
        "email": {"observation": "That hire is asked to set up HubSpot pipelines while "
                                 "pricing routes larger buyers to a quote.",
                  "observation_claims": [0, 1],
                  "slice_name": "a table of the open roles beside the site",
                  "subject_observation": "CRM before calls"},
    },
}

_COMPANY = re.compile(r"^Company: (.+)$", re.MULTILINE)


class CannedProvider:
    """A provider (prompt, schema, model) -> ProviderResult that answers from this
    file. role is 'writer' (judge and drafter) or 'verifier'."""

    def __init__(self, role: str, model: str):
        self.role = role
        self.model = model
        self.calls = 0

    def __call__(self, prompt: str, schema: dict, model: str | None) -> ProviderResult:
        self.calls += 1
        props = (schema or {}).get("properties", {})
        if "verdict" in props:
            if self.role != "verifier":
                raise ProviderError("demo: the writer was asked to verify")
            return self._result(VERDICT)
        if self.role != "writer":
            raise ProviderError("demo: the verifier was asked to write")
        m = _COMPANY.search(prompt)
        name = m.group(1).strip() if m else ""
        table = DRAFT if "section3" in props else JUDGE
        if name not in table:
            raise ProviderError(f"demo: no canned answer for {name or 'this prompt'}")
        return self._result(table[name])

    def _result(self, output: Any) -> ProviderResult:
        # tokens and cost are illustrative; the demo spends nothing
        return ProviderResult(output=output, model=self.model, input_tokens=0,
                              output_tokens=0, cost_usd=0.0)
