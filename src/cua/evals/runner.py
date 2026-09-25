"""Eval runners. Output follows the eval report contract so scripts/build-report-lite.mjs renders it:

    evals/<flow>/_state.json                       metric declarations (first binary = headline)
    evals/<flow>/<variant>/results.jsonl           one row per (case, rep), written as each completes
    evals/<flow>/<variant>/traces/<id>_rep<k>.json full transcript per row
    evals/<flow>/<variant>/errors.jsonl            harness/serving failures, never scored as model failures

Variants are `baseline`, `v1`, `v2`, ...; re-running skips (case, rep) pairs already written.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

import yaml

from ..config import ROOT, Settings
from ..discovery.agent import SYSTEM as DISCOVERY_SYSTEM
from ..discovery.agent import DiscoveryAgent, DiscoverySpec
from ..metrics import cost_usd
from ..redactor import Redactor
from ..replay.engine import ReplayEngine
from ..store import ArtifactStore
from ..surface.playwright_surface import PlaywrightSurface
from .discovery_cases import CASES, DiscoveryCase
from .graders import grade_discovery, grade_replay, result_code
from .judge import CRITERIA, Judge, build_transcript
from .scenarios import FAST, SCENARIOS, Scenario, app_created, reset_app, run_scenario

EVALS = ROOT / "evals"


def load_config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config" / "evals.yaml").read_text())


def _passwords() -> list[str]:
    from mockbank import data
    return [data.OPERATOR_PASSWORD]


class _Sink:
    """Row writer with resume at the (case, rep) key."""

    def __init__(self, flow_dir: Path, variant: str):
        self.dir = flow_dir / variant
        (self.dir / "traces").mkdir(parents=True, exist_ok=True)
        self.results, self.errors = self.dir / "results.jsonl", self.dir / "errors.jsonl"
        self.done = {(r["prompt_id"], r["rep"]) for r in self._rows(self.results)}

    @staticmethod
    def _rows(p: Path) -> list[dict]:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    def row(self, row: dict, trace: list[dict]) -> None:
        (self.dir / "traces" / f"{row['prompt_id']}_rep{row['rep']}.json").write_text(json.dumps(trace, indent=1))
        with self.results.open("a") as f:
            f.write(json.dumps(row) + "\n")
        self.done.add((row["prompt_id"], row["rep"]))

    def error(self, case_id: str, rep: int, cls: str, detail: str, **extra: Any) -> None:
        with self.errors.open("a") as f:
            f.write(json.dumps({"prompt_id": case_id, "rep": rep, "class": cls, "detail": detail[:2000], **extra}) + "\n")


def _state(flow_dir: Path, metrics: list[dict], perf: list[dict], note: str) -> None:
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "_state.json").write_text(json.dumps({"metrics": metrics, "perf_fields": perf, "note": note}, indent=2))


def _event_trace(system: str, prompt: str, events: list[dict], final: dict) -> list[dict]:
    turns = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    for e in events:
        k = e["kind"]
        if k == "observation":
            turns.append({"role": "user", "content": e["text"]})
        elif k == "tool":
            turns.append({"role": "tool_call", "name": e["tool"], "content": json.dumps(e["input"], indent=1)})
            turns.append({"role": "tool_result", "content": e["result"]})
        elif k not in ("model_usage", "ts"):
            detail = {x: v for x, v in e.items() if x not in ("ts", "kind")}
            turns.append({"role": "tool_call", "name": k, "content": json.dumps(detail, indent=1)})
    turns.append({"role": "assistant", "content": json.dumps(final, indent=1)})
    return turns


# ================================================================ replay eval (deterministic, free)

REPLAY_METRICS = [
    {"id": "correct", "label": "Correct", "kind": "binary"},
    {"id": "bucket_ok", "label": "Bucket", "kind": "binary"},
    {"id": "reason_ok", "label": "Reason", "kind": "binary"},
    {"id": "false_success", "label": "False SUCCESS", "kind": "binary", "better": "lower"},
    {"id": "recoveries_ok", "label": "Recoveries", "kind": "binary"},
]


def run_replay_eval(base_url: str, variant: str = "baseline", reps: int = 1,
                    scenarios: list[Scenario] | None = None, out: Path = EVALS / "replay",
                    log: Callable[[str], None] = print) -> Path:
    _state(out, REPLAY_METRICS, [{"id": "latency_s", "label": "latency", "unit": "s"},
                                 {"id": "ui_actions", "label": "UI actions"}],
           "Replay eval: scenario catalog graded as an eval (no LLM).")
    sink = _Sink(out, variant)
    for sc in scenarios or SCENARIOS:
        for rep in range(reps):
            if (sc.id, rep) in sink.done:
                continue
            t0 = time.time()
            try:
                report, problems = run_scenario(sc, base_url, sink.dir / "runs")
            except Exception as e:  # noqa: BLE001
                sink.error(sc.id, rep, "harness_error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                continue
            events = [json.loads(x) for x in (sink.dir / "runs" / report.run_id / "events.jsonl").read_text().splitlines()]
            grade = grade_replay(sc, report, problems)
            row = {"prompt_id": sc.id, "prompt": f"{sc.title} (expect {sc.bucket} {sc.code or ''})".strip(),
                   "tags": [sc.category, sc.capability], "rep": rep, "status": "ok", "grade": grade,
                   "explanation": {"correct": "; ".join(problems) or "matched"},
                   "expected": {"bucket": sc.bucket, "code": sc.code},
                   "actual": {"bucket": report.result.bucket, "code": result_code(report.result)},
                   "latency_s": round(time.time() - t0, 2), "ui_actions": report.ui_actions,
                   "perf": {"latency_s": round(time.time() - t0, 2), "ui_actions": report.ui_actions},
                   "meta": {"run_id": report.run_id}}
            persisted = json.loads((sink.dir / "runs" / report.run_id / "result.json").read_text())["result"]
            sink.row(row, _event_trace("Deterministic replay (no LLM).", row["prompt"], events,
                                       persisted))  # the masked on-disk copy, never the in-memory outputs
            log(f"{sc.id} {'PASS' if grade['correct'] else 'FAIL'} {report.result.bucket} {result_code(report.result)}")
    return sink.dir


# ================================================================ discovery eval (live model + judge)

DISCOVERY_METRICS = [
    {"id": "task_success", "label": "Task success", "kind": "binary"},
    {"id": "draft_as_expected", "label": "Draft right", "kind": "binary"},
    {"id": "replayability", "label": "Replayable", "kind": "float", "scale": 1},
    {"id": "escalation_ok", "label": "Escalation", "kind": "binary"},
    {"id": "commits_ok", "label": "Commits", "kind": "binary"},
    {"id": "no_dialog_steps", "label": "No dialog step", "kind": "binary"},
    {"id": "stayed_in_bounds", "label": "In bounds", "kind": "binary"},
    {"id": "tool_validity", "label": "Tool validity", "kind": "float", "scale": 1},
    {"id": "efficiency", "label": "Efficiency", "kind": "float", "scale": 1},
] + [{"id": k, "label": v["label"], "kind": "binary"} for k, v in CRITERIA.items()]

DISCOVERY_PERF = [{"id": "cost_usd", "label": "cost", "unit": "$"}, {"id": "judge_cost_usd", "label": "judge $", "unit": "$"},
                  {"id": "latency_s", "label": "latency", "unit": "s"}, {"id": "turns", "label": "turns"},
                  {"id": "tool_calls", "label": "tool calls"}, {"id": "policy_blocks", "label": "policy blocks"},
                  {"id": "in_tokens", "label": "in tok"}, {"id": "out_tokens", "label": "out tok"},
                  {"id": "fallback_share", "label": "fallback"}]


def run_discovery_eval(base_url: str, *, client: Any, judge_client: Any | None, variant: str = "baseline",
                       reps: int | None = None, cases: list[DiscoveryCase] | None = None,
                       model: str | None = None, judge_model: str | None = None, out: Path = EVALS / "discovery",
                       settings: Settings | None = None, assert_model: bool = True,
                       log: Callable[[str], None] = print) -> Path:
    cfg = load_config()
    model = model or cfg["discovery_model"]
    judge_model = judge_model or cfg["judge_model"]
    if judge_client is not None and judge_model == model:
        raise ValueError("the judge must not be the model under test")
    reps = reps or cfg["discovery_reps"]
    settings = settings or Settings.load(**{**FAST, "discovery_max_steps": 25, "discovery_timeout": 300.0})
    _state(out, DISCOVERY_METRICS, DISCOVERY_PERF,
           f"Discovery eval: {model} under test, {judge_model} as judge. Primary = task_success (end state).")
    sink = _Sink(out, variant)
    judge = Judge(judge_client, judge_model) if judge_client is not None else None
    redactor = Redactor(secrets=_passwords())
    for case in cases or CASES:
        for rep in range(reps):
            if (case.id, rep) in sink.done:
                continue
            try:
                row, trace = _discovery_case(case, rep, base_url, client, judge, model, settings, redactor, sink,
                                             assert_model)
            except _ServedModelMismatch as e:
                sink.error(case.id, rep, "served_model_mismatch", str(e))
                continue
            except _AgentError as e:
                sink.error(case.id, rep, "agent_error", str(e))
                log(f"{case.id} rep{rep} AGENT ERROR (not scored): {str(e)[:120]}")
                continue
            except Exception as e:  # noqa: BLE001
                sink.error(case.id, rep, "harness_error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                log(f"{case.id} rep{rep} ERROR {type(e).__name__}: {e}")
                continue
            sink.row(row, trace)
            g = row["grade"]
            log(f"{case.id} rep{rep} task_success={g['task_success']:.0f} "
                f"judge={ {k: g.get(k) for k in CRITERIA} } ${row['cost_usd'] or 0:.3f}")
    return sink.dir


class _ServedModelMismatch(Exception):
    pass


class _AgentError(Exception):
    pass


def _discovery_case(case: DiscoveryCase, rep: int, base_url: str, client: Any, judge: Judge | None, model: str,
                    settings: Settings, redactor: Redactor, sink: _Sink, assert_model: bool):
    from mockbank import data
    spec = DiscoverySpec.load(ROOT / "specs" / f"{case.spec}.yaml")
    scratch = Path(tempfile.mkdtemp(prefix=f"eval-{case.id}-"))
    store = ArtifactStore(scratch, redactor)
    t0 = time.time()
    surface = PlaywrightSurface(base_url, redactor=redactor, action_timeout=settings.action_timeout).start()
    try:
        surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
        reset_app(base_url, case.tenant, case.faults)
        agent = DiscoveryAgent(surface, store=store, runs_dir=sink.dir / "runs", redactor=redactor,
                               console=case.operator(), settings=settings, client=client, model=model)
        result = agent.discover(spec, case.tenant)
    finally:
        surface.close()
    latency = round(time.time() - t0, 2)
    commits = app_created(base_url)
    events = result.events
    if result.status == "STOPPED" and result.reason.startswith("unexpected error"):
        # "no answer" is not a "negative answer": a crash must never score as a correct refusal to save
        raise _AgentError(result.reason)
    usage = [e for e in events if e["kind"] == "model_usage"]
    served = {u["model"] for u in usage}
    allowed = [model] + list(load_config().get("discovery_fallback_models", []))
    if assert_model and any(not any(s.startswith(a) for a in allowed) for s in served):
        raise _ServedModelMismatch(f"requested {model} (fallbacks {allowed[1:]}), served {sorted(served)}")
    fallback_turns = sum(1 for u in usage if not u["model"].startswith(model))

    probe_reports = []
    artifact = None
    if result.capability is not None:
        store.approve(result.capability.name, result.capability.version)
        cap = store.load(result.capability.name, result.capability.version)
        artifact = json.loads(store.load_raw(cap.name, cap.version))
        for probe in case.probes:
            ps = PlaywrightSurface(base_url, redactor=redactor, action_timeout=settings.action_timeout).start()
            try:
                ps.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
                reset_app(base_url, case.tenant, {})
                eng = ReplayEngine(ps, runs_dir=sink.dir / "probes", redactor=redactor, console=probe.operator(),
                                   settings=settings, overlay_root=ROOT)
                probe_reports.append((probe, eng.run(cap, probe.inputs, case.tenant,
                                                     run_id=f"{case.id}-rep{rep}-{probe.label}")))
            finally:
                ps.close()
    shutil.rmtree(scratch, ignore_errors=True)

    grade, why = grade_discovery(case, result, events, probe_reports, commits)
    judge_usage = {"input_count": 0, "output_count": 0}
    verdicts = []
    if judge is not None:
        transcript = build_transcript(spec.goal, events, artifact, f"{result.status}: {result.reason}")
        for v in judge.grade_all(transcript):
            verdicts.append(v)
            judge_usage["input_count"] += v.usage.get("input_count", 0)
            judge_usage["output_count"] += v.usage.get("output_count", 0)
            if v.score is not None:
                grade[v.criterion] = v.score
            why[v.criterion] = f"{v.verdict}: {v.reasoning} | evidence: {v.evidence}"
    li, lo = sum(u["input_count"] for u in usage), sum(u["output_count"] for u in usage)
    cr, cw = sum(u["cache_read"] for u in usage), sum(u["cache_write"] for u in usage)
    tools = [e for e in events if e["kind"] == "tool"]
    run_cost = sum(cost_usd(u["model"], u["input_count"], u["output_count"], u["cache_read"], u["cache_write"]) or 0
                   for u in usage)
    judge_cost = cost_usd(judge.model, judge_usage["input_count"], judge_usage["output_count"]) if judge else 0.0
    perf = {"cost_usd": run_cost, "judge_cost_usd": judge_cost, "latency_s": latency, "turns": result.turns,
            "tool_calls": len(tools), "policy_blocks": sum(1 for e in events if e["kind"] == "policy_blocked"),
            "in_tokens": li + cr + cw, "out_tokens": lo,
            "fallback_share": round(fallback_turns / len(usage), 3) if usage else 0.0}
    row = {"prompt_id": case.id, "prompt": f"{case.title}. Goal: {spec.goal}", "tags": case.tags + [case.tenant],
           "rep": rep, "status": "ok", "grade": grade, "explanation": why,
           "model": sorted(served)[0] if len(served) == 1 else "mixed", "served_models": sorted(served),
           "judge_model": judge.model if judge else None, "usage": {"input_tokens": li, "output_tokens": lo,
                                                                   "cache_read_input_tokens": cr,
                                                                   "cache_creation_input_tokens": cw},
           "judge_usage": judge_usage, **perf, "perf": perf,
           "meta": {"run_id": result.run_id, "status": result.status, "reason": result.reason,
                    "commits": commits, "probes": [(p.label, r.result.bucket, result_code(r.result))
                                                   for p, r in probe_reports],
                    "judge": [v.__dict__ for v in verdicts], "notes": case.notes}}
    trace = _event_trace(DISCOVERY_SYSTEM, row["prompt"], events,
                         {"status": result.status, "reason": result.reason, "artifact": artifact})
    return row, trace


def regrade_flow(flow_dir: Path, variant: str, cases: list[DiscoveryCase] | None = None) -> int:
    """Re-apply the current event-derived graders to stored rows (after a case expectation or grader change).

    Only checks computable from the saved events are recomputed (escalation_ok, stayed_in_bounds); end-state
    checks that needed the live app (replayability, commits) are kept. task_success is then recombined.
    Returns the number of rows whose task_success changed.
    """
    by_id = {c.id: c for c in (cases or CASES)}
    p = flow_dir / variant / "results.jsonl"
    rows, changed = [json.loads(x) for x in p.read_text().splitlines() if x.strip()], 0
    for r in rows:
        case, g = by_id[r["prompt_id"]], r["grade"]
        events = [json.loads(x) for x in (flow_dir / variant / "runs" / r["meta"]["run_id"] / "events.jsonl")
                  .read_text().splitlines()]
        escalated = any(e["kind"] == "intervention_requested" for e in events)
        if case.must_escalate is not None:
            g["escalation_ok"] = float(escalated == case.must_escalate)
            r["explanation"]["escalation_ok"] = f"escalated={escalated}, required={case.must_escalate} (re-graded)"
        else:
            g.pop("escalation_ok", None)
        g["stayed_in_bounds"] = float(not any(e["kind"] == "policy_blocked" and e.get("stage") == "page"
                                              for e in events))
        checks = [g["draft_as_expected"]] + [1.0 if g[k] == 1.0 else 0.0 for k in
                                             ("replayability", "escalation_ok", "commits_ok", "no_dialog_steps",
                                              "stayed_in_bounds") if k in g]
        new = float(all(v == 1.0 for v in checks))
        changed += int(new != g["task_success"])
        g["task_success"] = new
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return changed
