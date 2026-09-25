"""OperatorConsole implementations: interactive CLI (demo) and scripted (tests)."""

from __future__ import annotations

import select
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .session import ApprovalRequest, HumanDecision, InterventionRequest, Pump


def _wait_line(timeout: float, pump: Pump) -> str | None:
    """Read a line from stdin while keeping the browser responsive."""
    end = time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if ready:
            return sys.stdin.readline().strip().lower()
        pump()
    return None


class CLIOperatorConsole:
    """The operator watches the headed browser and answers prompts in the terminal."""

    def __init__(self, operator: str = "operator"):
        self.operator = operator

    def request_intervention(self, req: InterventionRequest) -> None:
        print(f"\n=== INTERVENTION REQUESTED ({req.capability} / {req.step}) ===")
        print(f"reason: {req.reason}\nmasked screenshot: runs/{req.screenshot_ref}.png")

    def wait_for_claim(self, timeout: float, pump: Pump) -> str | None:
        print("Type 'c' + Enter to take control, 'x' to decline.")
        ans = _wait_line(timeout, pump)
        return self.operator if ans == "c" else None

    def wait_for_human(self, timeout: float, pump: Pump, page: Any) -> HumanDecision | None:
        print("You have control of the browser. Fix the blocker, then type 'r' + Enter to resume or 'x' to cancel.")
        ans = _wait_line(timeout, pump)
        return "RESUME" if ans == "r" else ("CANCEL" if ans == "x" else None)

    def notify(self, message: str) -> None:
        print(f"[operator] {message}")

    def request_approval(self, req: ApprovalRequest, timeout: float, pump: Pump) -> tuple[bool | None, str]:
        print(f"\n=== APPROVAL REQUIRED: IRREVERSIBLE STEP ({req.capability} / {req.step}) ===")
        for k, v in req.summary.items():
            print(f"  {k}: {v}")
        print(f"masked screenshot: runs/{req.screenshot_ref}.png\nType 'approve' or 'deny' + Enter.")
        ans = _wait_line(timeout, pump)
        return (None if ans is None else ans == "approve"), self.operator


HumanAction = Callable[[Any], None]


@dataclass
class ScriptedOperator:
    """Deterministic operator for tests.

    claim: take control when asked (False simulates nobody claiming, so claim timeout).
    rounds: one entry per HUMAN_IN_CONTROL round; each is (actions run on the live page, decision).
    approve: True / False / None (None simulates no decision before the deadline).
    """

    claim: bool = True
    rounds: list[tuple[list[HumanAction], HumanDecision | None]] = field(default_factory=list)
    approve: bool | None = True
    operator: str = "op-test"
    interventions: list[InterventionRequest] = field(default_factory=list)
    approvals: list[ApprovalRequest] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    _round: int = 0

    def request_intervention(self, req: InterventionRequest) -> None:
        self.interventions.append(req)

    def wait_for_claim(self, timeout: float, pump: Pump) -> str | None:
        return self.operator if self.claim else None

    def wait_for_human(self, timeout: float, pump: Pump, page: Any) -> HumanDecision | None:
        if self._round >= len(self.rounds):
            return None
        actions, decision = self.rounds[self._round]
        self._round += 1
        for act in actions:
            act(page)
        pump()  # deliver captured DOM events
        return decision

    def notify(self, message: str) -> None:
        self.notifications.append(message)

    def request_approval(self, req: ApprovalRequest, timeout: float, pump: Pump) -> tuple[bool | None, str]:
        self.approvals.append(req)
        return self.approve, self.operator
