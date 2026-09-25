"""cua CLI: discover, approve, deprecate, replay, list, propose-revision, serve-mock."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import ROOT, Settings
from .redactor import Redactor
from .store import ArtifactStore

DEFAULT_URL = os.environ.get("BANK_BASE_URL", "http://127.0.0.1:5055")


def _store(redactor: Redactor | None = None) -> ArtifactStore:
    return ArtifactStore(ROOT / "artifacts", redactor)


def _creds() -> tuple[str, str]:
    from mockbank import data  # dev defaults for the local mock only
    return (os.environ.get("CUA_BANK_USERNAME", data.OPERATOR_USERNAME),
            os.environ.get("CUA_BANK_PASSWORD", data.OPERATOR_PASSWORD))


def _surface(args, redactor: Redactor):
    """A human can only take over a browser they can see: with an operator attached, the browser is headed."""
    from .surface.playwright_surface import PlaywrightSurface
    headless = args.unattended and not args.headed
    if not args.unattended and not args.headed:
        print("[cua] operator attached: opening a visible browser so a human can take over (--unattended to skip)")
    s = PlaywrightSurface(args.base_url, headless=headless, redactor=redactor,
                          action_timeout=Settings.load().action_timeout).start()
    s.authenticate(*_creds())
    return s


def _console(args):
    from .operator import CLIOperatorConsole
    return None if args.unattended else CLIOperatorConsole()


def cmd_list(args) -> None:
    for name, version, status in _store().list_all():
        print(f"{name:24} v{version:<3} {status}")


def cmd_approve(args) -> None:
    cap = _store().approve(args.name, args.version)
    print(f"{cap.name} v{cap.version} APPROVED ({cap.content_hash()})")


def cmd_deprecate(args) -> None:
    cap = _store().deprecate(args.name, args.version)
    print(f"{cap.name} v{cap.version} DEPRECATED")


def cmd_replay(args) -> int:
    from .replay.engine import ReplayEngine
    redactor = Redactor(secrets=[_creds()[1]])
    store = _store(redactor)
    cap = store.load(args.name, args.version) if args.version else store.latest(args.name)
    if cap is None:
        print(f"no APPROVED version of {args.name}", file=sys.stderr)
        return 2
    surface = _surface(args, redactor)
    try:
        engine = ReplayEngine(surface, runs_dir=ROOT / "runs", redactor=redactor, console=_console(args),
                              overlay_root=ROOT)
        report = engine.run(cap, json.loads(args.inputs), args.tenant, unattended=not args.attended)
    finally:
        surface.close()
    print(json.dumps(report.result.model_dump(mode="json"), indent=2))
    print(f"run log: runs/{report.run_id}/ (sensitive outputs are masked on disk)")
    return 0 if report.result.bucket in ("SUCCESS", "BUSINESS_OUTCOME") else 1


def cmd_discover(args) -> int:
    from .discovery.agent import DEFAULT_MODEL, DiscoveryAgent, DiscoverySpec
    redactor = Redactor(secrets=[_creds()[1]])
    surface = _surface(args, redactor)
    try:
        agent = DiscoveryAgent(surface, store=_store(redactor), runs_dir=ROOT / "runs", redactor=redactor,
                               console=_console(args), model=args.model or DEFAULT_MODEL)
        result = agent.discover(DiscoverySpec.load(Path(args.spec)), args.tenant)
    finally:
        surface.close()
    print(f"{result.status}: {result.reason} ({result.turns} turns, log runs/{result.run_id}/)")
    if result.capability:
        c = result.capability
        print(f"saved {c.name} v{c.version} as DRAFT. Review artifacts/{c.name}/v{c.version}.json, then:\n"
              f"  cua approve {c.name} {c.version}")
    return 0 if result.status == "DRAFT_SAVED" else 1


def cmd_propose(args) -> None:
    from .revision import propose_revision
    rev = propose_revision(_store(), args.name, args.version, Path(args.run_dir))
    print(f"{rev.name} v{rev.version} DRAFT proposed from {args.run_dir}\n{rev.provenance.notes}")


def _mock_server():
    from mockbank.app import MockBankServer
    return MockBankServer().start()


def cmd_eval_replay(args) -> int:
    from .evals.runner import run_replay_eval
    from .evals.scorecard import summarize_flow
    srv = _mock_server()
    try:
        out = run_replay_eval(srv.base_url, variant=args.variant, reps=args.reps)
    finally:
        srv.stop()
    s = summarize_flow(out.parent, args.variant)
    print(json.dumps({k: v["mean"] for k, v in s["metrics"].items()}), f"errors={s['errors'] or 0}")
    return 0 if s["metrics"]["correct"]["mean"] == 1.0 and s["metrics"]["false_success"]["mean"] == 0 else 1


def cmd_eval_discovery(args) -> int:
    import anthropic

    from .evals.discovery_cases import CASES
    from .evals.runner import EVALS, run_discovery_eval
    from .evals.scorecard import summarize_flow
    cases = [c for c in CASES if not args.cases or c.id in args.cases.split(",")]
    client = anthropic.Anthropic(timeout=180.0)
    judge = None if args.no_judge else client
    srv = _mock_server()
    try:
        if not args.yes:  # consent gate: a measured pilot before any full paid run
            pilot = EVALS / "discovery_pilot"
            run_discovery_eval(srv.base_url, client=client, judge_client=judge, variant="baseline", reps=1,
                               cases=cases[:2], out=pilot)
            s = summarize_flow(pilot, "baseline")
            per = (s.get("cost_usd") or {})
            each = (per.get("per_run") or 0) + (per.get("judge_total") or 0) / max(1, s["rows"])
            reps = args.reps or 2
            print(f"pilot: {s['rows']} runs, ${per.get('total', 0):.3f} under test + ${per.get('judge_total', 0):.3f} judge.\n"
                  f"full run estimate: {len(cases)} cases x {reps} reps x ${each:.3f} = ${len(cases) * reps * each:.2f}. "
                  f"Re-run with --yes to proceed.")
            return 0
        run_discovery_eval(srv.base_url, client=client, judge_client=judge, variant=args.variant, reps=args.reps,
                           cases=cases)
    finally:
        srv.stop()
    s = summarize_flow(EVALS / "discovery", args.variant)
    print(json.dumps({k: v["mean"] for k, v in s["metrics"].items()}), s.get("cost_usd"), f"errors={s['errors'] or 0}")
    return 0


def cmd_eval_calibrate(args) -> int:
    import anthropic

    from .evals.calibration import run_calibration
    from .evals.judge import Judge
    from .evals.runner import EVALS, load_config
    model = args.judge_model or load_config()["judge_model"]
    summary = run_calibration(Judge(anthropic.Anthropic(timeout=180.0), model),
                              EVALS / "judge_calibration" / args.variant)
    print(json.dumps({"agreement": summary["agreement"], "repeat_consistency": summary["repeat_consistency"],
                      "per_criterion": {k: v["agreement"] for k, v in summary["per_criterion"].items()}}))
    return 0


def cmd_eval_scorecard(args) -> int:
    from .evals.scorecard import build
    ledgers = [Path(x) for x in args.ledger]
    if args.include_evals:
        ledgers += sorted((ROOT / "evals").glob("*/*/runs/metrics.jsonl"))
    card = build(ledgers)
    print((ROOT / "evals" / "SCORECARD.md").read_text())
    return 1 if card["gate_breaches"] else 0


def cmd_eval_report(args) -> int:
    import shutil
    import subprocess
    node = shutil.which("node") or shutil.which("bun")
    if not node:
        print("node or bun is needed to build report.html; results are in evals/<flow>/", file=sys.stderr)
        return 2
    return subprocess.call([node, str(ROOT / "scripts" / "build-report-lite.mjs"), str(ROOT / "evals" / args.flow)])


def cmd_metrics(args) -> int:
    from .evals.scorecard import ledger_summary
    from .metrics import read
    print(json.dumps(ledger_summary(read(Path(args.ledger))), indent=2))
    return 0


def cmd_serve(args) -> None:
    from mockbank.app import create_app
    create_app().run(port=args.port, threaded=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def browser_opts(p):
        p.add_argument("--base-url", default=DEFAULT_URL)
        p.add_argument("--tenant", default=os.environ.get("TENANT_ID", "tenant_a"))
        p.add_argument("--headed", action="store_true", help="show the browser even when unattended")
        p.add_argument("--unattended", action="store_true",
                       help="no operator: blockers fail instead of escalating, irreversible steps are refused")

    sub.add_parser("list", help="list artifacts").set_defaults(fn=cmd_list)
    for name, fn in (("approve", cmd_approve), ("deprecate", cmd_deprecate)):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("version", type=int)
        p.set_defaults(fn=fn)

    p = sub.add_parser("replay", help="replay an artifact deterministically (no LLM)")
    p.add_argument("name")
    p.add_argument("--version", type=int)
    p.add_argument("--inputs", required=True, help='JSON, e.g. \'{"member_id": "M1001"}\'')
    p.add_argument("--attended", action="store_true", help="allow a DRAFT artifact with an operator present")
    browser_opts(p)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("discover", help="LLM-driven discovery; records a DRAFT artifact")
    p.add_argument("spec", help="specs/<capability>.yaml")
    p.add_argument("--model", default=None, help="defaults to the agent's DEFAULT_MODEL")
    browser_opts(p)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("propose-revision", help="new DRAFT from a run's captured human actions")
    p.add_argument("name")
    p.add_argument("version", type=int)
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_propose)

    ev = sub.add_parser("eval", help="evals: replay, discovery, calibrate-judge, scorecard, report")
    esub = ev.add_subparsers(dest="eval_cmd", required=True)
    p = esub.add_parser("replay", help="deterministic replay eval over the scenario catalog (no key)")
    p.add_argument("--variant", default="baseline")
    p.add_argument("--reps", type=int, default=1)
    p.set_defaults(fn=cmd_eval_replay)
    p = esub.add_parser("discovery", help="live discovery eval + LLM judge (needs key; pilot first)")
    p.add_argument("--variant", default="baseline")
    p.add_argument("--reps", type=int, default=None)
    p.add_argument("--cases", default="", help="comma-separated case ids, e.g. D01,D05")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip the pilot/consent gate and run the full eval")
    p.set_defaults(fn=cmd_eval_discovery)
    p = esub.add_parser("calibrate-judge", help="measure judge agreement on a labelled set (needs key)")
    p.add_argument("--judge-model", default=None)
    p.add_argument("--variant", default="baseline", help="baseline, v1, ... (e.g. after a rubric change)")
    p.set_defaults(fn=cmd_eval_calibrate)
    p = esub.add_parser("scorecard", help="aggregate evals + runtime ledger; exits 1 on a gate breach")
    p.add_argument("--ledger", nargs="*", default=[str(ROOT / "runs" / "metrics.jsonl")])
    p.add_argument("--include-evals", action="store_true", help="also aggregate the ledgers written by eval runs")
    p.set_defaults(fn=cmd_eval_scorecard)
    p = esub.add_parser("report", help="build evals/<flow>/report.html")
    p.add_argument("flow", choices=["replay", "discovery", "judge_calibration"])
    p.set_defaults(fn=cmd_eval_report)
    p = sub.add_parser("metrics", help="summarize the runtime metrics ledger")
    p.add_argument("--ledger", default=str(ROOT / "runs" / "metrics.jsonl"))
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("serve-mock", help="run the mock CoreOne Banking app")
    p.add_argument("--port", type=int, default=5055)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
