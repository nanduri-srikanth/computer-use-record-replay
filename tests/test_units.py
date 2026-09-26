"""Unit tests with no browser (docs/BUILD_MAP.md 3b)."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from cua.contracts import (ActionType, ArtifactStatus, BusinessCode, BusinessOutcome, Capability, Escalated,
                           Failure, FailureReason, HandoffRecord, LocatorCandidate, LocatorKind, RiskTier, Success,
                           TargetDescriptor, TenantOverlay)
from cua.locators import AmbiguousTarget, TargetNotFound, resolve_ladder
from cua.overlay import OverlayRejected, load_overlay, merge
from cua.policy import PolicyEngine, PolicyViolation
from cua.redactor import Redactor
from cua.replay.engine import ReplayEngine, _Ctx
from cua.session import S, TRANSITIONS, ControlToken, IllegalTransition
from cua.store import ArtifactStore, LifecycleError
from cua.surface import Match

from .conftest import ROOT


def golden_cap(name="get_savings_balance") -> Capability:
    return Capability.model_validate_json((ROOT / "artifacts" / name / "v1.json").read_text())


# ---------------------------------------------------------------- contracts


def test_artifact_round_trips():
    cap = golden_cap()
    again = Capability.model_validate_json(cap.model_dump_json())
    assert again == cap and again.content_hash() == cap.content_hash()


@pytest.mark.parametrize("edit,message", [
    (lambda b: b["success"].update(input_present=["inputs.nope"]), "unknown input"),
    (lambda b: b["inputs"][0].update(sensitive=True), "sensitive input"),
    (lambda b: b.update(outcomes=b["outcomes"] * 2), "duplicate outcome"),
    (lambda b: b["inputs"][0].update(type="decimal", pattern=None), "only bind string inputs"),
    (lambda b: b["success"].update(text_present=[], input_present=[]), "text_present or input_present"),
])
def test_contract_validation(edit, message):
    body = json.loads(golden_cap().model_dump_json())
    edit(body)
    with pytest.raises(ValidationError, match=message):
        Capability.model_validate(body)


def test_irreversible_capability_must_declare_operator_decline():
    body = json.loads(golden_cap("open_sub_account").model_dump_json())
    body["outcomes"] = [o for o in body["outcomes"] if o["code"] != "DECLINED_BY_OPERATOR"]
    with pytest.raises(ValidationError, match="DECLINED_BY_OPERATOR"):
        Capability.model_validate(body)


def test_contract_view_has_no_steps_and_lists_outcomes():
    c = golden_cap().contract()
    assert "steps" not in c and set(c["results"]["BUSINESS_OUTCOME"]) == {"MEMBER_NOT_FOUND"}
    assert all(f.get("description") for f in c["inputs"] + c["outputs"])


def test_older_schema_refused_with_clear_message(tmp_path):
    body = json.loads(golden_cap().model_dump_json())
    body["schema_version"] = 2
    (tmp_path / "get_savings_balance").mkdir()
    (tmp_path / "get_savings_balance" / "v1.json").write_text(json.dumps(body))
    with pytest.raises(LifecycleError, match="schema v2"):
        ArtifactStore(tmp_path).load("get_savings_balance", 1)


def test_unknown_fields_rejected():
    body = json.loads(golden_cap().model_dump_json())
    body["steps"][0]["selector"] = "#member"
    with pytest.raises(ValidationError):
        Capability.model_validate(body)


@pytest.mark.parametrize("step,field,value", [
    (0, "value_from", "M1001"),  # a literal value instead of an input reference
    (0, "value_from", "inputs"),  # malformed reference
    (1, "value_from", "inputs.member_id"),  # value_from on a CLICK
])
def test_value_from_must_be_an_input_reference_on_writes(step, field, value):
    body = json.loads(golden_cap().model_dump_json())
    body["steps"][step][field] = value
    with pytest.raises(ValidationError):
        Capability.model_validate(body)


def test_enum_input_needs_values_and_success_condition_required():
    body = json.loads(golden_cap().model_dump_json())
    body["inputs"].append({"name": "kind", "type": "enum"})
    with pytest.raises(ValidationError):
        Capability.model_validate(body)
    body = json.loads(golden_cap().model_dump_json())
    del body["success"]
    with pytest.raises(ValidationError):
        Capability.model_validate(body)


def test_ladder_order_enforced():
    with pytest.raises(ValidationError):
        TargetDescriptor(description="x", candidates=[
            LocatorCandidate(kind=LocatorKind.COORDINATES, x=1, y=1),
            LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="button", name="Go")])


def test_status_change_keeps_content_hash():
    cap = golden_cap()
    assert cap.model_copy(update={"status": ArtifactStatus.DEPRECATED}).content_hash() == cap.content_hash()


@pytest.mark.parametrize("field", ["steps", "risk", "allowed_actions", "permissions"])
def test_overlay_schema_cannot_express_steps_or_permissions(field):
    with pytest.raises(ValidationError):
        TenantOverlay.model_validate({"tenant": "t", "base_capability": "c", field: []})


# ---------------------------------------------------------------- locator ladder


class FakeSurface:
    def __init__(self, counts):
        self.counts, self.calls = counts, []

    def resolve(self, frame, c):
        self.calls.append(c.kind)
        n = self.counts[c.kind]
        return Match(count=n, handle="h" if n == 1 else None)


LADDER = TargetDescriptor(description="t", candidates=[
    LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="link", name="View"),
    LocatorCandidate(kind=LocatorKind.LABEL_PROXIMITY, label="Acct:", element="a"),
    LocatorCandidate(kind=LocatorKind.TABLE_ANCHOR, anchor_text="Savings", column=4, element="a"),
    LocatorCandidate(kind=LocatorKind.COORDINATES, x=10, y=10)])


def test_ladder_first_unique_wins():
    s = FakeSurface({LocatorKind.ROLE_NAME: 1, LocatorKind.LABEL_PROXIMITY: 1,
                     LocatorKind.TABLE_ANCHOR: 1, LocatorKind.COORDINATES: 1})
    assert resolve_ladder(s, LADDER).candidate_index == 0 and s.calls == [LocatorKind.ROLE_NAME]


def test_ladder_zero_moves_to_next_semantic_candidate():
    s = FakeSurface({LocatorKind.ROLE_NAME: 0, LocatorKind.LABEL_PROXIMITY: 0,
                     LocatorKind.TABLE_ANCHOR: 1, LocatorKind.COORDINATES: 1})
    assert resolve_ladder(s, LADDER).candidate_index == 2


def test_ladder_never_falls_back_to_coordinates_after_semantic_drift():
    s = FakeSurface({LocatorKind.ROLE_NAME: 0, LocatorKind.LABEL_PROXIMITY: 0,
                     LocatorKind.TABLE_ANCHOR: 0, LocatorKind.COORDINATES: 1})
    with pytest.raises(TargetNotFound, match="not used after semantic drift"):
        resolve_ladder(s, LADDER)
    assert LocatorKind.COORDINATES not in s.calls


def test_ladder_coordinate_only_target_resolves():
    s = FakeSurface({LocatorKind.COORDINATES: 1})
    only = TargetDescriptor(description="t", candidates=[LocatorCandidate(kind=LocatorKind.COORDINATES, x=1, y=1)])
    assert resolve_ladder(s, only).candidate_index == 0


def test_ladder_ambiguous_stops_immediately():
    s = FakeSurface({LocatorKind.ROLE_NAME: 0, LocatorKind.LABEL_PROXIMITY: 2,
                     LocatorKind.TABLE_ANCHOR: 1, LocatorKind.COORDINATES: 1})
    with pytest.raises(AmbiguousTarget):
        resolve_ladder(s, LADDER)
    assert LocatorKind.TABLE_ANCHOR not in s.calls


def test_ladder_nothing_matches():
    s = FakeSurface({k: 0 for k in LocatorKind})
    with pytest.raises(TargetNotFound):
        resolve_ladder(s, LADDER)


# ---------------------------------------------------------------- policy


def test_allowlist_routes_and_hosts():
    p = PolicyEngine()
    p.check_url("http://127.0.0.1:5000/member/summary?mid=1")
    for bad in ("http://evil.example/member/", "http://127.0.0.1:5000/admin/wire"):
        with pytest.raises(PolicyViolation):
            p.check_url(bad)
    with pytest.raises(PolicyViolation):
        p.check_action(ActionType.NAVIGATE)


def test_risk_classification_and_no_downgrade():
    p = PolicyEngine()
    assert p.classify(ActionType.CLICK, "/subaccount/review", "Confirm") == RiskTier.IRREVERSIBLE
    assert p.classify(ActionType.FILL, "/member/search", None) == RiskTier.REVERSIBLE_WRITE
    assert p.classify(ActionType.CLICK, "/member/search", "Search") == RiskTier.READ
    # fail closed: unlisted commit-like controls and POST submits are irreversible anywhere
    assert p.classify(ActionType.CLICK, "/member/summary", "Delete Member") == RiskTier.IRREVERSIBLE
    assert p.classify(ActionType.CLICK, "/member/summary", "Wire Transfer") == RiskTier.IRREVERSIBLE
    assert p.classify(ActionType.CLICK, "/member/summary", "Save", submits_post=True) == RiskTier.IRREVERSIBLE
    assert p.classify(ActionType.CLICK, "/member/summary", "Postal Address") == RiskTier.READ
    assert p.effective(RiskTier.READ, RiskTier.IRREVERSIBLE) == RiskTier.IRREVERSIBLE
    assert p.effective(RiskTier.IRREVERSIBLE, RiskTier.READ) == RiskTier.IRREVERSIBLE


def test_approval_token_single_use_and_bound():
    p = PolicyEngine()
    t = p.issue_token("run-1", "s7", "op")
    with pytest.raises(PolicyViolation):
        p.consume_token(t, "run-2", "s7")  # cross-run
    p.consume_token(t, "run-1", "s7")
    with pytest.raises(PolicyViolation):
        p.consume_token(t, "run-1", "s7")  # replayed
    forged = PolicyEngine().issue_token("run-1", "s7", "op")
    with pytest.raises(PolicyViolation):
        p.consume_token(forged, "run-1", "s7")  # signed by another engine


# ---------------------------------------------------------------- control token


def test_every_valid_transition():
    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            t = ControlToken()
            t.state = src
            t.transition(dst)
            assert t.state == dst


def test_invalid_transitions_raise():
    for src in S:
        for dst in set(S) - TRANSITIONS[src]:
            t = ControlToken()
            t.state = src
            with pytest.raises(IllegalTransition):
                t.transition(dst)


def test_act_rejected_unless_automation(tmp_path):
    from cua.config import Settings
    from cua.evidence import EvidenceSink
    from cua.session import SessionController
    from cua.surface import ControlTokenHeld

    class Stub:
        def set_act_guard(self, g):
            self.guard = g

    stub, red = Stub(), Redactor()
    sc = SessionController(stub, None, PolicyEngine(), EvidenceSink(tmp_path, "r", red), red, Settings(), "r")
    stub.guard()  # AUTOMATION: allowed
    for state in set(S) - {S.AUTOMATION}:
        sc.token.state = state
        with pytest.raises(ControlTokenHeld):
            stub.guard()
    assert sum(e["kind"] == "act_rejected" for e in sc.evidence.events) == len(S) - 1


# ---------------------------------------------------------------- classifier


H = [HandoffRecord(step="s2", reason="UNKNOWN_DIALOG", outcome="RESUMED")]


@pytest.mark.parametrize("result,handoffs,bucket", [
    (Success(outputs={"a": "1"}), [], "SUCCESS"),
    (Success(outputs={"a": "1"}), H, "ESCALATED"),
    (BusinessOutcome(code=BusinessCode.MEMBER_NOT_FOUND), [], "BUSINESS_OUTCOME"),
    (BusinessOutcome(code=BusinessCode.MEMBER_NOT_FOUND), H, "ESCALATED"),
    (Failure(reason=FailureReason.TIMEOUT, step="s3", expected="x", observed="y"), H, "FAILURE"),
    (Escalated(reason="cancelled", handoffs=H), H, "ESCALATED"),
])
def test_classifier_precedence(result, handoffs, bucket):
    ctx = _Ctx(run_id="r", cap=golden_cap(), ev=None, session=None, det=None, handoffs=handoffs)
    assert ReplayEngine._classify(ctx, result).bucket == bucket


# ---------------------------------------------------------------- lifecycle


def test_lifecycle_one_way_and_supersede(tmp_path):
    store = ArtifactStore(tmp_path)
    v1 = store.save_draft(golden_cap())
    with pytest.raises(LifecycleError):
        store.deprecate(v1.name, 1)  # DRAFT -> DEPRECATED not allowed
    store.approve(v1.name, 1)
    with pytest.raises(LifecycleError):
        store.approve(v1.name, 1)  # APPROVED -> APPROVED not allowed
    v2 = store.save_draft(golden_cap())
    assert v2.version == 2 and store.latest(v1.name).version == 1
    store.approve(v1.name, 2)
    assert store.load(v1.name, 1).status == ArtifactStatus.DEPRECATED
    assert store.latest(v1.name).version == 2
    with pytest.raises(LifecycleError):
        store._transition(v1.name, 1, ArtifactStatus.APPROVED)  # DEPRECATED is terminal


def test_store_refuses_pii(tmp_path):
    cap = golden_cap()
    leaky = cap.model_copy(update={"goal": "look up 123-45-6789"})
    with pytest.raises(LifecycleError):
        ArtifactStore(tmp_path).save_draft(leaky)


# ---------------------------------------------------------------- overlay merge


def test_overlay_merge_deterministic():
    base, ov = golden_cap(), load_overlay(ROOT / "overlays" / "tenant_b.json")
    a, b = merge(base, ov), merge(base, ov)
    assert a.content_hash() == b.content_hash() != base.content_hash()
    assert a.steps[0].target.candidates[0].label == "Member #:"
    assert a.steps[1].target.candidates[0].name == "Find"
    assert a.steps[2].target.candidates[0].anchor_text == "Share Savings"
    assert [s.risk for s in a.steps] == [s.risk for s in base.steps]


def test_overlay_for_unknown_step_rejected():
    ov = TenantOverlay(tenant="t", base_capability="get_savings_balance",
                       locator_overrides={"s99": [LocatorCandidate(kind=LocatorKind.COORDINATES, x=1, y=1)]})
    with pytest.raises(OverlayRejected):
        merge(golden_cap(), ov)


# ---------------------------------------------------------------- redactor


@pytest.mark.parametrize("raw,clean", [
    ("SSN 123-45-6789", "SSN ***-**-****"),
    ("DOB 1984-03-12", "DOB ****-**-**"),
    ("acct 10012346", "acct ****2346"),
    ("member M1001", "member M**01"),
    ("mail a.b@bank.com", "mail ***@***"),
    ("Available Balance: $2,450.17", "Available Balance: $***"),
    ("at least $25.00.", "at least $***."),
    ("Account Type: Savings", "Account Type: Savings"),
    ("Name:\tJane Doe\nSSN:\t123-45-6789", "Name: [REDACTED]\nSSN:\t***-**-****"),
    ("User Name:\tteller1", "User Name:\tteller1"),
])
def test_redactor_patterns(raw, clean):
    assert Redactor().text(raw) == clean


def test_redactor_masks_values_by_label():
    r = Redactor()
    assert r.labelled("Name:", "Jane Doe") == "[REDACTED]"
    assert r.labelled("Currency:", "USD") == "USD"
    assert r.labelled("Available Balance:", "$2,450.17") == "$***"


def test_redactor_secrets_and_keys():
    r = Redactor(secrets=["hunter2-secret"])
    assert r.text("pw is hunter2-secret") == "pw is [SECRET]"
    assert r.value({"password": "anything", "nested": {"api_key": "x"}}) == \
        {"password": "[SECRET]", "nested": {"api_key": "[SECRET]"}}


def test_screenshots_mask_every_on_screen_identifier():
    """Anything the text redactor masks that an app can render as page text must be masked in screenshots too."""
    masked = {p.pattern for p in Redactor.mask_patterns()}
    for probe in ("M1001", "123-45-6789", "10012346", "$2,450.17", "1984-03-12"):
        assert Redactor().text(probe) != probe
        assert any(re.search(p, probe) for p in masked), probe
