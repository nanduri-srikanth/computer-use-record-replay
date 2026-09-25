"""Runtime telemetry: every real run leaves one ledger row with the numbers operations needs."""

from __future__ import annotations

from cua.contracts import LocatorCandidate, LocatorKind
from cua.metrics import cost_usd, read

from .scenarios import SCENARIOS, run_scenario


def _scenario(sid):
    return next(s for s in SCENARIOS if s.id == sid)


def test_every_replay_appends_a_ledger_row(reset, engine, golden, runs_dir):
    before = len(read(runs_dir / "metrics.jsonl"))
    rep = engine().run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    rows = read(runs_dir / "metrics.jsonl")
    assert len(rows) == before + 1
    m = rows[-1]
    assert (m.run_id, m.kind, m.bucket, m.capability) == (rep.run_id, "replay", "SUCCESS", "get_savings_balance")
    assert m.resolutions == 5 and m.fallback_resolutions == 0 and m.ui_actions == 3
    assert set(m.step_seconds) == {"s1", "s2", "s3", "s4", "s5"}


def test_policy_violations_are_counted_by_stage(server, tmp_path):
    for sid, stage in (("X34", "start_page"), ("X35", "mid_flow")):
        run_scenario(_scenario(sid), server.base_url, tmp_path)
    rows = {r.run_id.split("-")[0]: r for r in read(tmp_path / "metrics.jsonl")}
    assert rows["X34"].policy_violations == {"start_page": 1} and rows["X34"].reason == "POLICY_VIOLATION"
    assert rows["X35"].policy_violations == {"mid_flow": 1}


def test_app_permission_denied_is_not_a_policy_violation(server, tmp_path):
    run_scenario(_scenario("X23"), server.base_url, tmp_path)
    m = read(tmp_path / "metrics.jsonl")[-1]
    assert m.reason == "PERMISSION_DENIED" and m.policy_violations == {}


def test_locator_fallback_is_visible_before_anything_fails(reset, engine, golden, runs_dir):
    cap = golden("get_savings_balance")
    s2 = cap.steps[1]
    stale = LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="button", name="Search Members")  # renamed in the UI
    backup = LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="button", name="Search")  # the second rung
    s2 = s2.model_copy(update={"target": s2.target.model_copy(update={"candidates": [stale, backup]})})
    drifted = cap.model_copy(update={"steps": [cap.steps[0], s2, *cap.steps[2:]]})
    rep = engine().run(drifted, {"member_id": "M1001"}, "tenant_a")
    m = read(runs_dir / "metrics.jsonl")[-1]
    assert rep.result.bucket == "SUCCESS"  # still works on the backup rung...
    assert m.fallback_resolutions == 1 and m.coordinate_resolutions == 0  # ...but the ledger shows the drift


def test_handoff_and_recovery_numbers(server, tmp_path):
    run_scenario(_scenario("X17"), server.base_url, tmp_path)
    m = read(tmp_path / "metrics.jsonl")[-1]
    assert m.bucket == "ESCALATED" and m.handoffs == 2 and m.handoff_outcomes == {"RESUMED": 2}
    assert len(m.seconds_to_claim) == 2 and m.human_events >= 2 and m.recoveries == {"KNOWN_DIALOG": 1}


def test_approvals_are_counted(server, tmp_path):
    run_scenario(_scenario("X06"), server.base_url, tmp_path)
    assert read(tmp_path / "metrics.jsonl")[-1].approvals == {"DENIED": 1}


def test_cost_uses_cache_rates():
    # 1M uncached in + 1M out on Opus 5 = $5 + $25; 1M cache reads = $0.50; 1M cache writes = $6.25
    assert cost_usd("claude-opus-5", 1_000_000, 1_000_000) == 30.0
    assert cost_usd("claude-opus-5", 0, 0, cache_read=1_000_000, cache_write=1_000_000) == 6.75
    assert cost_usd("unknown-model", 1, 1) is None
