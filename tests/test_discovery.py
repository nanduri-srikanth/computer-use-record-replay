"""Discovery loop (D2) with a scripted model, plus discovery-to-replay round trips (D1).

The live-model variant at the bottom needs credentials and is excluded by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cua.config import PolicyConfig, Settings
from cua.contracts import ArtifactStatus, LocatorKind, RiskTier
from cua.discovery.agent import DiscoveryAgent, DiscoverySpec
from cua.operator import ScriptedOperator
from cua.policy import PolicyEngine
from cua.revision import propose_revision
from cua.store import ArtifactStore

from .conftest import CANARIES, FAST, ROOT
from .fake_llm import SAVINGS_SCRIPT, SUBACCOUNT_SCRIPT, ScriptedLLM, tool


@pytest.fixture
def store(tmp_path, redactor):
    return ArtifactStore(tmp_path / "artifacts", redactor)


@pytest.fixture
def discover(surface, redactor, runs_dir, store):
    def _run(script, spec_name="get_savings_balance", console=None, policy=None, **settings):
        llm = ScriptedLLM(script)
        agent = DiscoveryAgent(surface, store=store, runs_dir=runs_dir, redactor=redactor, console=console,
                               policy=policy, client=llm, settings=Settings.load(**{**FAST, **settings}))
        return agent.discover(DiscoverySpec.load(ROOT / "specs" / f"{spec_name}.yaml"), "tenant_a"), llm
    return _run


def test_discovery_records_draft_that_replays(reset, discover, store, engine):
    result, llm = discover(SAVINGS_SCRIPT)
    assert result.status == "DRAFT_SAVED", result.reason
    cap = result.capability
    assert cap.status == ArtifactStatus.DRAFT and cap.provenance.created_by == "discovery"
    assert [s.action.value for s in cap.steps] == ["FILL", "CLICK", "CLICK", "EXTRACT", "EXTRACT"]
    # ladder: best candidate first; the ambiguous "View" role+name was dropped; no blind coordinate fallback
    # once a semantic locator exists (replay would not use it after drift anyway)
    assert cap.steps[0].target.candidates[0].kind == LocatorKind.LABEL_PROXIMITY
    view = cap.steps[2].target.candidates
    assert view[0].kind == LocatorKind.TABLE_ANCHOR and view[0].anchor_text == "Savings"
    assert all(c.kind != LocatorKind.ROLE_NAME for c in view)
    assert all(c.kind != LocatorKind.COORDINATES for s in cap.steps for c in s.target.candidates)
    assert cap.steps[1].post.text_present == ["Member Summary"]
    # the model saw masked data only
    sent = json.dumps([c["messages"][0]["content"][0]["text"] for c in llm.calls])
    assert "123-45-6789" not in sent and "M1001" not in sent
    # approve, then replay with no LLM, on a different member
    store.approve(cap.name, cap.version)
    reset()
    rep = engine().run(store.load(cap.name, cap.version), {"member_id": "M1003"}, "tenant_a")
    assert rep.result.bucket == "SUCCESS" and rep.result.outputs["balance"] == "0.00"


def test_discovered_artifact_matches_golden_behaviour(reset, discover, store, engine, golden):
    """Discovery is judged by whether its artifact replays like the golden one."""
    result, _ = discover(SAVINGS_SCRIPT)
    store.approve(result.capability.name, 1)
    for member in ("M1001", "M9999", "M1002"):
        reset()
        a = engine().run(store.load("get_savings_balance", 1), {"member_id": member}, "tenant_a").result
        reset()
        b = engine().run(golden("get_savings_balance"), {"member_id": member}, "tenant_a").result
        assert a.bucket == b.bucket, member
        assert getattr(a, "outputs", None) == getattr(b, "outputs", None)


def test_discovery_irreversible_requires_approval(reset, discover, store, app_state):
    op = ScriptedOperator(approve=True)
    result, _ = discover(SUBACCOUNT_SCRIPT, "open_sub_account", console=op)
    assert result.status == "DRAFT_SAVED", result.reason
    confirm = next(s for s in result.capability.steps if s.target.candidates[0].name == "Confirm")
    assert confirm.risk == RiskTier.IRREVERSIBLE
    assert len(op.approvals) == 1 and len(app_state()["created"]) == 1
    assert all(s.value_from is None or s.value_from.startswith("inputs.") for s in result.capability.steps)


def test_discovery_irreversible_denied_stops(reset, discover, app_state):
    result, _ = discover(SUBACCOUNT_SCRIPT, "open_sub_account", console=ScriptedOperator(approve=False))
    assert result.status == "STOPPED" and "not approved" in result.reason
    assert app_state()["created"] == []


def test_discovery_max_steps(reset, discover):
    result, _ = discover(SAVINGS_SCRIPT, discovery_max_steps=2)
    assert result.status == "STOPPED" and result.reason == "max steps reached"
    assert result.capability is None


def test_discovery_policy_block_is_fed_back(reset, discover):
    cfg = PolicyConfig.load()
    policy = PolicyEngine(PolicyConfig(cfg.allowed_hosts, cfg.allowed_routes, ["CLICK", "EXTRACT"],
                                       cfg.write_actions, cfg.irreversible_controls))
    result, llm = discover(SAVINGS_SCRIPT[:1] * 3, policy=policy, discovery_stuck_after=3)
    assert result.status == "STOPPED" and "no operator" in result.reason
    results = [b for m in llm.calls[-1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert results and all("Blocked by policy" in b["content"] and b["is_error"] for b in results)


def test_discovery_stuck_escalates_to_human_and_continues(reset, discover):
    op = ScriptedOperator(rounds=[([], "RESUME")])
    script = [tool("request_human", reason="unsure which account")] + SAVINGS_SCRIPT
    result, _ = discover(script, console=op)
    assert result.status == "DRAFT_SAVED"
    assert op.interventions[0].reason.startswith("STUCK")


def test_human_proposed_revision_creates_new_draft(reset, engine, golden, store, runs_dir):
    from .test_acceptance import acknowledge_dialog
    base = golden("get_savings_balance")
    store.save_draft(base)
    store.approve(base.name, 1)  # through the lifecycle, so the approval is recorded against this content
    reset(fault="F3")
    rep = engine(console=ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")])).run(base, {"member_id": "M1001"},
                                                                                         "tenant_a")
    rev = propose_revision(store, base.name, 1, runs_dir / rep.run_id)
    assert rev.version == 2 and rev.status == ArtifactStatus.DRAFT
    assert rev.provenance.created_by == "human-revision" and rev.provenance.parent_version == 1
    assert "Acknowledge" in rev.provenance.notes
    assert store.load(base.name, 1).status == ArtifactStatus.APPROVED  # never auto-promoted


def test_discovered_artifacts_hold_no_pii(reset, discover, store):
    result, _ = discover(SAVINGS_SCRIPT)
    text = store.load_raw(result.capability.name, result.capability.version)
    for canary in CANARIES:
        assert canary not in text


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs Anthropic credentials")
def test_live_discovery_then_replay(reset, surface, redactor, runs_dir, store, engine):
    agent = DiscoveryAgent(surface, store=store, runs_dir=runs_dir, redactor=redactor,
                           settings=Settings.load(**FAST))
    result = agent.discover(DiscoverySpec.load(ROOT / "specs" / "get_savings_balance.yaml"), "tenant_a")
    assert result.status == "DRAFT_SAVED", result.reason
    store.approve(result.capability.name, result.capability.version)
    reset()
    rep = engine().run(store.load(result.capability.name, result.capability.version), {"member_id": "M1001"},
                       "tenant_a")
    assert rep.result.bucket == "SUCCESS" and rep.result.outputs["balance"] == "2450.17"


# ---------------------------------------------------------------- discovery under stress


def _center(surface, role, name):
    box = surface.page.frame(name="main").get_by_role(role, name=name, exact=True).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_discovery_by_screenshot_coordinates(reset, discover, store, engine, surface):
    """The no-DOM-ref path: the model clicks by screenshot position; the recorder still builds a locator ladder."""
    def click_search_by_point(obs):
        x, y = _center(surface, "button", "Search")
        return "click_point", {"x": x, "y": y, "reason": "Search button in the screenshot"}
    script = [SAVINGS_SCRIPT[0], click_search_by_point, *SAVINGS_SCRIPT[2:]]
    result, _ = discover(script)
    assert result.status == "DRAFT_SAVED", result.reason
    search = result.capability.steps[1]
    assert search.target.candidates[0].kind == LocatorKind.ROLE_NAME and search.target.candidates[0].name == "Search"
    store.approve(result.capability.name, 1)
    reset()
    assert engine().run(store.load("get_savings_balance", 1), {"member_id": "M1001"}, "tenant_a").result.bucket == "SUCCESS"


def test_discovery_dismisses_interstitial_without_recording_it(reset, discover, store, engine):
    reset(fault="F2")
    result, _ = discover([tool("click", ('button "Continue"',)), *SAVINGS_SCRIPT])
    assert result.status == "DRAFT_SAVED", result.reason
    assert all(s.target.candidates[0].name != "Continue" for s in result.capability.steps)
    assert any(e["kind"] == "dialog_dismissed_not_recorded" for e in result.events)
    store.approve(result.capability.name, 1)
    reset(fault="F2")  # replay meets the same interstitial and recovers on its own
    rep = engine().run(store.load("get_savings_balance", 1), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "SUCCESS" and [r.kind for r in rep.result.recoveries] == ["KNOWN_DIALOG"]


def test_dialog_only_takeover_continues_without_restart(reset, discover, store, engine):
    """A human closing a dialog adds no steps, so discovery continues instead of restarting into the dialog again."""
    from .scenarios import acknowledge_dialog
    reset(fault="F3")
    op = ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")])
    script = [SAVINGS_SCRIPT[0], SAVINGS_SCRIPT[1], tool("request_human", reason="security attestation dialog"),
              *SAVINGS_SCRIPT[2:]]
    result, _ = discover(script, console=op)
    assert result.status == "DRAFT_SAVED", result.reason
    kinds = [e["kind"] for e in result.events]
    assert "human_closed_dialog" in kinds and "discovery_restarted" not in kinds
    assert [s.action.value for s in result.capability.steps] == ["FILL", "CLICK", "CLICK", "EXTRACT", "EXTRACT"]


def test_page_changing_takeover_restarts_so_artifact_stays_complete(reset, discover, store, engine):
    from .scenarios import sign_in
    reset(session_expire_at=4)  # search result bounces to the sign-in page
    op = ScriptedOperator(rounds=[([sign_in], "RESUME")])
    script = [SAVINGS_SCRIPT[0], SAVINGS_SCRIPT[1], tool("request_human", reason="sign-in required"),
              *SAVINGS_SCRIPT]
    result, llm = discover(script, console=op)
    assert result.status == "DRAFT_SAVED", result.reason
    assert any(e["kind"] == "discovery_restarted" for e in result.events)
    assert [s.action.value for s in result.capability.steps] == ["FILL", "CLICK", "CLICK", "EXTRACT", "EXTRACT"]
    texts = [b["text"] for m in llm.calls[-1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
             for b in m["content"] if isinstance(b, dict) and b.get("type") == "text"]
    note = next(t for t in texts if "reset to its entry point" in t)
    assert "Sign In" in note  # the model is told what the human did
    store.approve(result.capability.name, 1)
    reset()
    assert engine().run(store.load("get_savings_balance", 1), {"member_id": "M1001"}, "tenant_a").result.bucket == "SUCCESS"


def test_no_restart_after_a_commit(reset, discover, app_state):
    from .scenarios import acknowledge_dialog
    op = ScriptedOperator(approve=True, rounds=[([lambda page: page.frame(name="nav").get_by_role(
        "link", name="Member Search").click()], "RESUME")])
    script = SUBACCOUNT_SCRIPT[:7] + [tool("request_human", reason="want a human check")]
    result, _ = discover(script, "open_sub_account", console=op)
    assert result.status == "STOPPED" and "after an irreversible action" in result.reason
    assert len(app_state()["created"]) == 1  # committed once, never replayed by a restart


def test_discovery_stop_keeps_evidence(reset, discover, runs_dir):
    result, _ = discover(SAVINGS_SCRIPT, discovery_max_steps=2)
    stopped = next(e for e in result.events if e["kind"] == "discovery_stopped")
    assert stopped["evidence"] and (runs_dir / f"{stopped['evidence']}.txt").exists()
