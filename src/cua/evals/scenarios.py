"""Replay scenario catalog: drives tests/test_stress.py, scripts/stress.py (evidence), and `cua eval replay`.

Each scenario pairs an injected browser condition with the exact outcome the result contract
must report. Categories follow the error taxonomy: baseline, business outcome, recoverable,
escalation, hard failure, drift, safety.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from cua.config import Settings
from cua.contracts import Capability, TenantConfig
from cua.operator import ScriptedOperator
from cua.redactor import Redactor
from cua.replay.engine import ReplayEngine
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank import data

from cua.config import ROOT
FAST = dict(checkpoint_timeout=1.0, slow_load_budget=3.5, retry_budget=3, action_timeout=2.0,
            claim_timeout=1.0, hold_timeout=5.0, checkpoint_retries=1, approval_timeout=1.0)
SAVINGS, SUBACCOUNT = "get_savings_balance", "open_sub_account"
M1001 = {"member_id": "M1001"}
SUB = {"member_id": "M1001", "account_type": "MONEY_MARKET", "initial_deposit": "100.00"}


# ---------------------------------------------------------------- scripted human actions (on the live page)

def _main(page):
    return page.frame(name="main")


def acknowledge_dialog(page) -> None:
    """A human acknowledges the security dialog if one is showing; otherwise there is nothing to do."""
    btn = _main(page).get_by_role("button", name="Acknowledge")
    if btn.count() and btn.first.is_visible():
        btn.first.click()


def sign_in(page) -> None:
    """A human signs in if the sign-in form is showing; otherwise there is nothing to do."""
    f = _main(page)
    if not f.locator("input[name=u]").count():
        return
    f.locator("input[name=u]").fill(data.OPERATOR_USERNAME)
    f.locator("input[name=p]").fill(data.OPERATOR_PASSWORD)
    f.get_by_role("button", name="Sign In").click()
    f.wait_for_load_state()


# ---------------------------------------------------------------- catalog


@dataclass
class Scenario:
    id: str
    category: str
    title: str
    capability: str
    inputs: dict[str, str]
    bucket: str
    code: str | None = None  # FailureReason or BusinessCode expected on the result
    faults: dict[str, Any] = field(default_factory=dict)
    tenant: str = "tenant_a"
    operator: Callable[[], ScriptedOperator | None] = lambda: None
    created: int | None = None  # sub-accounts the app must hold afterwards
    recoveries: list[str] = field(default_factory=list)  # recovery kinds that must be reported
    handoffs: int | None = None
    max_ui_actions: int | None = None
    shows: str = ""  # what this proves, for the evidence summary


def op(**kw) -> Callable[[], ScriptedOperator]:
    return lambda: ScriptedOperator(**kw)


def rounds(*actions_per_round) -> Callable[[], ScriptedOperator]:
    return op(rounds=[(list(a), "RESUME") for a in actions_per_round])


SCENARIOS: list[Scenario] = [
    # baseline
    Scenario("X01", "baseline", "Savings balance, clean run", SAVINGS, M1001, "SUCCESS",
             shows="Happy path; sensitive balance returned to caller, masked on disk"),
    Scenario("X02", "baseline", "Open sub-account, operator approves", SUBACCOUNT, SUB, "SUCCESS", operator=op(approve=True),
             created=1, shows="Irreversible commit gated by a single-use approval token"),
    # business outcomes
    Scenario("X03", "business", "Member not found", SAVINGS, {"member_id": "M9999"}, "BUSINESS_OUTCOME", "MEMBER_NOT_FOUND",
             shows="'No such member' is an answer, not a crash"),
    Scenario("X04", "business", "Deposit below app minimum", SUBACCOUNT, {**SUB, "initial_deposit": "5.00"},
             "BUSINESS_OUTCOME", "VALIDATION_REJECTED", operator=op(), created=0,
             shows="App-side validation is a business outcome; caller input was valid"),
    Scenario("X05", "business", "Frozen member", SUBACCOUNT, {**SUB, "member_id": "M1006"}, "BUSINESS_OUTCOME",
             "VALIDATION_REJECTED", operator=op(), created=0),
    Scenario("X06", "business", "Operator declines the commit", SUBACCOUNT, SUB, "BUSINESS_OUTCOME", "DECLINED_BY_OPERATOR",
             operator=op(approve=False), created=0, shows="Denial is a legitimate answer; nothing committed"),
    # recoverable
    Scenario("X07", "recoverable", "Known maintenance interstitial", SAVINGS, M1001, "SUCCESS", faults={"interstitial": True},
             recoveries=["KNOWN_DIALOG"], shows="Known dialog dismissed and logged, not surfaced as failure"),
    Scenario("X08", "recoverable", "Slow page within budget (1.8s)", SAVINGS, M1001, "SUCCESS", faults={"slow_ms": 1800},
             recoveries=["SLOW_LOAD"]),
    Scenario("X09", "recoverable", "Transient HTTP 500 once", SAVINGS, M1001, "SUCCESS", faults={"transient_500": 1},
             recoveries=["TRANSIENT_APP_ERROR"]),
    Scenario("X10", "recoverable", "Accounts table rendered late by script", SAVINGS, M1001, "SUCCESS",
             faults={"late_render_ms": 1500}, recoveries=["SLOW_LOAD"], shows="Target appears late; waited within budget"),
    Scenario("X11", "recoverable", "Known native alert on page load", SAVINGS, M1001, "SUCCESS",
             faults={"native_alert": "known"}, recoveries=["NATIVE_DIALOG"]),
    Scenario("X12", "recoverable", "Native confirm() guarding the commit", SUBACCOUNT, SUB, "SUCCESS",
             faults={"confirm_prompt": True}, operator=op(approve=True), created=1, recoveries=["NATIVE_DIALOG"],
             shows="Browser confirm accepted only because it is a known prompt, after operator approval"),
    Scenario("X13", "recoverable", "Slow irreversible commit within budget (1.5s)", SUBACCOUNT, SUB, "SUCCESS",
             faults={"slow_confirm_ms": 1500}, operator=op(approve=True), created=1, recoveries=["SLOW_LOAD"],
             shows="Waited for the commit response; never re-submitted"),
    # escalation
    Scenario("X14", "escalation", "Unknown dialog, human acknowledges", SAVINGS, M1001, "ESCALATED",
             faults={"unknown_dialog": True}, operator=rounds([acknowledge_dialog]), handoffs=1,
             shows="Same live session handed to a human and back after checkpoint verification"),
    Scenario("X15", "escalation", "Session expired, human signs in", SAVINGS, M1001, "ESCALATED",
             faults={"session_expire_at": 5}, operator=rounds([sign_in]), handoffs=1,
             shows="Credentials never typed by automation; human re-authenticates on the same session"),
    Scenario("X16", "escalation", "Session expires mid-commit flow, human signs in, operator approves", SUBACCOUNT, SUB,
             "ESCALATED", faults={"session_expire_at": 6}, operator=lambda: ScriptedOperator(
                 rounds=[([sign_in], "RESUME")], approve=True), created=1, handoffs=1,
             shows="Handoff and irreversible approval in one run; exactly one commit"),
    Scenario("X17", "escalation", "Compound: interstitial + unknown dialog + session expiry", SAVINGS, M1001, "ESCALATED",
             faults={"interstitial": True, "unknown_dialog": True, "session_expire_at": 5},
             operator=rounds([acknowledge_dialog], [sign_in]), recoveries=["KNOWN_DIALOG"], handoffs=2,
             shows="One recovery plus two separate handoffs, each with its own transition log"),
    Scenario("X18", "escalation", "Unknown dialog, nobody claims", SAVINGS, M1001, "FAILURE", "HANDOFF_FAILED",
             faults={"unknown_dialog": True}, operator=op(claim=False), handoffs=1),
    Scenario("X19", "escalation", "Unknown dialog, operator cancels", SAVINGS, M1001, "ESCALATED",
             faults={"unknown_dialog": True}, operator=op(rounds=[([], "CANCEL")]), handoffs=1),
    # hard failures
    Scenario("X20", "hard_failure", "Persistent HTTP 500", SAVINGS, M1001, "FAILURE", "APP_ERROR", faults={"persistent_500": True},
             recoveries=["TRANSIENT_APP_ERROR"] * 3, shows="Retry budget spent, then a clear APP_ERROR"),
    Scenario("X21", "hard_failure", "Slow page beyond budget (6s)", SAVINGS, M1001, "FAILURE", "TIMEOUT", faults={"slow_ms": 6000}),
    Scenario("X22", "hard_failure", "Ambiguous target (two Savings rows)", SAVINGS, {"member_id": "M1002"}, "FAILURE",
             "AMBIGUOUS_TARGET", max_ui_actions=2, shows="Stops instead of guessing which row to click"),
    Scenario("X23", "hard_failure", "Permission denied by the app", SAVINGS, {"member_id": "M1005"}, "FAILURE", "PERMISSION_DENIED"),
    Scenario("X24", "hard_failure", "Malformed caller input", SAVINGS, {"member_id": "abc"}, "FAILURE", "VALIDATION_ERROR",
             max_ui_actions=0, shows="Rejected before the UI is touched"),
    Scenario("X25", "hard_failure", "Unknown native alert", SAVINGS, M1001, "FAILURE", "UNRECOVERABLE_BLOCKER",
             faults={"native_alert": "unknown"}, shows="Unknown browser prompt dismissed and the run stopped"),
    Scenario("X26", "hard_failure", "Invisible overlay intercepts clicks", SAVINGS, M1001, "FAILURE", "ACTION_FAILED",
             faults={"obscure_links": True}, recoveries=["ACTION_RETRY"] * 3,
             shows="Element found but not clickable: bounded retries, then ACTION_FAILED"),
    Scenario("X27", "hard_failure", "Input silently truncated by the page", SAVINGS, M1001, "FAILURE", "ACTION_FAILED",
             faults={"truncate_input": True}, max_ui_actions=1, shows="FILL is verified; a truncated value never gets submitted"),
    Scenario("X28", "hard_failure", "Irreversible commit slower than budget (6s)", SUBACCOUNT, SUB, "FAILURE", "TIMEOUT",
             faults={"slow_confirm_ms": 6000}, operator=op(approve=True), created=1,
             shows="Committed once, never retried; failure tells the operator to verify in the app"),
    # drift (secondary: the UI is stable, but drift must still be caught)
    Scenario("X29", "drift", "Accounts table columns reordered", SAVINGS, M1001, "FAILURE", "TARGET_NOT_FOUND",
             faults={"column_reorder": True}),
    Scenario("X30", "drift", "Output label renamed", SAVINGS, M1001, "FAILURE", "TARGET_NOT_FOUND", faults={"label_rename": True}),
    Scenario("X31", "drift", "Content frame renamed", SAVINGS, M1001, "FAILURE", "DRIFT_DETECTED", faults={"frame_rename": True},
             max_ui_actions=0),
    Scenario("X32", "drift", "App version drift (tenant_c on 4.3.0)", SAVINGS, M1001, "FAILURE", "DRIFT_DETECTED",
             tenant="tenant_c", max_ui_actions=0),
    Scenario("X33", "drift", "Relabelled tenant with overlay (tenant_b)", SAVINGS, M1001, "SUCCESS", tenant="tenant_b",
             shows="One base artifact reused across tenants via a label/locator overlay"),
    # safety
    Scenario("X34", "safety", "Entry point redirects off the allowlist", SAVINGS, M1001, "FAILURE", "POLICY_VIOLATION",
             faults={"start_redirect": True}, max_ui_actions=0),
    Scenario("X35", "safety", "Mid-flow redirect off the allowlist", SAVINGS, M1001, "FAILURE", "POLICY_VIOLATION",
             faults={"summary_redirect": True}, shows="Allowlist checked while waiting on every checkpoint"),
    Scenario("X36", "safety", "PII echoed on an error page", SAVINGS, {"member_id": "M9999"}, "BUSINESS_OUTCOME",
             "MEMBER_NOT_FOUND", faults={"pii_in_error": True}, shows="Evidence and logs stay free of SSN, DOB, email"),
    Scenario("X37", "safety", "Irreversible step with no operator", SUBACCOUNT, SUB, "FAILURE", "HANDOFF_FAILED", created=0),
    Scenario("X38", "safety", "Approval never arrives", SUBACCOUNT, SUB, "FAILURE", "HANDOFF_FAILED", operator=op(approve=None),
             created=0),
]


# ---------------------------------------------------------------- execution


def reset_app(base_url: str, tenant: str, faults: dict[str, Any]) -> None:
    body = json.dumps({"tenant": tenant, "faults": faults}).encode()
    urllib.request.urlopen(urllib.request.Request(f"{base_url}/__admin/reset", body, {"Content-Type": "application/json"}))


def app_created(base_url: str) -> int:
    return len(json.loads(urllib.request.urlopen(f"{base_url}/__admin/state").read())["created"])


def load_cap(name: str) -> Capability:
    return Capability.model_validate_json((ROOT / "artifacts" / name / "v1.json").read_text())


def run_scenario(sc: Scenario, base_url: str, runs_dir: Path, redactor: Redactor | None = None,
                 tenants: dict[str, TenantConfig] | None = None):
    """Fresh browser session per scenario, then replay the golden artifact. Returns (report, problems)."""
    redactor = redactor or Redactor(secrets=[data.OPERATOR_PASSWORD])
    surface = PlaywrightSurface(base_url, redactor=redactor, action_timeout=FAST["action_timeout"]).start()
    try:
        surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
        reset_app(base_url, sc.tenant, sc.faults)
        engine = ReplayEngine(surface, runs_dir=runs_dir, redactor=redactor, console=sc.operator(),
                              settings=Settings.load(**FAST), overlay_root=ROOT, tenants=tenants)
        report = engine.run(load_cap(sc.capability), sc.inputs, sc.tenant, run_id=f"{sc.id}-{_slug(sc.title)}")
    finally:
        surface.close()
    return report, check(sc, report, base_url)


def check(sc: Scenario, report, base_url: str) -> list[str]:
    r, problems = report.result, []
    if r.bucket != sc.bucket:
        problems.append(f"bucket {r.bucket} != {sc.bucket}")
    got_code = getattr(r, "reason", None) or getattr(r, "code", None)
    got_code = getattr(got_code, "value", got_code)
    if sc.code and got_code != sc.code:
        problems.append(f"code {got_code} != {sc.code}")
    kinds = [x.kind for x in r.recoveries]
    for k in set(sc.recoveries):
        if kinds.count(k) < sc.recoveries.count(k):
            problems.append(f"recoveries {kinds} missing {sc.recoveries}")
    if sc.handoffs is not None:
        n = len(getattr(r, "handoffs", []) or [])
        if n != sc.handoffs:
            problems.append(f"handoffs {n} != {sc.handoffs}")
    if sc.max_ui_actions is not None and report.ui_actions > sc.max_ui_actions:
        problems.append(f"ui_actions {report.ui_actions} > {sc.max_ui_actions}")
    if sc.created is not None:
        n = app_created(base_url)
        if n != sc.created:
            problems.append(f"app holds {n} new sub-accounts, expected {sc.created}")
    return problems


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40].rstrip("-")
