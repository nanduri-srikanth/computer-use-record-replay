"""Runtime telemetry: one flat RunMetrics row per real run, appended to a JSONL ledger.

Lives on the production path (called at the end of every replay and discovery run), so it
must stay cheap and deterministic and must never import the LLM client. Everything is derived
from the run's own events and result; no extra instrumentation calls are needed at call sites.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# $/MTok (input, output). Cache reads bill at 0.1x input, cache writes at 1.25x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0), "claude-fable-5-1": (10.0, 50.0),
}


def price_for(model: str) -> tuple[float, float] | None:
    return next((p for m, p in PRICES.items() if model.startswith(m)), None)


def cost_usd(model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    p = price_for(model or "")
    if p is None:
        return None
    pin, pout = p
    return round((inp * pin + out * pout + cache_read * pin * 0.1 + cache_write * pin * 1.25) / 1e6, 6)


class RunMetrics(BaseModel):
    """Flat on purpose: one row per run is easy to aggregate, grep, and ship to any log pipeline."""

    ts: str
    run_id: str
    kind: str  # replay | discovery
    capability: str
    version: int | None = None
    tenant: str | None = None
    bucket: str  # SUCCESS / BUSINESS_OUTCOME / ESCALATED / FAILURE, or DRAFT_SAVED / STOPPED for discovery
    reason: str = ""  # failure reason or business code
    duration_s: float = 0.0
    # execution
    steps_executed: int = 0
    ui_actions: int = 0
    step_seconds: dict[str, float] = Field(default_factory=dict)
    recoveries: dict[str, int] = Field(default_factory=dict)
    # locator health (drift early warning)
    resolutions: int = 0
    fallback_resolutions: int = 0  # resolved by a candidate other than the first
    coordinate_resolutions: int = 0
    ambiguous: int = 0
    # policy
    policy_violations: dict[str, int] = Field(default_factory=dict)  # stage -> count (terminal, replay)
    policy_blocked: dict[str, int] = Field(default_factory=dict)  # stage -> count (fed back, discovery)
    # human in the loop
    handoffs: int = 0
    handoff_outcomes: dict[str, int] = Field(default_factory=dict)
    seconds_to_claim: list[float] = Field(default_factory=list)
    seconds_in_control: float = 0.0
    human_events: int = 0
    resume_first_pass: int = 0  # handoffs whose first checkpoint verification passed
    approvals: dict[str, int] = Field(default_factory=dict)  # APPROVED / DENIED / TIMEOUT
    approval_seconds: list[float] = Field(default_factory=list)
    # safety counters (must stay zero)
    acts_rejected_while_human: int = 0  # attempted and correctly refused (not a violation)
    # discovery / LLM
    turns: int = 0
    tool_calls: dict[str, int] = Field(default_factory=dict)
    tool_errors: int = 0  # calls that made no progress (invalid ref, blocked, no effect, bad extract)
    restarts: int = 0
    llm_model: str = ""  # model that served the last turn
    llm_models: dict[str, int] = Field(default_factory=dict)  # turns served per model
    fallback_turns: int = 0  # turns served by a model other than the one requested (server-side fallback)
    fallback_categories: dict[str, int] = Field(default_factory=dict)
    llm_input: int = 0
    llm_output: int = 0
    llm_cache_read: int = 0
    llm_cache_write: int = 0
    llm_latency_s: float = 0.0
    cost_usd: float | None = None


def _count(events: list[dict], kind: str, key: str) -> dict[str, int]:
    return dict(Counter(str(e.get(key, "")) for e in events if e["kind"] == kind))


def _common(events: list[dict]) -> dict[str, Any]:
    resolved = [e for e in events if e["kind"] == "resolved"]
    ends = [e for e in events if e["kind"] == "handoff_ended"]
    return dict(
        step_seconds={e["step"]: e["seconds"] for e in events if e["kind"] == "step_timing"},
        recoveries=_count(events, "recovered", "recovery"),
        resolutions=len(resolved),
        fallback_resolutions=sum(1 for e in resolved if e.get("candidate", 0) > 0),
        coordinate_resolutions=sum(1 for e in resolved if (e.get("tried") or [""])[-1].startswith("COORDINATES")),
        policy_violations=_count(events, "policy_violation", "stage"),
        policy_blocked=_count(events, "policy_blocked", "stage"),
        handoffs=sum(1 for e in events if e["kind"] == "intervention_requested"),
        handoff_outcomes=dict(Counter(e["outcome"] for e in ends)),
        seconds_to_claim=[e["seconds_to_claim"] for e in events if e["kind"] == "claimed"],
        seconds_in_control=round(sum(e.get("seconds_in_control", 0) or 0 for e in ends), 2),
        human_events=sum(1 for e in events if e["kind"] == "human_event"),
        resume_first_pass=sum(1 for e in ends if e["outcome"] == "RESUMED"
                              and not any("back to human" in t for t in e.get("transitions", []))),
        approvals=_count(events, "approval_decided", "outcome"),
        approval_seconds=[e["seconds"] for e in events if e["kind"] == "approval_decided"],
        acts_rejected_while_human=sum(1 for e in events if e["kind"] == "act_rejected"),
    )


def _duration(events: list[dict]) -> float:
    return round(events[-1]["ts"] - events[0]["ts"], 2) if events else 0.0


def derive_replay(report: Any, events: list[dict]) -> RunMetrics:
    r = report.result
    code = getattr(r, "reason", None) or getattr(r, "code", None) or getattr(r, "business_code", None)
    return RunMetrics(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id=report.run_id, kind="replay",
        capability=report.capability, version=report.version, tenant=report.tenant, bucket=r.bucket,
        reason=getattr(code, "value", code) or "", duration_s=_duration(events),
        steps_executed=len(report.steps_executed), ui_actions=report.ui_actions,
        ambiguous=int(getattr(code, "value", "") == "AMBIGUOUS_TARGET"), **_common(events))


def derive_discovery(result: Any, events: list[dict], capability: str, tenant: str) -> RunMetrics:
    usage = [e for e in events if e["kind"] == "model_usage"]
    tools = [e for e in events if e["kind"] == "tool"]
    model = usage[-1]["model"] if usage else ""
    li, lo = sum(u["input_count"] for u in usage), sum(u["output_count"] for u in usage)
    cr, cw = sum(u["cache_read"] for u in usage), sum(u["cache_write"] for u in usage)
    return RunMetrics(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id=result.run_id, kind="discovery",
        capability=capability, version=result.capability.version if result.capability else None, tenant=tenant,
        bucket=result.status, reason="" if result.status == "DRAFT_SAVED" else result.reason[:120],
        duration_s=_duration(events), steps_executed=len(result.capability.steps) if result.capability else 0,
        turns=result.turns, tool_calls=dict(Counter(t["tool"] for t in tools)),
        tool_errors=sum(1 for t in tools if not t.get("progress") and t["tool"] not in ("request_human", "wait")),
        restarts=sum(1 for e in events if e["kind"] == "discovery_restarted"),
        llm_model=model, llm_models=dict(Counter(u["model"] for u in usage)),
        fallback_turns=sum(1 for u in usage if u.get("requested") and not u["model"].startswith(u["requested"])),
        fallback_categories=_count(events, "model_fallback", "category"),
        llm_input=li, llm_output=lo, llm_cache_read=cr, llm_cache_write=cw,
        llm_latency_s=round(sum(u.get("latency_s", 0) or 0 for u in usage), 2),
        cost_usd=round(sum(cost_usd(u["model"], u["input_count"], u["output_count"], u["cache_read"],
                                    u["cache_write"]) or 0 for u in usage), 6) if usage else None,
        **_common(events))


def append(ledger: Path, row: RunMetrics) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with Path(ledger).open("a") as f:
        f.write(row.model_dump_json() + "\n")


def read(ledger: Path) -> list[RunMetrics]:
    p = Path(ledger)
    if not p.exists():
        return []
    return [RunMetrics.model_validate(json.loads(line)) for line in p.read_text().splitlines() if line.strip()]
