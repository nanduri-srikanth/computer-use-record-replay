"""Independent review: adversarial replay against the real Playwright surface and the mock bank (no LLM)."""

from __future__ import annotations

import json

import pytest

from cua.contracts import ArtifactStatus, Checkpoint, FieldSpec, LocatorCandidate, LocatorKind, RiskTier, TenantConfig
from cua.operator import ScriptedOperator
from cua.store import ArtifactStore

from .helpers import (click_step, coords, element, evidence_artifact, open_main, probe_capability, replace_candidates,
                      run_dir_text)

SUB = {"member_id": "M1001", "account_type": "MONEY_MARKET", "initial_deposit": "100.00"}


# ---------------------------------------------------------------- tampered / unapproved artifacts


@pytest.mark.xfail(strict=True, reason="FINDING (high): approval is not bound to content. content_hash() is computed and logged (engine.py:94) but never recorded at approve time or verified at load/replay (store.py:37-38, 74-79); an edited APPROVED file replays unattended and here returns the Checking balance as SUCCESS")
def test_tampered_approved_artifact_is_refused(tmp_path, reset, engine, golden, redactor):
    """Approve v1, then edit the approved file on disk (re-point the account row from Savings to Checking)."""
    store = ArtifactStore(tmp_path / "artifacts", redactor)
    store.save_draft(golden("get_savings_balance"))
    store.approve("get_savings_balance", 1)
    p = tmp_path / "artifacts" / "get_savings_balance" / "v1.json"
    body = json.loads(p.read_text())
    body["steps"][2]["target"]["candidates"][0]["anchor_text"] = "Checking"
    p.write_text(json.dumps(body))
    try:
        tampered = store.latest("get_savings_balance")
    except Exception:  # noqa: BLE001 - refusing to load a tampered artifact is the desired behaviour
        return
    rep = engine().run(tampered, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket != "SUCCESS", f"tampered approved artifact ran and returned {rep.result.outputs}"


@pytest.mark.xfail(strict=True, reason="FINDING (high): lifecycle status is a plain field in the JSON (store.py:37-38); flipping DRAFT to APPROVED on disk passes the unattended gate (engine.py:126) with no approval record or signature")
def test_status_forged_on_disk_does_not_grant_unattended_replay(tmp_path, reset, engine, golden, redactor):
    store = ArtifactStore(tmp_path / "artifacts", redactor)
    store.save_draft(golden("get_savings_balance"))  # a DRAFT nobody approved
    p = tmp_path / "artifacts" / "get_savings_balance" / "v1.json"
    p.write_text(p.read_text().replace('"status": "DRAFT"', '"status": "APPROVED"'))
    cap = store.latest("get_savings_balance")
    if cap is None:
        return
    rep = engine().run(cap, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "ARTIFACT_NOT_APPROVED"


def test_deprecated_artifact_is_refused_unattended_without_touching_ui(reset, engine, golden):
    cap = golden("get_savings_balance").model_copy(update={"status": ArtifactStatus.DEPRECATED})
    rep = engine().run(cap, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "ARTIFACT_NOT_APPROVED" and rep.ui_actions == 0


def test_lowering_declared_risk_does_not_skip_the_commit_approval(reset, engine, golden, app_state):
    cap = golden("open_sub_account")
    cap = cap.model_copy(update={"steps": [s.model_copy(update={"risk": RiskTier.READ}) for s in cap.steps]})
    rep = engine().run(cap, SUB, "tenant_a")  # no operator console: nobody can approve
    assert rep.result.bucket == "FAILURE" and rep.result.step == "s7"
    assert app_state()["created"] == []


# ---------------------------------------------------------------- allowlist


def test_start_route_off_allowlist_refused_before_any_ui_action(reset, engine, golden):
    cap = golden("get_savings_balance").model_copy(update={"start_route": "/admin/audit"})
    rep = engine().run(cap, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "POLICY_VIOLATION" and rep.ui_actions == 0


def _audit_link_capability(candidate: LocatorCandidate):
    return probe_capability(
        [click_step("s1", "nav", [candidate], post=Checkpoint(frame="main", text_present=["Last Entry:"]))],
        success=Checkpoint(frame="main", text_present=["Audit Console"]))


def test_role_name_click_toward_off_allowlist_page_refused_before_click(reset, engine):
    reset(audit_link=True)
    cap = _audit_link_capability(LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="link", name="Audit Console"))
    rep = engine().run(cap, {}, "tenant_a")
    assert rep.result.reason.value == "POLICY_VIOLATION"
    assert rep.result.observed.startswith("pre_action"), rep.result.observed


@pytest.mark.xfail(strict=True, reason="FINDING (high): the pre-click allowlist check only runs when Match.target_url is set (engine.py:233-237), and COORDINATES matches never set it (playwright_surface.py:276-281). The post-wait checks URLs before reading the checkpoint (engine.py:298-299, TOCTOU) and there is no URL check before SUCCESS (engine.py:177-186), so the run navigated to /admin/audit and reported SUCCESS (3/3 runs)")
def test_coordinate_click_toward_off_allowlist_page_refused_before_click(reset, engine, surface, server):
    reset(audit_link=True)
    surface.goto("/")
    link = element(surface, "nav", role="link", name="Audit Console")
    rep = engine().run(_audit_link_capability(coords(link.x, link.y)), {}, "tenant_a")
    assert rep.result.bucket == "FAILURE", f"run ended {rep.result.bucket} after navigating off the allowlist"
    assert rep.result.reason.value == "POLICY_VIOLATION"
    assert rep.result.observed.startswith("pre_action"), rep.result.observed  # i.e. the browser never went there


# ---------------------------------------------------------------- locator ambiguity and drift


@pytest.mark.xfail(strict=True, reason="FINDING (high): COORDINATES always resolves count=1 when anything is at the point (playwright_surface.py:276-281), and the recorder appends it to every discovered target (recorder.py:52-53). When a label drifts, the ladder falls through to a blind click: M1002 returns one of two savings balances as SUCCESS where tenant_a gives AMBIGUOUS_TARGET")
def test_coordinate_fallback_does_not_bypass_the_ambiguity_guard(reset, engine, golden, surface, server):
    """Same artifact, two tenants: on tenant_a the Savings anchor matches two rows (AMBIGUOUS_TARGET). On the
    relabelled tenant_b ('Share Savings') the anchor matches nothing, so the ladder falls to COORDINATES."""
    reset(tenant="tenant_b")
    open_main(surface, server.base_url, "/member/summary?mid=M1002")
    second_view = element(surface, "main", role="link", name="View", index=1)
    cap = golden("get_savings_balance")
    cap = replace_candidates(cap, "s1", [LocatorCandidate(kind=LocatorKind.LABEL_PROXIMITY, label="Member #:",
                                                          element="input")])
    cap = replace_candidates(cap, "s2", [LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="button", name="Find")])
    cap = replace_candidates(cap, "s3", [LocatorCandidate(kind=LocatorKind.TABLE_ANCHOR, anchor_text="Savings",
                                                          column=4, element="a"),
                                         coords(second_view.x, second_view.y)])
    tenants = {"tenant_b": TenantConfig(tenant="tenant_b", expected_version="4.2.1")}
    rep = engine(tenants=tenants).run(cap, {"member_id": "M1002"}, "tenant_b")
    assert rep.result.bucket == "FAILURE", f"M1002 has two savings accounts, yet replay returned {rep.result}"


@pytest.mark.xfail(strict=True, reason="FINDING (claim mismatch): REPORT section 3 says column reorder gives TARGET_NOT_FOUND. That holds only for the hand-written golden. The live-discovered APPROVED artifact (evidence v4) falls back to COORDINATES, clicks the wrong cell, and ends in TIMEOUT (tried TABLE_ANCHOR=0, COORDINATES=1)")
def test_discovered_artifact_reports_column_reorder_as_target_not_found(reset, engine):
    """REPORT section 3: 'Column reorders or renamed labels give TARGET_NOT_FOUND with evidence'.
    Checked here against the real live-discovered, APPROVED artifact in /evidence, not the hand-written golden."""
    reset(column_reorder=True)
    rep = engine().run(evidence_artifact(), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE"
    assert rep.result.reason.value == "TARGET_NOT_FOUND", (rep.result.reason, rep.result.observed[:120])


def test_golden_truncated_fill_is_action_failed(reset, engine, golden):
    reset(truncate_input=True)
    rep = engine().run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "ACTION_FAILED"


@pytest.mark.xfail(strict=True, reason="FINDING (high): FILL read-back is skipped for coordinate matches (engine.py:265, isinstance(handle, tuple)). A truncated member id is submitted and the caller receives BUSINESS_OUTCOME MEMBER_NOT_FOUND, a false business answer instead of ACTION_FAILED")
def test_coordinate_fill_is_verified_like_any_other_fill(reset, engine, golden, surface):
    """The label drifted, so FILL resolves by coordinates; the field truncates input. The read-back that turns
    truncation into ACTION_FAILED must still apply, otherwise 'M10' is searched and the caller hears 'no such member'."""
    reset(truncate_input=True)
    surface.goto("/")
    box = element(surface, "main", role="textbox", label="Member ID:")
    cap = replace_candidates(golden("get_savings_balance"), "s1", [
        LocatorCandidate(kind=LocatorKind.LABEL_PROXIMITY, label="Member Number:", element="input"),
        coords(box.x, box.y)])
    rep = engine().run(cap, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "ACTION_FAILED", rep.result


def test_commit_reached_by_coordinate_fallback_still_needs_approval(reset, engine, golden, surface, server, app_state):
    open_main(surface, server.base_url, "/subaccount/review?mid=M1001&type=MONEY_MARKET&deposit=100.00")
    btn = element(surface, "main", role="button", name="Confirm")
    reset()
    cap = replace_candidates(golden("open_sub_account"), "s7", [
        LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="button", name="Confirm Now"), coords(btn.x, btn.y)],
        risk=RiskTier.READ)
    rep = engine().run(cap, SUB, "tenant_a")  # no operator console
    assert rep.result.bucket == "FAILURE" and rep.result.step == "s7"
    assert app_state()["created"] == []


# ---------------------------------------------------------------- outputs, success condition, sensitive data


def test_output_schema_violation_is_failure_without_leaking_values(reset, engine, golden):
    cap = golden("get_savings_balance")
    outs = [o if o.name != "currency" else o.model_copy(update={"pattern": "^[A-Z]{2}$"}) for o in cap.outputs]
    rep = engine().run(cap.model_copy(update={"outputs": outs}), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "OUTPUT_INVALID"
    assert "2450.17" not in rep.result.model_dump_json() and "2,450.17" not in rep.result.model_dump_json()


def test_unmet_success_condition_returns_no_outputs(reset, engine, golden):
    cap = golden("get_savings_balance").model_copy(
        update={"success": Checkpoint(frame="main", text_present=["Account Detail", "Statement Closed"])})
    rep = engine().run(cap, {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "SUCCESS_CONDITION_UNMET" and not hasattr(rep.result, "outputs")


@pytest.mark.xfail(strict=True, reason="FINDING (medium): the sensitive flag is honoured for outputs only (engine.py:489-498). Inputs are persisted through pattern redaction alone (engine.py:111), so a sensitive free-text input is written verbatim to result.json")
def test_sensitive_input_is_never_persisted(reset, engine, golden, runs_dir):
    """FieldSpec.sensitive: 'masked in anything persisted'. Applied here to an input rather than an output."""
    cap = golden("get_savings_balance")
    note = FieldSpec(name="caller_note", type="string", sensitive=True)
    cap = cap.model_copy(update={"inputs": [*cap.inputs, note]})
    rep = engine().run(cap, {"member_id": "M1001", "caller_note": "Jane Q Public asked by phone"}, "tenant_a")
    assert rep.result.bucket == "SUCCESS"
    assert "Jane Q Public" not in run_dir_text(runs_dir, rep.run_id)


# ---------------------------------------------------------------- determinism


@pytest.mark.parametrize("fault,member", [("F2", "M1001"), ("F1", "M9999"), ("F6", "M1001")])
def test_repeated_replays_are_identical(reset, engine, golden, runs_dir, fault, member):
    from .helpers import events
    traces = []
    for _ in range(3):
        reset(fault=fault)
        rep = engine().run(golden("get_savings_balance"), {"member_id": member}, "tenant_a")
        evs = events(runs_dir, rep.run_id)
        traces.append((rep.result.bucket, tuple(rep.steps_executed), rep.ui_actions,
                       tuple(r.kind for r in rep.result.recoveries),
                       tuple((e["kind"], e.get("step"), e.get("candidate")) for e in evs
                             if e["kind"] in ("resolved", "acted", "recovered", "business_outcome", "failure"))))
    assert len(set(traces)) == 1, traces
