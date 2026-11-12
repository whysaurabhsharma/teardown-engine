"""Claude Code in non-interactive mode, called as a subprocess.

    claude -p --output-format json --json-schema <schema> [--model <model>]

The prompt goes on standard input; the command is an argument list, never a
shell string. No --bare: the call uses the subscription login and loads
CLAUDE.md and the skills. The result is the `structured_output` field of the
JSON response; a missing or null field means no valid result.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from engine.llm.base import NotConfigured, ProviderError, ProviderResult, QuotaError

PROVIDER = "claude"
EXECUTABLE = "claude"
TIMEOUT = 600.0  # seconds; a judge call reads a long prompt
REPO_ROOT = Path(__file__).resolve().parents[2]  # run here so CLAUDE.md and skills load
EXCERPT = 300

_QUOTA_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|quota|too many requests|\b429\b", re.IGNORECASE
)
# A .cmd/.bat shim (npm's claude.cmd on Windows) is run through cmd.exe, which
# re-reads the command line. These characters would be interpreted there.
_CMD_UNSAFE = re.compile(r'[%^&|<>!\r\n]')


def schema_text(schema: dict[str, Any]) -> str:
    """Compact, ASCII-only JSON for the --json-schema argument."""
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=True)


def build_args(executable: str, schema: dict[str, Any], model: str | None = None) -> list[str]:
    text = schema_text(schema)
    if Path(executable).suffix.lower() in {".cmd", ".bat"} and _CMD_UNSAFE.search(text):
        raise ProviderError(
            "schema contains a character cmd.exe would interpret (% ^ & | < > !); "
            "it cannot be passed safely through the claude.cmd shim"
        )
    args = [executable, "-p", "--output-format", "json", "--json-schema", text]
    if model:
        args += ["--model", model]
    return args


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _tokens(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    # Input counts everything the model read, cached or not.
    inp = sum(
        _int(usage.get(k))
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return inp, _int(usage.get("output_tokens"))


def _model(payload: dict[str, Any], requested: str | None) -> str:
    usage = payload.get("modelUsage")
    if isinstance(usage, dict) and usage:
        # Claude Code may use a small model for side tasks; report the main one.
        def weight(item: tuple[str, Any]) -> int:
            v = item[1]
            return _int(v.get("outputTokens")) if isinstance(v, dict) else 0

        return max(usage.items(), key=weight)[0]
    return requested or "claude-default"


def _cost(payload: dict[str, Any]) -> float | None:
    cost = payload.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost)
    return None


def run(
    prompt: str,
    schema: dict[str, Any],
    model: str | None = None,
    *,
    timeout: float = TIMEOUT,
    executable: str | None = None,
) -> ProviderResult:
    """One headless Claude Code call. Raises QuotaError, NotConfigured or ProviderError."""
    exe = executable or shutil.which(EXECUTABLE)
    if not exe:
        raise NotConfigured("`claude` is not on PATH")
    args = build_args(exe, schema, model)
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=REPO_ROOT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"claude timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise NotConfigured(f"cannot run {exe}") from exc

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        if _QUOTA_RE.search(stdout + "\n" + stderr):
            raise QuotaError("claude: usage or rate limit reached")
        raise ProviderError(
            f"claude exited {proc.returncode} without a JSON result: {(stderr or stdout)[:EXCERPT]}"
        )

    result_text = payload.get("result") if isinstance(payload.get("result"), str) else ""
    subtype = str(payload.get("subtype") or "")
    if payload.get("is_error") or proc.returncode != 0:
        if _QUOTA_RE.search(f"{result_text}\n{subtype}\n{stderr}"):
            raise QuotaError("claude: usage or rate limit reached")
        if "structured_output" not in subtype:
            raise ProviderError(
                f"claude reported an error ({subtype or 'exit ' + str(proc.returncode)}): "
                f"{(result_text or stderr)[:EXCERPT]}"
            )

    inp, out = _tokens(payload)
    structured = payload.get("structured_output")
    problem = None
    if structured is None:
        problem = "the response had no structured_output, so there is no valid result"
    return ProviderResult(
        output=structured,
        model=_model(payload, model),
        input_tokens=inp,
        output_tokens=out,
        cost_usd=_cost(payload),
        problem=problem,
    )
