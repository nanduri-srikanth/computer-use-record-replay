"""Independent review: handoff races, control-token misuse, operator-channel failures, and CLI error paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pytest

from cua.evals.scenarios import acknowledge_dialog, sign_in
from cua.operator import ScriptedOperator
from cua.surface import ControlTokenHeld

from ..conftest import ROOT
from .helpers import events, run_dir_text

SUB = {"member_id": "M1001", "account_type": "MONEY_MARKET", "initial_deposit": "100.00"}


# ---------------------------------------------------------------- control token


def test_automation_cannot_act_while_the_human_holds_the_token(reset, engine, golden, surface, runs_dir):
    reset(fault="F3")
    seen: dict[str, str] = {}

    def racing_automation_then_human_fix(page):
        for name, call in (("goto", lambda: surface.goto("/")), ("reload", lambda: surface.reload("main")),
                           ("dismiss", lambda: surface.click_role("main", "button", "Acknowledge"))):
            try:
                call()
                seen[name] = "acted"
            except ControlTokenHeld:
                seen[name] = "rejected"
        acknowledge_dialog(page)

    op = ScriptedOperator(rounds=[([racing_automation_then_human_fix], "RESUME")])
    rep = engine(console=op).run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert seen == {"goto": "rejected", "reload": "rejected", "dismiss": "rejected"}
    assert rep.result.bucket == "ESCALATED" and rep.result.outputs["currency"] == "USD"
    assert sum(e["kind"] == "act_rejected" for e in events(runs_dir, rep.run_id)) == 3


def test_resume_without_fixing_bounces_back_then_fails(reset, engine, golden):
    reset(fault="F3")
    op = ScriptedOperator(rounds=[([], "RESUME")] * 6)
    rep = engine(console=op).run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "HANDOFF_FAILED"
    trans = " ".join(rep.result.handoffs[0].transitions)
    assert "VERIFYING_CHECKPOINT->HUMAN_IN_CONTROL" in trans and "->FAILED" in trans
    assert op.notifications and op._round == 2  # checkpoint_retries=1: two resume attempts, then stop
    assert rep.ui_actions == 2  # fill + search only; nothing acted during the hold


def test_values_typed_by_the_human_are_never_captured(reset, engine, golden, runs_dir):
    reset(fault="F8")

    def fumble_then_sign_in(page):
        page.frame(name="main").locator("input[name=u]").fill("ZETA-unique-7781-typed-by-human")
        sign_in(page)

    op = ScriptedOperator(rounds=[([fumble_then_sign_in], "RESUME")])
    rep = engine(console=op).run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "ESCALATED"
    assert rep.result.handoffs[0].human_events >= 1
    assert "ZETA-unique-7781" not in run_dir_text(runs_dir, rep.run_id)


@pytest.mark.xfail(strict=True, reason="FINDING (low): the replay allowlist check covers only the top, 'main' and "
                   "step frames (engine.py:444-448), while discovery checks every frame (agent.py:262-263). A frame the "
                   "human navigates off the allowlist during a handoff goes unnoticed, and the run resumes and completes")
def test_session_left_off_allowlist_by_the_human_is_not_resumed(reset, engine, golden, server):
    reset(fault="F3")

    def wander_then_fix(page):
        page.frame(name="nav").goto(server.base_url + "/admin/audit")
        acknowledge_dialog(page)

    op = ScriptedOperator(rounds=[([wander_then_fix], "RESUME")])
    rep = engine(console=op).run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "POLICY_VIOLATION", rep.result


# ---------------------------------------------------------------- operator channel failures


@dataclass
class _ApprovalChannelDown(ScriptedOperator):
    def request_approval(self, req, timeout, pump):
        raise ConnectionError("operator console unreachable")


@dataclass
class _NotifyDown(ScriptedOperator):
    def notify(self, message):
        raise ConnectionError("operator console unreachable")


def test_broken_approval_channel_fails_closed(reset, engine, golden, app_state):
    rep = engine(console=_ApprovalChannelDown()).run(golden("open_sub_account"), SUB, "tenant_a")
    assert rep.result.bucket == "FAILURE" and "ConnectionError" in rep.result.observed
    assert "s7" not in rep.steps_executed and app_state()["created"] == []
    # Note (low): reported as UNEXPECTED_ERROR at step s6, the last *completed* step (engine.py:100), not s7.


@pytest.mark.xfail(strict=True, reason="FINDING (low): SessionController wraps console calls in _console() so a broken "
                   "channel ends as HANDOFF_FAILED, but console.notify is called unwrapped (session.py:222). The failure "
                   "escapes as UNEXPECTED_ERROR and the token is left in HUMAN_IN_CONTROL")
def test_broken_notify_channel_fails_the_handoff_not_the_run(reset, engine, golden):
    reset(fault="F3")
    op = _NotifyDown(rounds=[([], "RESUME"), ([acknowledge_dialog], "RESUME")])
    rep = engine(console=op).run(golden("get_savings_balance"), {"member_id": "M1001"}, "tenant_a")
    assert rep.result.bucket == "FAILURE" and rep.result.reason.value == "HANDOFF_FAILED", rep.result


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_root(tmp_path):
    """An isolated CUA_ROOT so CLI runs never write into the repo's artifacts/ or runs/."""
    for d in ("config", "artifacts", "overlays"):
        shutil.copytree(ROOT / d, tmp_path / d)
    return tmp_path


