"""PolicyEngine: allowlist, risk tiers, and approval tokens (backbone item 6).

Shared by discovery and replay so both phases enforce identical rules.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import PolicyConfig
from .contracts import ActionType, Capability, RiskTier


class PolicyViolation(Exception):
    """Our allowlist or approval rules refused something. kind: host, route, action, approval_token."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ApprovalToken:
    """Single use, bound to one run and one step, signed so it cannot be forged."""

    token_id: str
    run_id: str
    step_id: str
    operator: str
    issued_at: float
    signature: str


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig.load()
        self._key = secrets.token_bytes(32)
        self._consumed: set[str] = set()

    # ---- allowlist -------------------------------------------------------------

    def route_allowed(self, path: str) -> bool:
        return any(path == r or (r.endswith("/") and r != "/" and path.startswith(r))
                   for r in self.config.allowed_routes)

    def check_url(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme in ("about", "data") or not u.netloc:
            return  # blank frames during load
        if u.hostname not in self.config.allowed_hosts:
            raise PolicyViolation(f"host {u.hostname} not on allowlist", "host")
        if not self.route_allowed(u.path or "/"):
            raise PolicyViolation(f"route {u.path} not on allowlist", "route")

    def check_action(self, action: ActionType) -> None:
        if action.value not in self.config.allowed_actions:
            raise PolicyViolation(f"action {action.value} not on allowlist", "action")

    def preflight(self, cap: Capability) -> None:
        """Every step must be expressible under the allowlist before the UI is touched."""
        if not self.route_allowed(cap.start_route):
            raise PolicyViolation(f"start route {cap.start_route} not on allowlist", "route")
        for s in cap.steps:
            self.check_action(s.action)
            if s.url and not self.route_allowed(urlparse(s.url).path):
                raise PolicyViolation(f"step {s.id} navigates to {s.url}, not on allowlist", "route")

    # ---- risk --------------------------------------------------------------------

    def classify(self, action: ActionType, route: str, control_name: str | None,
                 submits_post: bool = False) -> RiskTier:
        """Fails closed: explicit rule, commit keyword, or a POST form submit all mean IRREVERSIBLE."""
        name = (control_name or "").strip().lower()
        if action == ActionType.CLICK:
            if submits_post:
                return RiskTier.IRREVERSIBLE
            for rule in self.config.irreversible_controls:
                if route.startswith(rule["route_prefix"]) and name == rule["name"].lower():
                    return RiskTier.IRREVERSIBLE
            if any(re.search(rf"\b{re.escape(k)}\b", name) for k in self.config.irreversible_keywords):
                return RiskTier.IRREVERSIBLE
        if action.value in self.config.write_actions:
            return RiskTier.REVERSIBLE_WRITE
        return RiskTier.READ

    @staticmethod
    def effective(declared: RiskTier, classified: RiskTier) -> RiskTier:
        """An artifact can raise its tier but never lower it below the policy classification."""
        return declared if declared.rank >= classified.rank else classified

    # ---- approval tokens ------------------------------------------------------------

    def _sign(self, token_id: str, run_id: str, step_id: str, operator: str, issued_at: float) -> str:
        msg = f"{token_id}|{run_id}|{step_id}|{operator}|{issued_at}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def issue_token(self, run_id: str, step_id: str, operator: str) -> ApprovalToken:
        tid, now = secrets.token_hex(8), time.time()
        return ApprovalToken(tid, run_id, step_id, operator, now, self._sign(tid, run_id, step_id, operator, now))

    def consume_token(self, token: ApprovalToken, run_id: str, step_id: str) -> None:
        expected = self._sign(token.token_id, token.run_id, token.step_id, token.operator, token.issued_at)
        if not hmac.compare_digest(expected, token.signature):
            raise PolicyViolation("approval token signature invalid", "approval_token")
        if (token.run_id, token.step_id) != (run_id, step_id):
            raise PolicyViolation("approval token bound to a different run or step", "approval_token")
        if token.token_id in self._consumed:
            raise PolicyViolation("approval token already used", "approval_token")
        self._consumed.add(token.token_id)
