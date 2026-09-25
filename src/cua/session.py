"""SessionController and the control-token state machine (D5, D6, D7; backbone item 9).

One token per live session decides who may act on the browser. Automation acts
are rejected unless the token is AUTOMATION. Control only returns to automation
through checkpoint verification. The irreversible approval uses the same operator
channel but never transfers the token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Protocol

from .config import Settings
from .contracts import HandoffRecord
from .evidence import EvidenceSink
from .policy import ApprovalToken, PolicyEngine
from .redactor import Redactor
from .surface import ControlTokenHeld, HumanEvent, Surface


class TokenState(str, Enum):
    AUTOMATION = "AUTOMATION"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    HUMAN_IN_CONTROL = "HUMAN_IN_CONTROL"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    VERIFYING_CHECKPOINT = "VERIFYING_CHECKPOINT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = TokenState
TRANSITIONS: dict[TokenState, set[TokenState]] = {
    S.AUTOMATION: {S.PAUSE_REQUESTED},
    S.PAUSE_REQUESTED: {S.HUMAN_IN_CONTROL, S.FAILED, S.CANCELLED},
    S.HUMAN_IN_CONTROL: {S.RESUME_REQUESTED, S.CANCELLED},
    S.RESUME_REQUESTED: {S.VERIFYING_CHECKPOINT},
    S.VERIFYING_CHECKPOINT: {S.AUTOMATION, S.HUMAN_IN_CONTROL, S.FAILED},
    S.FAILED: set(),
    S.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


class _ConsoleError(Exception):
    pass


class ControlToken:
    def __init__(self) -> None:
        self.state = S.AUTOMATION
        self.history: list[str] = []

    def transition(self, to: TokenState, reason: str = "") -> None:
        if to not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {to.value}")
        self.history.append(f"{self.state.value}->{to.value}" + (f" ({reason})" if reason else ""))
        self.state = to


# ---------------------------------------------------------------- operator channel


@dataclass
class InterventionRequest:
    run_id: str
    capability: str
    step: str
    reason: str
    screenshot_ref: str  # masked
    page_text: str  # redacted


@dataclass
class ApprovalRequest:
    run_id: str
    capability: str
    step: str
    summary: dict[str, Any]  # redacted
    screenshot_ref: str


Pump = Callable[[], None]
HumanDecision = Literal["RESUME", "CANCEL"]


class OperatorConsole(Protocol):
    def request_intervention(self, req: InterventionRequest) -> None: ...
    def wait_for_claim(self, timeout: float, pump: Pump) -> str | None: ...
    def wait_for_human(self, timeout: float, pump: Pump, page: Any) -> HumanDecision | None: ...
    def notify(self, message: str) -> None: ...
    def request_approval(self, req: ApprovalRequest, timeout: float, pump: Pump) -> tuple[bool | None, str]: ...


# ---------------------------------------------------------------- controller


@dataclass
class ApprovalDecision:
    outcome: Literal["APPROVED", "DENIED", "TIMEOUT"]
    token: ApprovalToken | None = None
    operator: str | None = None


@dataclass
class HandoffResult:
    record: HandoffRecord
    verdict: str | None = None  # "NEXT" (human completed the step) or "RETRY" (retry the blocked step)


@dataclass
class SessionController:
    surface: Surface
    console: OperatorConsole | None
    policy: PolicyEngine
    evidence: EvidenceSink
    redactor: Redactor
    settings: Settings
    run_id: str
    token: ControlToken = field(default_factory=ControlToken)
    human_events: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.surface.set_act_guard(self.guard)

    @property
    def can_escalate(self) -> bool:
        return self.console is not None

    def guard(self) -> None:
        """Installed on the Surface seam: every automation act passes through here."""
        if self.token.state != S.AUTOMATION:
            self.evidence.log("act_rejected", token=self.token.state.value)
            raise ControlTokenHeld(f"control token held: {self.token.state.value}")

    @staticmethod
    def _console(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            raise _ConsoleError(f"{type(e).__name__}: {str(e).splitlines()[0][:160]}") from None

    def _pump(self) -> None:
        self.surface.pump()

    def _capture(self, ev: HumanEvent) -> None:
        if self.token.state != S.HUMAN_IN_CONTROL:
            return
        rec = self.redactor.value({"type": ev.type, "frame": ev.frame or "", "tag": ev.tag, "role": ev.role,
                                   "name": ev.name, "label": ev.label})
        self.human_events.append(rec)
        self.evidence.log("human_event", **rec)

    def handoff(self, capability: str, step: str, reason: str, verify: Callable[[], str | None]) -> HandoffResult:
        if self.console is None:
            raise RuntimeError("handoff requested with no operator console")
        try:
            return self._handoff(capability, step, reason, verify)
        except _ConsoleError as e:  # a broken operator channel fails the handoff; it never crashes the run
            self.evidence.log("operator_error", detail=str(e))
            if self.token.state not in (S.FAILED, S.CANCELLED):
                self.token.state = S.FAILED
                self.token.history.append(f"->FAILED (operator console error: {e})")
            return HandoffResult(HandoffRecord(step=step, reason=reason, outcome="FAILED",
                                               transitions=self.token.history[-3:]))
        finally:
            self.surface.set_event_sink(None)  # human input capture never outlives the handoff

    def _handoff(self, capability: str, step: str, reason: str, verify: Callable[[], str | None]) -> HandoffResult:
        t = self.token
        start_events = len(self.human_events)
        start_transitions = len(t.history)
        t.transition(S.PAUSE_REQUESTED, reason)
        ref = self.evidence.capture(self.surface.snapshot(), "intervention")
        req = InterventionRequest(self.run_id, capability, step, reason, ref,
                                  self.redactor.text(self.surface.frame_text("main"))[:2000])
        self.evidence.log("intervention_requested", capability=capability, step=step, reason=reason, screenshot=ref)
        self._console(self.console.request_intervention, req)

        def done(outcome: str, operator: str | None = None, verdict: str | None = None) -> HandoffResult:
            self.surface.set_event_sink(None)
            rec = HandoffRecord(step=step, reason=reason, outcome=outcome, operator=operator,
                                transitions=t.history[start_transitions:],
                                human_events=len(self.human_events) - start_events)
            self.evidence.log("handoff_ended", outcome=outcome, transitions=rec.transitions,
                              seconds_in_control=round(time.time() - t_claim, 2) if operator else 0.0)
            return HandoffResult(rec, verdict)

        t_request = time.time()
        operator = self._console(self.console.wait_for_claim, self.settings.claim_timeout, self._pump)
        t_claim = time.time()
        if operator is None:
            t.transition(S.FAILED, "claim timeout")
            return done("FAILED")
        t.transition(S.HUMAN_IN_CONTROL, f"claimed by {operator}")
        self.evidence.log("claimed", operator=operator, seconds_to_claim=round(t_claim - t_request, 2))
        self.surface.set_event_sink(self._capture)
        failures = 0
        while True:
            decision = self._console(self.console.wait_for_human, self.settings.hold_timeout, self._pump,
                                     self.surface.human_page())
            if decision != "RESUME":
                t.transition(S.CANCELLED, "operator cancelled" if decision == "CANCEL" else "hold timeout")
                return done("CANCELLED", operator)
            t.transition(S.RESUME_REQUESTED, "human requested resume")
            self.surface.set_event_sink(None)
            t.transition(S.VERIFYING_CHECKPOINT, "human input released")
            verdict = verify()
            if verdict:
                t.transition(S.AUTOMATION, f"checkpoint passed, {verdict}")
                return done("RESUMED", operator, verdict)
            failures += 1
            if failures > self.settings.checkpoint_retries:
                t.transition(S.FAILED, "checkpoint retries exhausted")
                return done("FAILED", operator)
            t.transition(S.HUMAN_IN_CONTROL, "checkpoint failed, back to human")
            self.console.notify("Checkpoint did not pass. Please finish the step, then resume.")
            self.surface.set_event_sink(self._capture)

    def request_approval(self, capability: str, step: str, summary: dict[str, Any]) -> ApprovalDecision:
        """Irreversible gate (D7). The control token stays with automation."""
        if self.console is None:
            raise RuntimeError("approval requested with no operator console")
        ref = self.evidence.capture(self.surface.snapshot(), "approval")
        req = ApprovalRequest(self.run_id, capability, step, self.redactor.value(summary), ref)
        self.evidence.log("approval_requested", capability=capability, step=step, summary=req.summary)
        t_req = time.time()
        approved, operator = self.console.request_approval(req, self.settings.approval_timeout, self._pump)
        self.evidence.log("approval_decided", step=step, seconds=round(time.time() - t_req, 2),
                          outcome="TIMEOUT" if approved is None else ("APPROVED" if approved else "DENIED"))
        if approved is None:
            self.evidence.log("approval_timeout", step=step)
            return ApprovalDecision("TIMEOUT")
        if not approved:
            self.evidence.log("approval_denied", step=step, operator=operator)
            return ApprovalDecision("DENIED", operator=operator)
        token = self.policy.issue_token(self.run_id, step, operator)
        self.evidence.log("approval_granted", step=step, operator=operator, token_id=token.token_id)
        return ApprovalDecision("APPROVED", token, operator)