def _cli(root, *args, timeout=120):
    env = {**os.environ, "CUA_ROOT": str(root), "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}"}
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        env.pop(k, None)
    return subprocess.run([sys.executable, "-m", "cua.cli", *args], capture_output=True, text=True, env=env,
                          timeout=timeout, cwd=root)


def test_cli_unknown_capability_exits_cleanly(cli_root, server):
    r = _cli(cli_root, "replay", "no_such_capability", "--inputs", "{}", "--unattended", "--base-url", server.base_url)
    assert r.returncode == 2 and "no APPROVED version" in r.stderr and "Traceback" not in r.stderr


def test_cli_replay_happy_path_and_masking(cli_root, server, reset):
    r = _cli(cli_root, "replay", "get_savings_balance", "--inputs", '{"member_id": "M1001"}', "--unattended",
             "--base-url", server.base_url)
    assert r.returncode == 0, r.stderr[-500:]
    out = json.loads(r.stdout[: r.stdout.rindex("}") + 1])
    assert out["bucket"] == "SUCCESS" and out["outputs"]["balance"] == "2450.17"
    on_disk = next((cli_root / "runs").glob("run-*/result.json")).read_text()
    assert "2450.17" not in on_disk and "[REDACTED:sensitive]" in on_disk


@pytest.mark.xfail(strict=True, reason="FINDING (low): CLI error paths crash with raw tracebacks instead of a clean "
                   "message and exit code: invalid --inputs JSON (cli.py:72), a missing --version file (cli.py:64 via "
                   "store.py:38), and an illegal lifecycle transition (cli.py:51)")
@pytest.mark.parametrize("args", [
    ("replay", "get_savings_balance", "--inputs", "{not json", "--unattended"),
    ("replay", "get_savings_balance", "--version", "99", "--inputs", '{"member_id": "M1001"}', "--unattended"),
    ("approve", "get_savings_balance", "1"),  # already APPROVED
], ids=["bad_json", "missing_version", "illegal_transition"])
def test_cli_error_paths_have_no_traceback(cli_root, server, args):
    r = _cli(cli_root, *args, "--base-url", server.base_url) if args[0] == "replay" else _cli(cli_root, *args)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr[-300:]


@pytest.mark.xfail(strict=True, reason="FINDING (medium): --attended ('allow a DRAFT artifact with an operator present') "
                   "can be combined with --unattended (no operator). The engine gate keys only on the attended flag "
                   "(cli.py:72, engine.py:126), so a DRAFT runs with nobody present")
def test_cli_draft_never_runs_without_an_operator(cli_root, server, reset):
    art = json.loads((cli_root / "artifacts" / "get_savings_balance" / "v1.json").read_text())
    art.update(version=2, status="DRAFT")
    (cli_root / "artifacts" / "get_savings_balance" / "v2.json").write_text(json.dumps(art))
    r = _cli(cli_root, "replay", "get_savings_balance", "--version", "2", "--inputs", '{"member_id": "M1001"}',
             "--attended", "--unattended", "--base-url", server.base_url)
    assert '"bucket": "SUCCESS"' not in r.stdout, "unapproved DRAFT replayed with no operator attached"
