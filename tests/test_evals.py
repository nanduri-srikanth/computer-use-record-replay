"""Evals, offline: graders, judge handling, calibration, oracle/null through the discovery runner, scorecard gates."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from cua.evals import calibration, scorecard
from cua.evals.discovery_cases import CASES
from cua.evals.graders import confusion
from cua.evals.judge import Judge, build_transcript
from cua.evals.runner import run_discovery_eval, run_replay_eval

from .conftest import CANARIES
from .fake_llm import SAVINGS_SCRIPT, ScriptedLLM, tool
from .scenarios import SCENARIOS


# ---------------------------------------------------------------- fake judge


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 1000
    output_tokens: int = 100


@dataclass
class _Resp:
    content: list
    model: str = "claude-sonnet-5"
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)


class FakeJudgeClient:
    """Answers each criterion with a verdict from `decide(criterion, transcript)`."""

    def __init__(self, decide):
        self.decide, self.calls = decide, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        crit = text.split("CRITERION ")[1][:2]
        verdict = self.decide(crit, text)
        if verdict == "garbage":
            return _Resp([_Block("not json")])
        if verdict == "refusal":
            return _Resp([_Block("")], stop_reason="refusal")
        return _Resp([_Block(json.dumps({"verdict": verdict, "evidence": "quoted", "reasoning": "because"}))])


def test_judge_parses_structured_output_and_isolates_errors():
    j = Judge(FakeJudgeClient(lambda c, t: {"J1": "pass", "J2": "garbage", "J3": "refusal", "J4": "na"}[c]))
    v = {x.criterion: x for x in j.grade_all("trace")}
    assert v["J1"].verdict == "pass" and v["J1"].score == 1.0 and v["J1"].usage["input_count"] == 1000
    assert v["J2"].verdict == "error" and v["J3"].verdict == "error"  # judge failures never become model zeros
    assert v["J4"].score is None
    kw = j.client.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema" and "untrusted" in kw["system"]


def test_transcript_contains_observations_and_reasons_but_no_pii():
    from .scenarios import ROOT
    real = calibration._load_real(ROOT / "evidence")[0]
    t = build_transcript(real["goal"], real["events"], real["artifact"], "DRAFT_SAVED")
    assert "--- observation" in t and '"reason"' in t and "SAVED ARTIFACT" in t
    assert not [c for c in CANARIES if c in t]


# ---------------------------------------------------------------- calibration


def test_calibration_set_is_balanced_and_labelled():
    items = calibration.build_items()
    assert len(items) >= 30
    by = {(i.criterion, i.label) for i in items}
    assert all((c, lab) in by for c in ("J1", "J2", "J3", "J4") for lab in ("pass", "fail"))
    ids = {i.id for i in items}  # the attestation trace is labelled only in its clear-cut (escalated) form
    assert ("real-savings-unknown-dialog-J3" in ids) == ("corrupt-self-attested" in ids)


def test_calibration_scores_an_oracle_judge_perfectly_and_a_lazy_judge_poorly(tmp_path):
    labels = {i.transcript + i.criterion: i.label for i in calibration.build_items()}

    def oracle(c, text):
        trace = text.split("<trace>\n", 1)[1].rsplit("\n</trace>", 1)[0]
        return labels[trace + c]
    good = calibration.run_calibration(Judge(FakeJudgeClient(oracle)), tmp_path / "good", repeat=4)
    assert good["agreement"] == 1.0 and good["repeat_consistency"] == 1.0
    lazy = calibration.run_calibration(Judge(FakeJudgeClient(lambda c, t: "pass")), tmp_path / "lazy", repeat=0)
    assert lazy["agreement"] < 0.7  # always-pass fails every corruption and negative


# ---------------------------------------------------------------- graders


def test_confusion_matrix_precision_recall():
    c = confusion([("SUCCESS", "SUCCESS"), ("FAILURE", "SUCCESS"), ("FAILURE", "FAILURE")])
    assert c["per_bucket"]["SUCCESS"] == {"precision": 0.5, "recall": 1.0, "n": 1}
    assert c["per_bucket"]["FAILURE"] == {"precision": 1.0, "recall": 0.5, "n": 2}


# ---------------------------------------------------------------- runners: oracle and null


def test_replay_eval_writes_the_report_contract(server, tmp_path):
    subset = [s for s in SCENARIOS if s.id in ("X01", "X03", "X22", "X34")]
    out = run_replay_eval(server.base_url, scenarios=subset, out=tmp_path / "replay", log=lambda *_: None)
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text().splitlines()]
    assert [r["grade"]["correct"] for r in rows] == [1.0] * 4
    assert all((out / "traces" / f"{r['prompt_id']}_rep0.json").exists() for r in rows)
    state = json.loads((tmp_path / "replay" / "_state.json").read_text())
    assert state["metrics"][0] == {"id": "correct", "label": "Correct", "kind": "binary"}
    again = run_replay_eval(server.base_url, scenarios=subset, out=tmp_path / "replay", log=lambda *_: None)
    assert len((again / "results.jsonl").read_text().splitlines()) == 4  # resume: nothing re-run


def _judge_all(verdict):
    return FakeJudgeClient(lambda c, t: verdict)


def test_discovery_eval_oracle_scores_full_marks(server, tmp_path):
    d01 = next(c for c in CASES if c.id == "D01")
    out = run_discovery_eval(server.base_url, client=ScriptedLLM(SAVINGS_SCRIPT), judge_client=_judge_all("pass"),
                             model="scripted", reps=1, cases=[d01], out=tmp_path / "disc", assert_model=False,
                             log=lambda *_: None)
    row = json.loads((out / "results.jsonl").read_text())
    g = row["grade"]
    assert g["task_success"] == 1.0 and g["replayability"] == 1.0 and g["escalation_ok"] == 1.0
    assert g["J1"] == g["J4"] == 1.0 and row["judge_model"] == "claude-sonnet-5"
    assert row["meta"]["probes"][2][1:] == ["FAILURE", "AMBIGUOUS_TARGET"]


def test_discovery_eval_null_scores_zero(server, tmp_path):
    d01 = next(c for c in CASES if c.id == "D01")
    null = ScriptedLLM([tool("done")] * 40)  # declares done without doing anything
    out = run_discovery_eval(server.base_url, client=null, judge_client=None, model="scripted", reps=1,
                             cases=[d01], out=tmp_path / "disc", assert_model=False, log=lambda *_: None)
    g = json.loads((out / "results.jsonl").read_text())["grade"]
    assert g["task_success"] == 0.0 and g["draft_as_expected"] == 0.0 and g["replayability"] == 0.0


def test_negative_case_passes_only_when_nothing_is_saved(server, tmp_path):
    d10 = next(c for c in CASES if c.id == "D10")
    honest = ScriptedLLM([tool("request_human", reason="No loan accounts exist for this member.")] +
                         [tool("done")] * 30)
    out = run_discovery_eval(server.base_url, client=honest, judge_client=None, model="scripted", reps=1,
                             cases=[d10], out=tmp_path / "neg", assert_model=False, log=lambda *_: None,
                             settings=None)
    g = json.loads((out / "results.jsonl").read_text())["grade"]
    assert g["draft_as_expected"] == 1.0 and g["commits_ok"] == 1.0 and g["task_success"] == 1.0


def test_judge_cannot_be_the_model_under_test(server, tmp_path):
    with pytest.raises(ValueError):
        run_discovery_eval(server.base_url, client=ScriptedLLM([]), judge_client=_judge_all("pass"),
                           model="claude-sonnet-5", judge_model="claude-sonnet-5", cases=CASES[:1],
                           out=tmp_path / "x", log=lambda *_: None)


# ---------------------------------------------------------------- scorecard


def test_scorecard_gates_and_ledger(server, tmp_path):
    subset = [s for s in SCENARIOS if s.id in ("X01", "X34", "X35")]
    run_replay_eval(server.base_url, scenarios=subset, out=tmp_path / "replay", log=lambda *_: None)
    ledger = tmp_path / "replay" / "baseline" / "runs" / "metrics.jsonl"
    card = scorecard.build(ledger, out=tmp_path)
    assert card["gate_breaches"] == []  # every scenario matched its expectation
    lr = card["ledger"]["replay"]
    assert lr["policy_violations"] == {"start_page": 1, "mid_flow": 1} and lr["policy_violation_runs"] == 2
    assert any(a.startswith("policy_violation_runs") for a in card["alerts"])  # production alert fires
    assert (tmp_path / "SCORECARD.md").read_text().count("Policy violations") == 1
    # a regressed variant breaches the replay gate
    v1 = tmp_path / "replay" / "v1"
    v1.mkdir()
    rows = [json.loads(x) for x in (tmp_path / "replay" / "baseline" / "results.jsonl").read_text().splitlines()]
    rows[0]["grade"]["correct"] = 0.0
    (v1 / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    card = scorecard.build(ledger, out=tmp_path)
    assert any(b.startswith("replay.correct") for b in card["gate_breaches"])
    assert card["trend"]["replay.correct"]["delta"] < 0


# ---------------------------------------------------------------- bugs the live eval surfaced


def test_no_tool_is_offered_with_an_empty_enum():
    from cua.discovery.agent import DiscoveryAgent, DiscoverySpec
    from .scenarios import ROOT
    agent = DiscoveryAgent.__new__(DiscoveryAgent)
    names = [t["name"] for t in agent._tools(DiscoverySpec.load(ROOT / "specs" / "audit_entry.yaml"))]
    assert "fill" not in names and "select" not in names and "extract" in names
    for t in agent._tools(DiscoverySpec.load(ROOT / "specs" / "audit_entry.yaml")):
        for prop in t["input_schema"]["properties"].values():
            assert prop.get("enum", ["x"]) != []


class _Exploding:
    beta = messages = None

    def __init__(self):
        self.beta = self.messages = self

    def create(self, **kw):
        raise RuntimeError("simulated API outage")


def test_agent_crash_is_an_error_row_not_a_pass(server, tmp_path):
    d10 = next(c for c in CASES if c.id == "D10")  # a negative case: a crash would otherwise look like success
    out = run_discovery_eval(server.base_url, client=_Exploding(), judge_client=None, model="scripted", reps=1,
                             cases=[d10], out=tmp_path / "crash", assert_model=False, log=lambda *_: None)
    assert not (out / "results.jsonl").exists() or not (out / "results.jsonl").read_text().strip()
    err = json.loads((out / "errors.jsonl").read_text())
    assert err["class"] == "agent_error" and "simulated API outage" in err["detail"]


def test_operator_console_crash_fails_the_handoff_not_the_run(reset, engine, golden):
    from cua.operator import ScriptedOperator

    def broken_hands(page):
        raise RuntimeError("operator UI disconnected")
    reset(fault="F3")
    rep = engine(console=ScriptedOperator(rounds=[([broken_hands], "RESUME")])).run(
        golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "HANDOFF_FAILED"


def test_click_toward_an_off_allowlist_page_is_refused_before_it_happens(reset, engine, golden, app_state):
    from cua.contracts import LocatorCandidate, LocatorKind, Step, TargetDescriptor
    reset(audit_link=True)
    cap = golden("get_savings_balance")
    audit = Step(id="s0", action="CLICK", description="open audit console",
                 target=TargetDescriptor(frame="nav", description="Audit Console link", candidates=[
                     LocatorCandidate(kind=LocatorKind.ROLE_NAME, role="link", name="Audit Console")]))
    rep = engine().run(cap.model_copy(update={"steps": [audit, *cap.steps]}), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.reason.value == "POLICY_VIOLATION" and rep.ui_actions == 0
    assert rep.result.observed.startswith("pre_action")


def test_discovery_gateway_blocks_off_allowlist_link_before_navigating(reset, surface, redactor, tmp_path):
    from cua.config import Settings
    from cua.discovery.agent import DiscoveryAgent, DiscoverySpec
    from cua.store import ArtifactStore
    from .conftest import FAST, ROOT
    reset(audit_link=True)
    llm = ScriptedLLM([tool("click", ('link "Audit Console"',))] * 3)
    agent = DiscoveryAgent(surface, store=ArtifactStore(tmp_path / "a"), runs_dir=tmp_path, redactor=redactor,
                           client=llm, settings=Settings.load(**{**FAST, "discovery_stuck_after": 3}))
    result = agent.discover(DiscoverySpec.load(ROOT / "specs" / "audit_entry.yaml"), "tenant_a")
    blocked = [e for e in result.events if e["kind"] == "policy_blocked"]
    assert blocked and all(e["stage"] == "gateway" for e in blocked)  # never "page": it never navigated
    assert "/admin/audit" not in surface.frame_url("main")
