"""Acceptance matrix A1 to A22 (docs/BUILD_MAP.md 3a).

Integration: real Chromium against MockBankApp, golden artifacts, no LLM.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cua.contracts import ActionType, ArtifactStatus, Step, TenantConfig
from cua.operator import ScriptedOperator
from cua.session import TokenState
from mockbank import data

SUB = {"member_id": "M1001", "account_type": "MONEY_MARKET", "initial_deposit": "100.00"}


def main(page):
    return page.frame(name="main")


def acknowledge_dialog(page):
    main(page).get_by_role("button", name="Acknowledge").click()


def sign_in(page):
    f = main(page)
    f.locator("input[name=u]").fill(data.OPERATOR_USERNAME)
    f.locator("input[name=p]").fill(data.OPERATOR_PASSWORD)
    f.get_by_role("button", name="Sign In").click()
    f.wait_for_load_state()


def run(engine, cap, inputs, tenant="tenant_a", console=None, **kw):
    return engine(console=console, **kw).run(cap, inputs, tenant)


# ---------------------------------------------------------------- get_savings_balance


def test_a1_happy_path(reset, engine, golden):
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "SUCCESS"
    assert rep.result.outputs == {"balance": "2450.17", "currency": "USD"}
    assert rep.steps_executed == ["s1", "s2", "s3", "s4", "s5"]
    assert rep.result.recoveries == []


@pytest.mark.parametrize("member,balance", [("M1003", "0.00"), ("M1004", "1234567.89")])
def test_a2_edge_outputs(reset, engine, golden, member, balance):
    rep = run(engine, golden("get_savings_balance"), {"member_id": member})
    assert rep.result.bucket == "SUCCESS"
    assert Decimal(rep.result.outputs["balance"]) == Decimal(balance)


def test_a3_member_not_found(reset, engine, golden):
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M9999"})
    assert rep.result.bucket == "BUSINESS_OUTCOME"
    assert rep.result.code.value == "MEMBER_NOT_FOUND"


def test_a4_known_interstitial_recovered(reset, engine, golden):
    reset(fault="F2")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "SUCCESS"
    assert [r.kind for r in rep.result.recoveries] == ["KNOWN_DIALOG"]


def test_a5_slow_load_within_budget(reset, engine, golden):
    reset(fault="F4")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "SUCCESS"
    assert [r.kind for r in rep.result.recoveries] == ["SLOW_LOAD"]


def test_a6_slow_load_beyond_budget(reset, engine, golden, runs_dir):
    reset(fault="F5")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "TIMEOUT"
    assert rep.result.step == "s3"
    assert (runs_dir / f"{rep.result.evidence_ref}.txt").exists()


def test_a7_transient_app_error_retried(reset, engine, golden):
    reset(fault="F6")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "SUCCESS"
    assert [r.kind for r in rep.result.recoveries] == ["TRANSIENT_APP_ERROR"]


def test_a8_persistent_app_error(reset, engine, golden):
    reset(fault="F7")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "APP_ERROR"


def test_a9_ambiguous_target_stops_without_clicking(reset, engine, golden, runs_dir):
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1002"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "AMBIGUOUS_TARGET"
    assert rep.result.step == "s3"
    assert rep.ui_actions == 2  # fill + search; the ambiguous View link was never clicked


def test_a10_drift_detected(reset, engine, golden):
    reset(tenant="tenant_c")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, tenant="tenant_c")
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "DRIFT_DETECTED"
    assert "4.3.0" in rep.result.observed
    assert rep.ui_actions == 0


@pytest.mark.parametrize("bad", [{"member_id": "abc"}, {"member_id": "M1001", "extra": "x"}, {}])
def test_a11_input_validation_error(reset, engine, golden, bad):
    rep = run(engine, golden("get_savings_balance"), bad)
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "VALIDATION_ERROR"
    assert rep.ui_actions == 0


def test_a12_permission_denied(reset, engine, golden):
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1005"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "PERMISSION_DENIED"


def test_a13_unknown_dialog_handoff_resumes(reset, engine, golden):
    reset(fault="F3")
    op = ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")])
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, console=op)
    assert rep.result.bucket == "ESCALATED"
    assert rep.result.outputs == {"balance": "2450.17", "currency": "USD"}
    h = rep.result.handoffs[0]
    assert h.outcome == "RESUMED" and h.step == "s2" and h.human_events >= 1  # dialog appears as s2's effect
    states = " ".join(h.transitions)
    for s in TokenState:
        if s not in (TokenState.FAILED, TokenState.CANCELLED):
            assert s.value in states
    assert op.interventions[0].reason.startswith("UNKNOWN_DIALOG")


def test_a13b_checkpoint_fails_then_human_fixes(reset, engine, golden):
    reset(fault="F3")
    op = ScriptedOperator(rounds=[([], "RESUME"), ([acknowledge_dialog], "RESUME")])
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, console=op)
    assert rep.result.bucket == "ESCALATED"
    assert "VERIFYING_CHECKPOINT->HUMAN_IN_CONTROL" in " ".join(rep.result.handoffs[0].transitions)
    assert op.notifications


def test_a14_unknown_dialog_nobody_claims(reset, engine, golden):
    reset(fault="F3")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, console=ScriptedOperator(claim=False))
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "HANDOFF_FAILED"


def test_a14b_operator_cancels(reset, engine, golden):
    reset(fault="F3")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"},
              console=ScriptedOperator(rounds=[([], "CANCEL")]))
    assert rep.result.bucket == "ESCALATED"
    assert rep.result.handoffs[0].outcome == "CANCELLED"


def test_a14c_blocker_without_operator_fails(reset, engine, golden):
    reset(fault="F3")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "UNRECOVERABLE_BLOCKER"


def test_a15_session_timeout_human_signs_in(reset, engine, golden):
    reset(fault="F8")
    op = ScriptedOperator(rounds=[([sign_in], "RESUME")])
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, console=op)
    assert rep.result.bucket == "ESCALATED", rep.result
    assert rep.result.outputs["balance"] == "2450.17"
    assert op.interventions[0].reason.startswith("SESSION_EXPIRED")


# ---------------------------------------------------------------- open_sub_account


def test_a16_irreversible_approved(reset, engine, golden, app_state):
    op = ScriptedOperator(approve=True)
    rep = run(engine, golden("open_sub_account"), SUB, console=op)
    assert rep.result.bucket == "SUCCESS", rep.result
    assert rep.result.outputs["confirmation_number"] == "CNF-10000001"
    assert len(app_state()["created"]) == 1
    summary = op.approvals[0].summary
    assert summary["member_id"] == "M**01" and summary["initial_deposit"] == "100.00"


def test_a17_irreversible_denied(reset, engine, golden, app_state):
    rep = run(engine, golden("open_sub_account"), SUB, console=ScriptedOperator(approve=False))
    assert rep.result.bucket == "BUSINESS_OUTCOME"
    assert rep.result.code.value == "DECLINED_BY_OPERATOR"
    assert app_state()["created"] == []


def test_a17b_approval_timeout_commits_nothing(reset, engine, golden, app_state):
    rep = run(engine, golden("open_sub_account"), SUB, console=ScriptedOperator(approve=None))
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "HANDOFF_FAILED"
    assert app_state()["created"] == []


def test_a17c_irreversible_without_operator_refused(reset, engine, golden, app_state):
    rep = run(engine, golden("open_sub_account"), SUB)
    assert rep.result.bucket == "FAILURE"
    assert app_state()["created"] == []


@pytest.mark.parametrize("inputs", [{**SUB, "initial_deposit": "5.00"}, {**SUB, "member_id": "M1006"}])
def test_a18_app_validation_rejected(reset, engine, golden, app_state, inputs):
    rep = run(engine, golden("open_sub_account"), inputs, console=ScriptedOperator())
    assert rep.result.bucket == "BUSINESS_OUTCOME"
    assert rep.result.code.value == "VALIDATION_REJECTED"
    assert app_state()["created"] == []


@pytest.mark.parametrize("bad", [{**SUB, "member_id": "abc"}, {**SUB, "initial_deposit": "-10"},
                                 {**SUB, "account_type": "CRYPTO"}])
def test_a11b_subaccount_input_validation(reset, engine, golden, bad):
    rep = run(engine, golden("open_sub_account"), bad, console=ScriptedOperator())
    assert rep.result.reason.value == "VALIDATION_ERROR"
    assert rep.ui_actions == 0


# ---------------------------------------------------------------- tenants + preflight


def test_a19_tenant_overlay(reset, engine, golden):
    reset(tenant="tenant_b")
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, tenant="tenant_b")
    assert rep.result.bucket == "SUCCESS", rep.result
    assert rep.result.outputs["balance"] == "2450.17"


def test_a19b_tenant_b_without_overlay_fails(reset, engine, golden):
    reset(tenant="tenant_b")
    tenants = {"tenant_b": TenantConfig(tenant="tenant_b", expected_version="4.2.1")}
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, tenant="tenant_b", tenants=tenants)
    assert rep.result.bucket == "FAILURE"


def test_a20_invalid_overlay_rejected(reset, engine, golden, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"tenant": "tenant_a", "base_capability": "get_savings_balance", '
                   '"steps": [{"id": "x", "action": "CLICK"}]}')
    tenants = {"tenant_a": TenantConfig(tenant="tenant_a", expected_version="4.2.1",
                                        overlays={"get_savings_balance": str(bad)})}
    rep = run(engine, golden("get_savings_balance"), {"member_id": "M1001"}, tenants=tenants)
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "OVERLAY_REJECTED"
    assert rep.ui_actions == 0


def test_a21_draft_refused_unattended(reset, engine, golden):
    draft = golden("get_savings_balance").model_copy(update={"status": ArtifactStatus.DRAFT})
    rep = run(engine, draft, {"member_id": "M1001"})
    assert rep.result.reason.value == "ARTIFACT_NOT_APPROVED"
    assert rep.ui_actions == 0


def test_a22_off_allowlist_step_refused(reset, engine, golden):
    cap = golden("get_savings_balance")
    evil = Step(id="s9", action=ActionType.NAVIGATE, description="wire funds", url="/admin/wire-transfer")
    tampered = cap.model_copy(update={"steps": cap.steps + [evil]})
    rep = run(engine, tampered, {"member_id": "M1001"})
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "POLICY_VIOLATION"
    assert rep.ui_actions == 0
