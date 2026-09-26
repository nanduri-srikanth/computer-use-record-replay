"""Records the browser footage for the video walkthrough (docs/walkthrough/).

Drives real runs against the mock bank in a headless, video-recording browser. Automation actions are
outlined and paced so a viewer can follow them; nothing else about the runs is changed. Clips and a
caption timeline land in docs/walkthrough/build/ and scripts/make_walkthrough.py turns them into the video.

    PYTHONPATH=src:. .venv/bin/python scripts/record_walkthrough.py            # replay + handoff clips (no key)
    scripts/with_api_key.sh env PYTHONPATH=src:. .venv/bin/python scripts/record_walkthrough.py --discovery
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from playwright.sync_api import sync_playwright  # noqa: E402

from cua.config import Settings  # noqa: E402
from cua.contracts import ActionType  # noqa: E402
from cua.operator import ScriptedOperator  # noqa: E402
from cua.redactor import Redactor  # noqa: E402
from cua.replay.engine import ReplayEngine  # noqa: E402
from cua.store import ArtifactStore  # noqa: E402
from cua.surface.playwright_surface import _CAPTURE_JS, PlaywrightSurface  # noqa: E402
from cua.evals.scenarios import FAST, SUB, load_cap, reset_app  # noqa: E402
from mockbank import data  # noqa: E402
from mockbank.app import MockBankServer  # noqa: E402

BUILD = ROOT / "docs" / "walkthrough" / "build"
VIEW = {"width": 800, "height": 540}
VIDEO = VIEW
COLORS = {ActionType.CLICK: "#2563eb", ActionType.FILL: "#2563eb", ActionType.SELECT: "#2563eb",
          ActionType.EXTRACT: "#16a34a"}
SETTINGS = Settings.load(**{**FAST, "checkpoint_timeout": 3.0, "hold_timeout": 60.0, "claim_timeout": 10.0,
                            "approval_timeout": 30.0, "discovery_max_steps": 25})

_BANNER_JS = """([text, color]) => {
  let b = document.getElementById('__walk');
  if (!b) { b = document.createElement('div'); b.id = '__walk'; document.body.appendChild(b); }
  b.textContent = text;
  b.style.cssText = `position:fixed;top:0;left:0;right:0;padding:8px 14px;font:bold 15px sans-serif;
    color:#fff;background:${color};z-index:99999;pointer-events:none;text-align:center`;
}"""
_OUTLINE_JS = "(el, c) => { el.style.outline = '3px solid ' + c; el.style.outlineOffset = '2px'; }"


class RecordingSurface(PlaywrightSurface):
    """PlaywrightSurface with a video-recording context and visible, paced actions."""

    def __init__(self, *a, clip: str, pace: float = 0.7, **kw):
        super().__init__(*a, **kw)
        self.clip, self.pace, self.t0 = clip, pace, 0.0

    def start(self) -> "RecordingSurface":
        (BUILD / "raw").mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(viewport=VIEW, record_video_dir=str(BUILD / "raw"),
                                                  record_video_size=VIDEO)
        self._context.expose_binding("__cuaEvent", self._on_event)
        self._context.add_init_script(_CAPTURE_JS)
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.action_timeout_ms)
        self.page.on("dialog", self._on_dialog)
        self.t0 = time.time()
        return self

    def close(self) -> None:
        video = self.page.video.path() if self.page and self.page.video else None
        super().close()  # the video file is complete once the context closes
        if video:
            shutil.move(video, BUILD / "clips" / f"{self.clip}.webm")

    def goto(self, route: str) -> None:
        super().goto(route)
        time.sleep(self.pace)

    def _act(self, action, match, value):
        h = match.handle
        if not isinstance(h, tuple):
            try:
                h.evaluate(_OUTLINE_JS, COLORS.get(action, "#2563eb"))
            except Exception:
                pass
        time.sleep(self.pace)
        out = super()._act(action, match, value)
        time.sleep(self.pace * 0.6)
        return out


def banner(page, text: str, color: str = "#ea580c") -> None:
    try:
        page.frame(name="main").evaluate(_BANNER_JS, [text, color])
    except Exception:
        pass


def human_sign_in(page) -> None:
    """The operator signs back in by hand, on the same live browser session."""
    f = page.frame(name="main")
    if not f.locator("input[name=u]").count():
        return
    banner(page, "OPERATOR IN CONTROL: automation is paused and its actions are rejected")
    time.sleep(1.5)
    for sel, text in (("input[name=u]", data.OPERATOR_USERNAME), ("input[name=p]", data.OPERATOR_PASSWORD)):
        f.locator(sel).evaluate(_OUTLINE_JS, "#ea580c")
        f.locator(sel).press_sequentially(text, delay=80)
        time.sleep(0.3)
    f.get_by_role("button", name="Sign In").click()
    f.wait_for_load_state()
    banner(page, "Operator resumed: replay verifies the checkpoint before continuing", "#16a34a")
    time.sleep(1.5)


def human_acknowledge(page) -> None:
    btn = page.frame(name="main").get_by_role("button", name="Acknowledge")
    if btn.count() and btn.first.is_visible():
        banner(page, "OPERATOR IN CONTROL: the human decides on the security prompt")
        time.sleep(1.5)
        btn.first.evaluate(_OUTLINE_JS, "#ea580c")
        time.sleep(0.8)
        btn.first.click()
        time.sleep(1.0)


class VisibleApprover(ScriptedOperator):
    """Approves like ScriptedOperator, but shows the approval request on screen first."""

    surface: RecordingSurface | None = None

    def request_approval(self, req, timeout, pump):
        banner(self.surface.page, f"APPROVAL REQUIRED: irreversible step '{req.step}': operator "
                                  f"{'approves' if self.approve else 'declines'}", "#7c3aed")
        time.sleep(2.5)
        return super().request_approval(req, timeout, pump)


REPLAYS = [  # clip, capability, inputs, faults, operator factory
    ("replay-success", "get_savings_balance", {"member_id": "M1001"}, {}, None),
    ("replay-not-found", "get_savings_balance", {"member_id": "M9999"}, {}, None),
    ("replay-ambiguous", "get_savings_balance", {"member_id": "M1002"}, {}, None),
    ("replay-wrong-member", "get_savings_balance", {"member_id": "M1001"}, {"wrong_member": "M1003"}, None),
    ("replay-handoff", "get_savings_balance", {"member_id": "M1001"}, {"session_expire_at": 5},
     lambda: ScriptedOperator(rounds=[([human_sign_in], "RESUME")])),
    ("replay-dialog", "get_savings_balance", {"member_id": "M1001"}, {"unknown_dialog": True},
     lambda: ScriptedOperator(rounds=[([human_acknowledge], "RESUME")])),
    ("replay-approval", "open_sub_account", SUB, {}, lambda: VisibleApprover(approve=True)),
    ("replay-policy", "get_savings_balance", {"member_id": "M1001"}, {"summary_redirect": True}, None),
]


def record_replays(base_url: str, redactor: Redactor, only: set[str]) -> list[dict]:
    rows = []
    for clip, cap, inputs, faults, op in REPLAYS:
        if only and clip not in only:
            continue
        surface = RecordingSurface(base_url, redactor=redactor, action_timeout=SETTINGS.action_timeout, clip=clip)
        surface.start()
        console = op() if op else None
        if isinstance(console, VisibleApprover):
            console.surface = surface
        try:
            surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
            reset_app(base_url, "tenant_a", faults)
            engine = ReplayEngine(surface, runs_dir=BUILD / "runs", redactor=redactor, console=console,
                                  settings=SETTINGS, overlay_root=ROOT)
            rep = engine.run(load_cap(cap), inputs, "tenant_a", run_id=f"walk-{clip}")
            time.sleep(1.5)
        finally:
            surface.close()
        result = rep.result.model_dump(mode="json", exclude_none=True)  # what the caller receives
        on_disk = json.loads((BUILD / "runs" / rep.run_id / "result.json").read_text())["result"]
        rows.append({"clip": clip, "capability": cap, "inputs": inputs, "result": result, "on_disk": on_disk})
        print(f"[replay] {clip}: {result['bucket']} {result.get('reason') or result.get('code') or ''}")
    return rows


DISCOVERIES = [  # clip, spec, faults, operator factory
    ("discovery-savings", "get_savings_balance", {}, lambda: ScriptedOperator()),
    ("discovery-expired", "get_savings_balance", {"session_expire_at": 4},
     lambda: ScriptedOperator(rounds=[([human_sign_in], "RESUME")] * 2)),
]


def record_discoveries(base_url: str, redactor: Redactor, only: set[str]) -> list[dict]:
    from cua.discovery.agent import DiscoveryAgent, DiscoverySpec

    store = ArtifactStore(BUILD / "artifacts", redactor)
    rows = []
    for clip, spec, faults, op in DISCOVERIES:
        if only and clip not in only:
            continue
        surface = RecordingSurface(base_url, redactor=redactor, action_timeout=SETTINGS.action_timeout,
                                   clip=clip, pace=0.5)
        surface.start()
        try:
            surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
            reset_app(base_url, "tenant_a", faults)
            agent = DiscoveryAgent(surface, store=store, runs_dir=BUILD / "runs", redactor=redactor,
                                   console=op(), settings=SETTINGS)
            result = agent.discover(DiscoverySpec.load(ROOT / "specs" / f"{spec}.yaml"), "tenant_a")
            time.sleep(1.5)
        finally:
            surface.close()
        # caption timeline: each tool call, from when it started (seconds into the clip)
        captions = [{"t": round(e["ts"] - e.get("seconds", 0) - surface.t0, 2), "tool": e["tool"],
                     "reason": e["input"].get("reason") or e["input"].get("summary", "")}
                    for e in result.events if e["kind"] == "tool"]
        steps = [f"{s.action.value} {s.target.description}" for s in result.capability.steps] \
            if result.capability else []
        rows.append({"clip": clip, "status": result.status, "reason": result.reason, "turns": result.turns,
                     "run": result.run_id, "captions": captions, "steps": steps,
                     "models": sorted({e["model"] for e in result.events if e["kind"] == "model_usage"})})
        print(f"[discovery] {clip}: {result.status} {result.reason} ({result.turns} turns)")
    return rows


def record_stills(base_url: str, redactor: Redactor) -> None:
    """The same page twice: as the operator sees it and as the Redactor saves it (for the redaction animation)."""
    (BUILD / "stills").mkdir(parents=True, exist_ok=True)
    surface = PlaywrightSurface(base_url, redactor=redactor)
    surface.start()
    try:
        surface.page.set_viewport_size(VIEW)
        surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
        reset_app(base_url, "tenant_a", {})
        surface.page.goto(base_url + "/", wait_until="load")
        surface.page.frame(name="main").goto(base_url + "/member/summary?mid=M1001", wait_until="load")
        (BUILD / "stills" / "redact-raw.png").write_bytes(surface.page.screenshot())
        (BUILD / "stills" / "redact-masked.png").write_bytes(surface.snapshot().png)
    finally:
        surface.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discovery", action="store_true", help="also record live Claude discovery (needs the key)")
    ap.add_argument("--only", nargs="*", default=[], help="clip names to (re)record")
    args = ap.parse_args()
    (BUILD / "clips").mkdir(parents=True, exist_ok=True)
    manifest_path = BUILD / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"replays": [], "discoveries": []}
    redactor = Redactor(secrets=[data.OPERATOR_PASSWORD])
    srv = MockBankServer().start()
    only = set(args.only)
    try:
        record_stills(srv.base_url, redactor)
        new = record_replays(srv.base_url, redactor, only)
        manifest["replays"] = [r for r in manifest["replays"] if r["clip"] not in {n["clip"] for n in new}] + new
        if args.discovery:
            new = record_discoveries(srv.base_url, redactor, only)
            manifest["discoveries"] = [r for r in manifest["discoveries"]
                                       if r["clip"] not in {n["clip"] for n in new}] + new
    finally:
        srv.stop()
    manifest_path.write_text(json.dumps(manifest, indent=2))
    shutil.rmtree(BUILD / "raw", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
