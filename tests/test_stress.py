"""Stress matrix: every scenario in tests/scenarios.py must produce exactly its expected outcome."""

from __future__ import annotations

import json

import pytest

from .conftest import CANARIES
from .scenarios import SCENARIOS, run_scenario


@pytest.mark.parametrize("sc", SCENARIOS, ids=[f"{s.id}-{s.category}" for s in SCENARIOS])
def test_stress_scenario(sc, server, tmp_path):
    report, problems = run_scenario(sc, server.base_url, tmp_path)
    assert problems == [], f"{sc.id} {sc.title}: {problems}\n{report.result.model_dump_json(indent=1)}"
    # every run leaves a result, and nothing it wrote carries PII or secrets
    run_dir = tmp_path / report.run_id
    assert json.loads((run_dir / "result.json").read_text())["result"]["bucket"] == sc.bucket
    leaks = [(p.name, c) for p in run_dir.rglob("*") if p.suffix in (".json", ".jsonl", ".txt")
             for c in CANARIES if c in p.read_text()]
    assert leaks == []


def test_separate_handoffs_keep_separate_transition_logs(server, tmp_path):
    sc = next(s for s in SCENARIOS if s.id == "X17")
    report, problems = run_scenario(sc, server.base_url, tmp_path)
    assert problems == []
    first, second = report.result.handoffs
    assert first.transitions[0].startswith("AUTOMATION->PAUSE_REQUESTED")
    assert second.transitions[0].startswith("AUTOMATION->PAUSE_REQUESTED")
    assert len(first.transitions) == len(second.transitions) == 5


def test_slow_irreversible_commit_is_never_resubmitted(server, tmp_path):
    sc = next(s for s in SCENARIOS if s.id == "X28")
    report, problems = run_scenario(sc, server.base_url, tmp_path)
    assert problems == []  # includes: the app holds exactly one new sub-account
    assert "verify the result in the app" in report.result.observed
