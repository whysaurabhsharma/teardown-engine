"""Step 88: the CI workflow is valid YAML and runs tests, the demo and verify on
ubuntu and windows with Python 3.12 and uv, using no secrets."""

from __future__ import annotations

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_runs_tests_demo_and_verify() -> None:
    text = CI.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert "secrets." not in text
    # YAML 1.1 reads the bare key `on` as True
    assert doc.get("on", doc.get(True)) is not None
    job = doc["jobs"]["test"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    steps = job["steps"]
    uv = next(s for s in steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv"))
    assert uv["with"]["python-version"] == "3.12" and uv["with"]["enable-cache"] is True
    runs = [s["run"] for s in steps if "run" in s]
    assert runs[0].startswith("uv sync")
    for cmd in ("uv run pytest -q", "uv run te demo", "uv run te verify --skip-tests"):
        assert cmd in runs, cmd
    assert runs.index("uv run pytest -q") < runs.index("uv run te demo")
