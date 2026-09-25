"""Real discovery runs (Claude) against the live mock app, then replays of what they recorded. Keeps the evidence.

Needs Anthropic credentials (scripts/with_api_key.sh). The operator is scripted here (approves commits,
acknowledges dialogs) so the run is unattended; that mock is documented in evidence/README.md.

    scripts/with_api_key.sh env PYTHONPATH=src:. .venv/bin/python scripts/live_evidence.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from cua.config import Settings  # noqa: E402
from cua.discovery.agent import DiscoveryAgent, DiscoverySpec  # noqa: E402
from cua.operator import ScriptedOperator  # noqa: E402
from cua.redactor import Redactor  # noqa: E402
from cua.replay.engine import ReplayEngine  # noqa: E402
from cua.store import ArtifactStore  # noqa: E402
from cua.surface.playwright_surface import PlaywrightSurface  # noqa: E402
from mockbank import data  # noqa: E402
from mockbank.app import MockBankServer  # noqa: E402
from cua.evals.scenarios import FAST, acknowledge_dialog, reset_app, sign_in  # noqa: E402

OUT = ROOT / "evidence"
SUB = {"member_id": "M1001", "account_type": "MONEY_MARKET", "initial_deposit": "100.00"}

DISCOVERIES = [  # (label, spec, faults during discovery, operator)
    ("savings-clean", "get_savings_balance", {}, lambda: ScriptedOperator()),
    ("subaccount-clean", "open_sub_account", {}, lambda: ScriptedOperator(approve=True)),
    ("savings-interstitial", "get_savings_balance", {"interstitial": True}, lambda: ScriptedOperator()),
    ("savings-unknown-dialog", "get_savings_balance", {"unknown_dialog": True},
     lambda: ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")] * 3)),
    # session expires right after the search: the model cannot sign in, so it must escalate; the human signs in
    ("savings-session-expired", "get_savings_balance", {"session_expire_at": 4},
     lambda: ScriptedOperator(rounds=[([sign_in], "RESUME")] * 2)),
]

REPLAYS = {  # label -> [(scenario label, inputs, faults, operator)]
    "savings-clean": [
        ("success", {"member_id": "M1001"}, {}, None),
        ("not-found", {"member_id": "M9999"}, {}, None),
        ("bad-input", {"member_id": "abc"}, {}, None),
        ("ambiguous", {"member_id": "M1002"}, {}, None),
        ("app-error", {"member_id": "M1001"}, {"persistent_500": True}, None),
        ("unknown-dialog-handoff", {"member_id": "M1001"}, {"unknown_dialog": True},
         lambda: ScriptedOperator(rounds=[([acknowledge_dialog], "RESUME")])),
    ],
    "subaccount-clean": [
        ("approved", SUB, {}, lambda: ScriptedOperator(approve=True)),
        ("declined", SUB, {}, lambda: ScriptedOperator(approve=False)),
        ("below-minimum", {**SUB, "initial_deposit": "5.00"}, {}, lambda: ScriptedOperator()),
    ],
    "savings-interstitial": [("interstitial-recovered", {"member_id": "M1001"}, {"interstitial": True}, None)],
    "savings-unknown-dialog": [("clean-after-dialog-discovery", {"member_id": "M1001"}, {}, None)],
    "savings-session-expired": [("clean-after-handoff-discovery", {"member_id": "M1001"}, {}, None),
                                ("session-expired-handoff", {"member_id": "M1001"}, {"session_expire_at": 5},
                                 lambda: ScriptedOperator(rounds=[([sign_in], "RESUME")]))],
}


def main() -> int:
    for d in ("discovery", "replay", "artifacts"):
        shutil.rmtree(OUT / d, ignore_errors=True)
    redactor = Redactor(secrets=[data.OPERATOR_PASSWORD])
    store = ArtifactStore(OUT / "artifacts", redactor)
    settings = Settings.load(**{**FAST, "discovery_max_steps": 25})
    srv = MockBankServer().start()
    disc_rows, replay_rows = [], []
    try:
        for label, spec_name, faults, operator in DISCOVERIES:
            surface = PlaywrightSurface(srv.base_url, redactor=redactor, action_timeout=settings.action_timeout).start()
            try:
                surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
                reset_app(srv.base_url, "tenant_a", faults)
                agent = DiscoveryAgent(surface, store=store, runs_dir=OUT / "discovery", redactor=redactor,
                                       console=operator(), settings=settings)
                result = agent.discover(DiscoverySpec.load(ROOT / "specs" / f"{spec_name}.yaml"), "tenant_a")
            finally:
                surface.close()
            usage = [e for e in result.events if e["kind"] == "model_usage"]
            row = {"label": label, "spec": spec_name, "faults": faults, "status": result.status,
                   "reason": result.reason, "turns": result.turns, "run": f"discovery/{result.run_id}",
                   "input_tokens": sum(u["input_count"] for u in usage),
                   "output_tokens": sum(u["output_count"] for u in usage),
                   "cache_read": sum(u["cache_read"] for u in usage),
                   "restarted": any(e["kind"] == "discovery_restarted" for e in result.events),
                   "served_models": sorted({u["model"] for u in usage}),
                   "fallback_categories": sorted({e["category"] for e in result.events
                                                  if e["kind"] == "model_fallback"}),
                   "handoffs": sum(e["kind"] == "intervention_requested" for e in result.events)}
            print(f"[discovery] {label}: {result.status} ({result.reason}, {result.turns} turns)")
            if result.capability:
                cap = store.approve(result.capability.name, result.capability.version)
                row["artifact"] = f"artifacts/{cap.name}/v{cap.version}.json"
                row["steps"] = [f"{s.action.value} {s.target.description}" for s in cap.steps]
                for scen, inputs, rfaults, rop in REPLAYS.get(label, []):
                    surface = PlaywrightSurface(srv.base_url, redactor=redactor,
                                                action_timeout=settings.action_timeout).start()
                    try:
                        surface.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
                        reset_app(srv.base_url, "tenant_a", rfaults)
                        engine = ReplayEngine(surface, runs_dir=OUT / "replay", redactor=redactor,
                                              console=rop() if rop else None, settings=settings, overlay_root=ROOT)
                        rep = engine.run(store.load(cap.name, cap.version), inputs, "tenant_a",
                                         run_id=f"{label}-v{cap.version}-{scen}")
                    finally:
                        surface.close()
                    r = rep.result
                    code = getattr(r, "reason", None) or getattr(r, "code", None) or getattr(r, "business_code", None)
                    replay_rows.append({"artifact": row["artifact"], "scenario": scen, "bucket": r.bucket,
                                        "code": getattr(code, "value", code) or "",
                                        "recoveries": [x.kind for x in r.recoveries],
                                        "run": f"replay/{rep.run_id}"})
                    print(f"  [replay] {scen}: {r.bucket} {replay_rows[-1]['code']}")
            disc_rows.append(row)
    finally:
        srv.stop()
    (OUT / "live_summary.json").write_text(json.dumps({"discoveries": disc_rows, "replays": replay_rows}, indent=2))
    return 0 if disc_rows and disc_rows[0]["status"] == "DRAFT_SAVED" else 1


if __name__ == "__main__":
    sys.exit(main())
