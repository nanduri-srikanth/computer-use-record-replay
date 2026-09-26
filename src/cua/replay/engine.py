"""ReplayEngine (D3; backbone items 4, 5, 7). Deterministic: no LLM anywhere in this package.

Every exit ends in exactly one of SUCCESS, BUSINESS_OUTCOME, ESCALATED, FAILURE, including
unanticipated exceptions. Recoverable conditions are handled here, reported on the result,
and never change the bucket.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, NoReturn

from .. import metrics
from ..config import DetectorPack, Settings, load_tenants
from ..contracts import (ActionType, ArtifactStatus, BusinessCode, BusinessOutcome, Capability, Checkpoint,
                         Escalated, Failure, FailureReason, HandoffRecord, RecoveryRecord, RiskTier, RunReport,
                         RunResult, Step, Success, TenantConfig, Verdict, validate_fields)
from ..evidence import EvidenceSink
from ..locators import AmbiguousTarget, TargetNotFound, resolve_ladder
from ..overlay import OverlayRejected, load_overlay, merge
from ..policy import PolicyEngine, PolicyViolation
from ..redactor import Redactor
from ..session import OperatorConsole, SessionController
from ..surface import ActionFailed, ControlTokenHeld, Match, Surface
from .detectors import Detection, Detectors

POLL = 0.15
Phase = Literal["pre", "target", "action", "post"]
_BUDGET_REASON: dict[str, FailureReason] = {"pre": FailureReason.TIMEOUT, "target": FailureReason.TARGET_NOT_FOUND,
                                            "action": FailureReason.ACTION_FAILED, "post": FailureReason.TIMEOUT}
SENSITIVE_MASK = "[REDACTED:sensitive]"


class _Stop(Exception):
    """Carries a terminal result out of the step loop."""

    def __init__(self, result: RunResult):
        self.result = result


class _NeedRecovery(Exception):
    def __init__(self, phase: Phase, expected: str):
        super().__init__(expected)
        self.phase, self.expected = phase, expected


@dataclass
class _Ctx:
    """All per-run state, so one engine can serve many runs."""

    run_id: str
    cap: Capability
    ev: EvidenceSink
    session: SessionController
    det: Detectors
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    handoffs: list[HandoffRecord] = field(default_factory=list)
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    step_risk: dict[str, RiskTier] = field(default_factory=dict)  # effective tier once resolved
    current: str | None = None  # the step being executed, for failures raised outside a step's own handling
    ui_actions: int = 0


class ReplayEngine:
    def __init__(self, surface: Surface, *, runs_dir: Path, redactor: Redactor, settings: Settings | None = None,
                 policy: PolicyEngine | None = None, console: OperatorConsole | None = None,
                 detectors: DetectorPack | None = None, tenants: dict[str, TenantConfig] | None = None,
                 overlay_root: Path | None = None):
        self.surface = surface
        self.runs_dir = Path(runs_dir)
        self.redactor = redactor
        self.settings = settings or Settings.load()
        self.policy = policy or PolicyEngine()
        self.console = console
        self.pack = detectors or DetectorPack.load()
        self.tenants = tenants if tenants is not None else load_tenants()
        self.overlay_root = overlay_root or Path(".")

    # ================================================================ entry point

    def run(self, cap: Capability, inputs: dict[str, Any], tenant: str, *, unattended: bool = True,
            run_id: str | None = None) -> RunReport:
        run_id = run_id or f"run-{datetime.now():%Y-%m-%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ev = EvidenceSink(self.runs_dir, run_id, self.redactor)
        session = SessionController(self.surface, self.console, self.policy, ev, self.redactor, self.settings, run_id)
        ctx = _Ctx(run_id=run_id, cap=cap, ev=ev, session=session, det=Detectors(self.pack, self.surface))
        self.surface.set_dialog_policy(self._native_dialog_policy)
        ev.log("run_started", capability=cap.name, version=cap.version, tenant=tenant, artifact_hash=cap.content_hash())
        try:
            result = self._run(ctx, cap, inputs, tenant, unattended)
        except _Stop as s:
            result = s.result
        except Exception as e:  # noqa: BLE001 - the contract is one bucket for every exit
            result = self._fail(ctx, FailureReason.UNEXPECTED_ERROR, ctx.current,
                                "run completes without an unhandled error", f"{type(e).__name__}: {e}")
        finally:
            self.surface.set_act_guard(lambda: None)
            self.surface.set_dialog_policy(lambda kind, message: False)
        result = self._classify(ctx, result)
        update: dict[str, Any] = {"recoveries": list(ctx.recoveries)}
        if isinstance(result, Failure):
            update["handoffs"] = list(ctx.handoffs)
        result = result.model_copy(update=update)
        report = RunReport(run_id=run_id, capability=cap.name, version=cap.version, tenant=tenant,
                           inputs_redacted=self._persistable_inputs(inputs, cap),
                           result=result, steps_executed=ctx.executed, ui_actions=ctx.ui_actions, started_at=started,
                           ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        ev.log("run_ended", bucket=result.bucket)
        ev.write_json("result.json", self._persistable(report, ctx.cap))
        try:  # telemetry never breaks a run
            metrics.append(self.runs_dir / "metrics.jsonl", metrics.derive_replay(report, ev.events))
        except Exception as e:  # noqa: BLE001
            ev.log("metrics_error", detail=f"{type(e).__name__}: {e}")
        return report

    # ================================================================ preflight + loop

    def _run(self, ctx: _Ctx, cap: Capability, inputs: dict[str, Any], tenant: str, unattended: bool) -> RunResult:
        # 1. artifact status: only APPROVED runs unattended; anything else needs an operator actually attached
        if cap.status != ArtifactStatus.APPROVED and (unattended or not ctx.session.can_escalate):
            return self._fail(ctx, FailureReason.ARTIFACT_NOT_APPROVED, None,
                              "status APPROVED, or an attended run with an operator console",
                              f"status {cap.status.value}, {'unattended' if unattended else 'no operator console'}",
                              capture=False)
        # 2. tenant overlay -> effective artifact
        tenant_cfg = self.tenants.get(tenant)
        if tenant_cfg is None:
            return self._fail(ctx, FailureReason.DRIFT_DETECTED, None, "registered tenant", f"unknown tenant {tenant}",
                              capture=False)
        overlay_path = tenant_cfg.overlays.get(cap.name)
        try:
            effective = merge(cap, load_overlay(self.overlay_root / overlay_path) if overlay_path else None)
        except OverlayRejected as e:
            return self._fail(ctx, FailureReason.OVERLAY_REJECTED, None,
                              "overlay with label maps and locator overrides only", str(e), capture=False)
        ctx.cap = effective
        ctx.ev.log("effective_artifact", hash=effective.content_hash(), overlay=overlay_path or "none")
        # 3. allowlist
        try:
            self.policy.preflight(effective)
        except PolicyViolation as e:
            return self._policy_fail(ctx, e, "preflight", None, "all steps within allowlist", capture=False)
        # 4. inputs (before the UI is touched)
        try:
            ctx.inputs = validate_fields(effective.inputs, inputs)
        except ValueError as e:
            return self._fail(ctx, FailureReason.VALIDATION_ERROR, None, "inputs match schema", str(e), capture=False)
        # 5. open the app, confirm it stayed on the allowlist, fingerprint tenant + version
        try:
            self.surface.goto(effective.start_route)
            self._check_urls()
        except PolicyViolation as e:
            return self._policy_fail(ctx, e, "start_page", None, "start page within allowlist")
        except ActionFailed as e:
            return self._fail(ctx, FailureReason.APP_ERROR, None, f"app opens at {effective.start_route}", str(e))
        fp = self._await_value(ctx.det.fingerprint, self.settings.checkpoint_timeout + self.settings.slow_load_budget)
        expected = f"tenant {tenant}, version in {effective.compatible_versions} and {tenant_cfg.expected_version}"
        if not fp:
            return self._fail(ctx, FailureReason.DRIFT_DETECTED, None, expected, "no fingerprint on page")
        if (fp["tenant"] != tenant or fp["version"] not in effective.compatible_versions
                or fp["version"] != tenant_cfg.expected_version):
            return self._fail(ctx, FailureReason.DRIFT_DETECTED, None, expected,
                              f"tenant {fp['tenant']}, version {fp['version']}")
        ctx.ev.log("fingerprint_ok", **fp)

        # 6. per-step loop
        for step in effective.steps:
            t_step = time.time()
            ctx.current = step.id
            self._execute(ctx, step)
            ctx.executed.append(step.id)
            ctx.ev.log("step_timing", step=step.id, seconds=round(time.time() - t_step, 3))

        # 7. capability-level success condition, read from a page that is still on the allowlist
        ok = self._await_value(lambda: self._checkpoint_ok(ctx, effective.success), self.settings.checkpoint_timeout)
        try:
            self._check_urls()
        except PolicyViolation as e:
            return self._policy_fail(ctx, e, "mid_flow", ctx.executed[-1], "page within allowlist at completion")
        if not ok:
            reason = (FailureReason.IDENTITY_MISMATCH if self._identity_mismatch(ctx, effective.success)
                      else FailureReason.SUCCESS_CONDITION_UNMET)
            return self._fail(ctx, reason, ctx.executed[-1],
                              f"success condition {effective.success.describe()}", self._observed(effective.success.frame))
        # 8. outputs
        try:
            typed = validate_fields(effective.outputs, ctx.outputs)
        except ValueError as e:
            return self._fail(ctx, FailureReason.OUTPUT_INVALID, None, "outputs match schema", str(e))
        return Success(outputs={k: str(v) for k, v in typed.items()})

    # ================================================================ one step

    def _execute(self, ctx: _Ctx, step: Step) -> None:
        """Runs one step to completion, recovering as needed. Raises _Stop on a terminal result."""
        retries = 0
        human_completed = False
        while True:
            need = self._guarded(ctx, step, lambda: self._attempt(ctx, step, human_completed))
            if need is None:
                return
            while need is not None:  # a recovery can surface a further condition; the retry budget bounds this
                verdict: list[Verdict] = []
                pending = need
                need = self._guarded(ctx, step, lambda: verdict.append(self._recover(ctx, step, pending, retries)))
                retries += 1
            if verdict == ["NEXT"]:
                if pending.phase == "post":
                    return
                human_completed = True

    def _guarded(self, ctx: _Ctx, step: Step, fn: Callable[[], Any]) -> _NeedRecovery | None:
        """Run fn; return a pending recovery, or convert control/policy violations into terminal failures."""
        try:
            fn()
            return None
        except _NeedRecovery as need:
            return need
        except ControlTokenHeld as e:
            self._stop(self._fail(ctx, FailureReason.HANDOFF_FAILED, step.id, "automation holds control token", str(e)))
        except PolicyViolation as e:
            self._stop(self._policy_fail(ctx, e, "mid_flow", step.id, "page and action within policy"))

    def _attempt(self, ctx: _Ctx, step: Step, human_completed: bool) -> None:
        self._check_urls()
        if human_completed:
            return
        if step.pre and not self._pre_ok(ctx, step):
            raise _NeedRecovery("pre", f"pre-checkpoint {step.pre.describe()}")
        try:
            res = resolve_ladder(self.surface, step.target)
        except TargetNotFound as e:
            raise _NeedRecovery("target", str(e)) from None
        except AmbiguousTarget as e:
            self._stop(self._fail(ctx, FailureReason.AMBIGUOUS_TARGET, step.id, "exactly one match", e.detail))
        ctx.ev.log("resolved", step=step.id, candidate=res.candidate_index, tried=res.tried)
        if step.action == ActionType.CLICK and res.match.target_url:
            try:  # refuse before clicking: never navigate off the allowlist and stop afterwards
                self.policy.check_url(res.match.target_url)
            except PolicyViolation as e:
                self._stop(self._policy_fail(ctx, e, "pre_action", step.id, "click destination within allowlist"))
        risk = ctx.step_risk[step.id] = self._risk(step, res.match)
        if risk == RiskTier.IRREVERSIBLE:
            self._approval_gate(ctx, step)
        self._act(ctx, step, res.match, risk)
        if step.action != ActionType.EXTRACT:
            if step.post:
                self._await_post(ctx, step)
            else:
                self._detect_after_action(ctx, step)

    def _risk(self, step: Step, match: Match) -> RiskTier:
        classified = self.policy.classify(step.action, match.frame_route, match.text, match.submits_post)
        return self.policy.effective(step.risk, classified)

    def _act(self, ctx: _Ctx, step: Step, match: Match, risk: RiskTier) -> None:
        value = str(ctx.inputs[step.input_name]) if step.input_name else None
        try:
            out = self.surface.act(step.action, match, value)
        except ActionFailed as e:
            if risk == RiskTier.IRREVERSIBLE:  # never retry a commit: the approval token is spent and a retry could double-submit
                self._stop(self._fail(ctx, FailureReason.ACTION_FAILED, step.id, "irreversible action performed once",
                                      f"{e}; not retried, verify in the app"))
            raise _NeedRecovery("action", f"{step.action.value} on {step.target.description}: {e}") from None
        if step.action != ActionType.EXTRACT:
            ctx.ui_actions += 1
        ctx.ev.log("acted", step=step.id, action=step.action.value, risk=risk.value)
        self._check_native_dialogs(ctx, step)
        if step.action in (ActionType.FILL, ActionType.SELECT):  # every match kind, coordinates included
            actual = self.surface.read_value(match)
            if actual.strip() != (value or "").strip():
                self._stop(self._fail(ctx, FailureReason.ACTION_FAILED, step.id,
                                      f"field holds the supplied {step.input_name} ({len(value or '')} chars)",
                                      f"field holds a different value ({len(actual)} chars); input truncated or rejected"))
        if step.action == ActionType.EXTRACT:
            ctx.outputs[step.output] = out or ""

    # ================================================================ checkpoints and detectors

    def _checkpoint_ok(self, ctx: _Ctx, cp: Checkpoint) -> bool:
        text = self.surface.frame_text(cp.frame)
        return (all(t in text for t in cp.text_present)
                and all(str(ctx.inputs[n]) in text for n in cp.input_names))

    def _identity_mismatch(self, ctx: _Ctx, cp: Checkpoint) -> bool:
        if not cp.input_names:
            return False
        text = self.surface.frame_text(cp.frame)
        return (all(t in text for t in cp.text_present)
                and not all(str(ctx.inputs[n]) in text for n in cp.input_names))

    def _pre_ok(self, ctx: _Ctx, step: Step) -> bool:
        if self.surface.dialog_visible(step.pre.frame, self.pack.dialog_selector) is not None:
            return False
        return self._checkpoint_ok(ctx, step.pre)

    @staticmethod
    def _await_value(fn: Callable[[], Any], timeout: float) -> Any:
        end = time.time() + timeout
        while True:
            v = fn()
            if v or time.time() >= end:
                return v
            time.sleep(POLL)

    def _await_post(self, ctx: _Ctx, step: Step) -> None:
        """Wait for the post-checkpoint, running outcome detectors while waiting."""
        end = time.time() + self.settings.checkpoint_timeout
        while True:
            self._check_urls()  # a redirect off the allowlist stops the run here
            ok = self._checkpoint_ok(ctx, step.post)  # checkpoint first, so the scan below sees the same page
            self._check_urls()  # and again after the read: a navigation that landed meanwhile never counts as passing
            d = ctx.det.scan()
            if d and d.terminal:
                self._outcome(ctx, step, d)
            if ok and d is None:
                return
            if d is not None or time.time() >= end:
                raise _NeedRecovery("post", f"post-checkpoint {step.post.describe()}")
            time.sleep(POLL)

    def _detect_after_action(self, ctx: _Ctx, step: Step) -> None:
        d = ctx.det.scan()
        if d and d.terminal:
            self._outcome(ctx, step, d)

    def _outcome(self, ctx: _Ctx, step: Step, d: Detection) -> NoReturn:
        if d.kind == "BUSINESS":
            if not ctx.cap.declares(d.code):  # the caller branches on declared outcomes only; anything else is a contract gap
                declared = [o.code.value for o in ctx.cap.outcomes]
                self._stop(self._fail(ctx, FailureReason.UNDECLARED_OUTCOME, step.id,
                                      f"a declared outcome {declared} or the post-checkpoint",
                                      f"{d.code}: {d.detail}"))
            ctx.ev.log("business_outcome", step=step.id, code=d.code)
            self._stop(BusinessOutcome(code=BusinessCode(d.code), step=step.id, detail=d.detail))
        self._stop(self._fail(ctx, FailureReason(d.code), step.id, "no failure detector", d.detail))

    def _native_dialog_policy(self, kind: str, message: str) -> bool:
        return any(r["text"] in message and r.get("accept", False) for r in self.pack.native_dialogs)

    def _check_native_dialogs(self, ctx: _Ctx, step: Step) -> None:
        for d in self.surface.pop_native_dialogs():
            if d.accepted:
                self._recovered(ctx, step, "NATIVE_DIALOG", f"accepted known {d.kind}: {d.message[:60]}")
            else:
                self._stop(self._fail(ctx, FailureReason.UNRECOVERABLE_BLOCKER, step.id, "no unknown native dialog",
                                      f"unknown {d.kind} dismissed: {d.message[:120]}"))

    # ================================================================ recovery

    def _recover(self, ctx: _Ctx, step: Step, need: _NeedRecovery, retries: int) -> Verdict:
        """Handle one recoverable condition. Returns a Verdict; raises _Stop on a terminal result."""
        exhausted = retries >= self.settings.retry_budget
        d = ctx.det.scan()
        if d and d.terminal:
            self._outcome(ctx, step, d)
        if d and d.kind == "KNOWN_DIALOG":
            if exhausted:
                self._stop(self._fail(ctx, FailureReason.TIMEOUT, step.id, need.expected, f"dialog keeps returning: {d.code}"))
            try:
                self.surface.click_role(d.frame, d.dismiss["role"], d.dismiss["name"])
            except ActionFailed as e:
                self._stop(self._fail(ctx, FailureReason.ACTION_FAILED, step.id, f"dismiss {d.code}", str(e)))
            self._recovered(ctx, step, "KNOWN_DIALOG", f"dismissed {d.code}")
            return self._recheck(ctx, step, need)
        if d and d.kind in ("UNKNOWN_DIALOG", "BLOCKER"):
            return self._escalate(ctx, step, f"{d.code}: {d.detail}")
        if d and d.kind == "APP_ERROR":
            if need.phase in ("post", "action") and ctx.step_risk.get(step.id) == RiskTier.IRREVERSIBLE:
                self._stop(self._fail(ctx, FailureReason.APP_ERROR, step.id, need.expected,
                                      f"{d.detail} after irreversible step; not retried, verify in the app"))
            if exhausted:
                self._stop(self._fail(ctx, FailureReason.APP_ERROR, step.id, need.expected, f"{d.detail} (persistent)"))
            try:
                self.surface.reload(d.frame)
            except ActionFailed as e:
                self._stop(self._fail(ctx, FailureReason.APP_ERROR, step.id, need.expected, str(e)))
            self._recovered(ctx, step, "TRANSIENT_APP_ERROR", f"reloaded after: {d.detail}")
            return self._recheck(ctx, step, need)
        if exhausted:
            self._stop(self._fail(ctx, self._budget_reason(ctx, step, need), step.id, need.expected,
                                  f"retry budget exhausted; {self._observed(self._frame_of(step))}"))
        if need.phase == "action":
            time.sleep(POLL * 3)
            self._recovered(ctx, step, "ACTION_RETRY", need.expected[:120])
            return "RETRY"
        # Nothing recognisable on screen: treat as a slow load and wait within budget.
        started = time.time()
        end = started + self.settings.slow_load_budget
        while time.time() < end:
            if self._condition_met(ctx, step, need):
                self._recovered(ctx, step, "SLOW_LOAD", f"waited {time.time() - started:.1f}s for {need.phase}")
                return "NEXT" if need.phase == "post" else "RETRY"
            if ctx.det.scan() is not None:
                raise _NeedRecovery(need.phase, need.expected)  # something appeared: classify it
            time.sleep(POLL)
        note = ("irreversible step already performed and not retried; verify the result in the app. "
                if ctx.step_risk.get(step.id) == RiskTier.IRREVERSIBLE and need.phase == "post" else "")
        self._stop(self._fail(ctx, self._budget_reason(ctx, step, need), step.id, need.expected,
                              note + self._observed(self._frame_of(step))))

    def _budget_reason(self, ctx: _Ctx, step: Step, need: _NeedRecovery) -> FailureReason:
        """A checkpoint whose page arrived but shows a different input value is not a slow page: say so."""
        cp = step.pre if need.phase == "pre" else step.post if need.phase == "post" else None
        if cp is not None and self._identity_mismatch(ctx, cp):
            return FailureReason.IDENTITY_MISMATCH
        return _BUDGET_REASON[need.phase]

    def _recheck(self, ctx: _Ctx, step: Step, need: _NeedRecovery) -> Verdict:
        if need.phase != "post":
            return "RETRY"
        self._await_post(ctx, step)  # may raise _NeedRecovery again; the caller's loop handles it
        return "NEXT"

    def _condition_met(self, ctx: _Ctx, step: Step, need: _NeedRecovery) -> bool:
        if need.phase == "pre":
            return self._pre_ok(ctx, step)
        if need.phase == "post":
            return self._checkpoint_ok(ctx, step.post)
        try:
            resolve_ladder(self.surface, step.target)
            return True
        except (TargetNotFound, AmbiguousTarget):
            return False

    def _recovered(self, ctx: _Ctx, step: Step, kind: str, detail: str) -> None:
        ctx.recoveries.append(RecoveryRecord(step=step.id, kind=kind, detail=self.redactor.text(detail)))
        ctx.ev.log("recovered", step=step.id, recovery=kind, detail=detail)

    # ================================================================ handoff + approval

    def _escalate(self, ctx: _Ctx, step: Step, reason: str) -> Verdict:
        if not ctx.session.can_escalate:
            self._stop(self._fail(ctx, FailureReason.UNRECOVERABLE_BLOCKER, step.id, "no blocker", reason))

        def verify() -> Verdict | None:
            if step.post and self._checkpoint_ok(ctx, step.post) and ctx.det.scan() is None:
                return "NEXT"
            if (step.pre is None or self._pre_ok(ctx, step)) and ctx.det.scan() is None:
                return "RETRY"
            return None

        res = ctx.session.handoff(ctx.cap.name, step.id, reason, verify)
        ctx.handoffs.append(res.record)
        if res.record.outcome == "FAILED":
            self._stop(self._fail(ctx, FailureReason.HANDOFF_FAILED, step.id,
                                  "handoff resumes with checkpoint passing", " / ".join(res.record.transitions[-2:])))
        if res.record.outcome == "CANCELLED":
            self._stop(Escalated(reason=f"operator took over at {step.id}: {reason}", handoffs=ctx.handoffs))
        return res.verdict or "RETRY"

    def _approval_gate(self, ctx: _Ctx, step: Step) -> None:
        if not ctx.session.can_escalate:
            self._stop(self._fail(ctx, FailureReason.HANDOFF_FAILED, step.id, "approval channel for irreversible step",
                                  "no operator console configured"))
        summary = {"capability": ctx.cap.name, "step": step.description,
                   **self._persistable_inputs(ctx.inputs, ctx.cap)}
        decision = ctx.session.request_approval(ctx.cap.name, step.id, summary)
        if decision.outcome == "DENIED":
            self._stop(BusinessOutcome(code=BusinessCode.DECLINED_BY_OPERATOR, step=step.id,
                                       detail=f"declined by {decision.operator}"))
        if decision.outcome == "TIMEOUT" or decision.token is None:
            self._stop(self._fail(ctx, FailureReason.HANDOFF_FAILED, step.id, "approval decision before deadline",
                                  "no decision; nothing committed"))
        self.policy.consume_token(decision.token, ctx.run_id, step.id)

    # ================================================================ results

    def _check_urls(self) -> None:
        """Every frame, not just the step's: a human (or a redirect) can leave any frame off the allowlist."""
        for url in self.surface.frame_urls():
            if url:
                self.policy.check_url(url)

    @staticmethod
    def _frame_of(step: Step) -> str | None:
        return (step.target.frame if step.target else None) or "main"

    def _observed(self, frame: str | None) -> str:
        return self.redactor.text(self.surface.frame_text(frame or "main"))[:300]

    def _policy_fail(self, ctx: _Ctx, e: PolicyViolation, stage: str, step: str | None, expected: str,
                     capture: bool = True) -> Failure:
        ctx.ev.log("policy_violation", stage=stage, rule=e.kind, step=step, detail=str(e))
        return self._fail(ctx, FailureReason.POLICY_VIOLATION, step, expected, f"{stage}: {e}", capture=capture)

    def _fail(self, ctx: _Ctx, reason: FailureReason, step: str | None, expected: str, observed: str,
              capture: bool = True) -> Failure:
        ref = None
        if capture:
            try:
                ref = ctx.ev.capture(self.surface.snapshot(), reason.value.lower())
            except Exception:  # noqa: BLE001 - evidence is best effort; the failure itself must still be reported
                ref = None
        f = Failure(reason=reason, step=step, expected=self.redactor.text(expected),
                    observed=self.redactor.text(observed), evidence_ref=ref)
        ctx.ev.log("failure", reason=reason.value, step=step, expected=f.expected, observed=f.observed)
        return f

    @staticmethod
    def _stop(result: RunResult) -> NoReturn:
        raise _Stop(result)

    @staticmethod
    def _classify(ctx: _Ctx, result: RunResult) -> RunResult:
        """Precedence (WORKFLOW OQ#1): if a human held the token, SUCCESS and BUSINESS become ESCALATED."""
        if not ctx.handoffs or isinstance(result, (Failure, Escalated)):
            return result
        reason = "completed after human intervention: " + "; ".join(h.reason for h in ctx.handoffs)
        if isinstance(result, Success):
            return Escalated(reason=reason, handoffs=ctx.handoffs, outputs=result.outputs)
        return Escalated(reason=reason, handoffs=ctx.handoffs, business_code=result.code)

    def _persistable_inputs(self, inputs: dict[str, Any], cap: Capability) -> dict[str, str]:
        """Inputs as persisted: sensitive fields masked outright, the rest pattern-redacted."""
        sensitive = {f.name for f in cap.inputs if f.sensitive}
        return {k: SENSITIVE_MASK if k in sensitive else self.redactor.text(str(v)) for k, v in inputs.items()}

    @staticmethod
    def _persistable(report: RunReport, cap: Capability) -> dict[str, Any]:
        """What goes to disk: sensitive outputs are masked; the caller still receives them in memory."""
        body = report.model_dump(mode="json")
        sensitive = {f.name for f in cap.outputs if f.sensitive}
        outputs = body["result"].get("outputs") or {}
        for k in outputs:
            if k in sensitive:
                outputs[k] = SENSITIVE_MASK
        return body
