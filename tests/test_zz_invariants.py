"""Cross-cutting invariants (docs/BUILD_MAP.md 3c). Each test produces the runs it checks, so order does not matter."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from pydantic import TypeAdapter

from cua.config import Settings
from cua.contracts import RunReport, RunResult
from cua.operator import ScriptedOperator
from cua.replay.engine import SENSITIVE_MASK, ReplayEngine

from .conftest import CANARIES, FAST, ROOT

MATRIX = [  # (fault, member, expected bucket)
    ("F1", "M1001", "SUCCESS"),
    ("F1", "M9999", "BUSINESS_OUTCOME"),
    ("F1", "M1002", "FAILURE"),
    ("F7", "M1001", "FAILURE"),
    ("F3", "M1001", "ESCALATED"),
]


@pytest.fixture
def matrix_runs(reset, surface, redactor, golden, tmp_path):
    from .test_acceptance import acknowledge_dialog
    reports = []
    for fault, member, _ in MATRIX:
        reset(fault=fault)
        op = ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")])
        eng = ReplayEngine(surface, runs_dir=tmp_path, redactor=redactor, console=op,
                           settings=Settings.load(**FAST), overlay_root=ROOT)
        reports.append(eng.run(golden("get_savings_balance"), {"member_id": member}, "tenant_a"))
    reset()
    bad = ReplayEngine(surface, runs_dir=tmp_path, redactor=redactor, settings=Settings.load(**FAST),
                       overlay_root=ROOT)
    reports.append(bad.run(golden("get_savings_balance"), {"member_id": "abc"}, "tenant_a"))
    return tmp_path, reports


def test_replay_never_imports_the_llm_client():
    code = ("import sys, cua.replay.engine, cua.surface.playwright_surface, cua.session, cua.operator, cua.metrics;"
            "bad = [m for m in sys.modules if m.startswith('anthropic') or m.startswith('cua.discovery')];"
            "print(bad); sys.exit(1 if bad else 0)")
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    env.pop("ANTHROPIC_API_KEY", None)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_replay_is_deterministic(reset, engine, golden, runs_dir):
    traces = []
    for _ in range(3):
        reset()
        rep = engine().run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
        events = [json.loads(line) for line in (runs_dir / rep.run_id / "events.jsonl").read_text().splitlines()]
        traces.append((rep.result.model_dump_json(), [(e["step"], e["candidate"]) for e in events
                                                      if e["kind"] == "resolved"]))
    assert traces[0] == traces[1] == traces[2]


def test_every_run_ends_in_exactly_one_bucket(matrix_runs):
    runs_dir, reports = matrix_runs
    assert [r.result.bucket for r in reports] == [b for _, _, b in MATRIX] + ["FAILURE"]
    adapter = TypeAdapter(RunResult)
    files = list(runs_dir.glob("*/result.json"))
    assert len(files) == len(reports)
    for p in files:
        body = json.loads(p.read_text())
        adapter.validate_python(body["result"])
        RunReport.model_validate(body)


def test_sensitive_outputs_masked_on_disk_but_returned(matrix_runs):
    runs_dir, reports = matrix_runs
    ok = reports[0]
    assert ok.result.outputs["balance"] == "2450.17"
    on_disk = json.loads((runs_dir / ok.run_id / "result.json").read_text())
    assert on_disk["result"]["outputs"] == {"balance": SENSITIVE_MASK, "currency": "USD"}


def test_no_pii_or_secrets_persisted(matrix_runs):
    runs_dir, _ = matrix_runs
    roots = [runs_dir, ROOT / "artifacts", ROOT / "overlays", ROOT / "evidence", ROOT / "evals"]
    files = [p for root in roots if root.exists() for p in root.rglob("*")
             if p.is_file() and p.suffix in (".jsonl", ".json", ".txt", ".md")]
    assert files
    hits = [(str(p), c) for p in files for c in CANARIES if c in p.read_text()]
    assert hits == []
