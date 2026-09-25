"""Scorecard: eval flows + runtime ledger -> one view with confidence intervals, gates, alerts, and trend."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..metrics import RunMetrics, read
from .graders import confusion
from .runner import EVALS, load_config


def wilson(k: float, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0, c - h), 3), round(min(1, c + h), 3))


def _rows(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def variants(flow_dir: Path) -> list[str]:
    vs = [d.name for d in flow_dir.iterdir() if d.is_dir() and (d.name == "baseline" or
                                                               (d.name[0] == "v" and d.name[1:].isdigit()))]
    return sorted(vs, key=lambda v: -1 if v == "baseline" else int(v[1:])) if flow_dir.exists() else []


def summarize_flow(flow_dir: Path, variant: str) -> dict[str, Any]:
    state = json.loads((flow_dir / "_state.json").read_text())
    rows = [r for r in _rows(flow_dir / variant / "results.jsonl") if r.get("status", "ok") == "ok"]
    errors = Counter(e["class"] for e in _rows(flow_dir / variant / "errors.jsonl"))
    out: dict[str, Any] = {"variant": variant, "rows": len(rows), "cases": len({r["prompt_id"] for r in rows}),
                           "errors": dict(errors), "metrics": {}}
    for m in state["metrics"]:
        vals = [r["grade"][m["id"]] for r in rows if isinstance(r.get("grade", {}).get(m["id"]), (int, float))]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        entry = {"mean": round(mean, 3), "n": len(vals), "label": m.get("label", m["id"]),
                 "better": m.get("better", "higher")}
        if m["kind"] == "binary":
            entry["ci95"] = wilson(sum(vals), len(vals))
        elif len(vals) > 1:
            h = 1.96 * statistics.stdev(vals) / math.sqrt(len(vals))
            entry["ci95"] = (round(mean - h, 3), round(mean + h, 3))
        out["metrics"][m["id"]] = entry
    costs = [r.get("cost_usd") for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    jcosts = [r.get("judge_cost_usd") for r in rows if isinstance(r.get("judge_cost_usd"), (int, float))]
    if costs:
        out["cost_usd"] = {"total": round(sum(costs), 4), "per_run": round(sum(costs) / len(costs), 4),
                           "judge_total": round(sum(jcosts), 4)}
    if rows and "expected" in rows[0]:
        out["confusion"] = confusion([(r["expected"]["bucket"], r["actual"]["bucket"]) for r in rows])
    return out


def paired_delta(flow_dir: Path, variant: str, metric: str, ref: str = "baseline") -> dict[str, Any] | None:
    def per_case(v: str) -> dict[str, float]:
        acc: dict[str, list[float]] = defaultdict(list)
        for r in _rows(flow_dir / v / "results.jsonl"):
            x = r.get("grade", {}).get(metric)
            if r.get("status", "ok") == "ok" and isinstance(x, (int, float)):
                acc[r["prompt_id"]].append(x)
        return {k: sum(v) / len(v) for k, v in acc.items()}
    a, b = per_case(variant), per_case(ref)
    common = sorted(set(a) & set(b))
    if len(common) < 2:
        return None
    d = [a[c] - b[c] for c in common]
    mean, h = sum(d) / len(d), 1.96 * statistics.stdev(d) / math.sqrt(len(d))
    return {"mean": round(mean, 3), "ci95": (round(mean - h, 3), round(mean + h, 3)), "n": len(d),
            "significant": not (mean - h <= 0 <= mean + h)}


# ---------------------------------------------------------------- runtime ledger


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)


def ledger_summary(rows: list[RunMetrics]) -> dict[str, Any]:
    rep = [r for r in rows if r.kind == "replay"]
    disc = [r for r in rows if r.kind == "discovery"]

    def replay_block(rs: list[RunMetrics]) -> dict[str, Any]:
        if not rs:
            return {"runs": 0}
        res = sum(r.resolutions for r in rs)
        pv: Counter = Counter()
        for r in rs:
            pv.update(r.policy_violations)
        rec: Counter = Counter()
        for r in rs:
            rec.update(r.recoveries)
        appr: Counter = Counter()
        for r in rs:
            appr.update(r.approvals)
        claims = [s for r in rs for s in r.seconds_to_claim]
        return {
            "runs": len(rs), "buckets": dict(Counter(r.bucket for r in rs)),
            "reasons": dict(Counter(r.reason for r in rs if r.bucket == "FAILURE")),
            "unattended_success": round(sum(1 for r in rs if r.bucket in ("SUCCESS", "BUSINESS_OUTCOME")
                                             and r.handoffs == 0) / len(rs), 3),
            "escalation_rate": round(sum(1 for r in rs if r.handoffs) / len(rs), 3),
            "policy_violations": dict(pv), "policy_violation_runs": sum(1 for r in rs if r.policy_violations),
            "locator_fallback_rate": round(sum(r.fallback_resolutions for r in rs) / res, 3) if res else 0.0,
            "coordinate_resolutions": sum(r.coordinate_resolutions for r in rs),
            "ambiguous": sum(r.ambiguous for r in rs), "recoveries": dict(rec), "approvals": dict(appr),
            "seconds_to_claim_p50": _pct(claims, 0.5),
            "resume_first_pass": sum(r.resume_first_pass for r in rs),
            "acts_rejected": sum(r.acts_rejected_while_human for r in rs),
            "duration_p50_s": _pct([r.duration_s for r in rs], 0.5),
            "duration_p95_s": _pct([r.duration_s for r in rs], 0.95),
        }

    by_cap = {cap: replay_block([r for r in rep if r.capability == cap]) for cap in sorted({r.capability for r in rep})}
    d: dict[str, Any] = {"runs": len(disc)}
    if disc:
        tools: Counter = Counter()
        blocked: Counter = Counter()
        for r in disc:
            tools.update(r.tool_calls)
            blocked.update(r.policy_blocked)
        costs = [r.cost_usd for r in disc if r.cost_usd is not None]
        drafts = sum(1 for r in disc if r.bucket == "DRAFT_SAVED")
        calls = sum(tools.values())
        d.update({"drafts": drafts, "stops": len(disc) - drafts, "turns_p50": _pct([r.turns for r in disc], 0.5),
                  "tool_calls": dict(tools), "tool_error_rate": round(sum(r.tool_errors for r in disc) / calls, 3)
                  if calls else 0.0, "policy_blocked": dict(blocked), "restarts": sum(r.restarts for r in disc),
                  "fallback_turn_share": round(sum(r.fallback_turns for r in disc) / max(1, sum(r.turns for r in disc)), 3),
                  "served_models": dict(sum((Counter(r.llm_models) for r in disc), Counter())),
                  "cost_total_usd": round(sum(costs), 4), "cost_per_draft_usd": round(sum(costs) / drafts, 4)
                  if drafts else None,
                  "cache_read_share": round(sum(r.llm_cache_read for r in disc) /
                                            max(1, sum(r.llm_cache_read + r.llm_input + r.llm_cache_write
                                                       for r in disc)), 3)})
    return {"replay": replay_block(rep), "replay_by_capability": by_cap, "discovery": d}


# ---------------------------------------------------------------- gates, alerts, render


def evaluate(summaries: dict[str, dict], ledger: dict[str, Any], calibration: dict | None,
             cfg: dict[str, Any]) -> tuple[list[str], list[str], bool]:
    breaches, alerts = [], []
    judge_trusted = bool(calibration and (calibration.get("agreement") or 0) >= cfg["judge_min_agreement"])
    for flow, gates in cfg["gates"].items():
        if flow == "judge" and not judge_trusted:
            continue  # judge metrics are advisory until calibration clears the bar
        s = summaries.get("discovery" if flow == "judge" else flow)
        if not s:
            continue
        for key, limit in gates.items():
            metric, bound = key.rsplit("_", 1)  # e.g. "task_success_min" -> ("task_success", "min")
            if metric == "cost_per_run_usd":
                val = (s.get("cost_usd") or {}).get("per_run")
            else:
                val = (s["metrics"].get(metric) or {}).get("mean")
            if val is None:
                continue
            if (bound == "min" and val < limit) or (bound == "max" and val > limit):
                breaches.append(f"{flow}.{metric} = {val} (gate {bound} {limit})")
    rp = ledger["replay"]
    if rp.get("runs"):
        a = cfg["alerts"]
        checks = [("unattended_success", rp["unattended_success"], "min", a["unattended_success_min"]),
                  ("policy_violation_runs", rp["policy_violation_runs"], "max", a["policy_violations_max"]),
                  ("locator_fallback_rate", rp["locator_fallback_rate"], "max", a["locator_fallback_rate_max"]),
                  ("escalation_rate", rp["escalation_rate"], "max", a["escalation_rate_max"]),
                  ("acts_rejected", rp["acts_rejected"], "max", a["acts_rejected_max"]),
                  ("duration_p95_s", rp["duration_p95_s"] or 0, "max", a["p95_duration_s_max"])]
        for name, val, bound, lim in checks:
            if (bound == "min" and val < lim) or (bound == "max" and val > lim):
                alerts.append(f"{name} = {val} ({bound} {lim})")
    return breaches, alerts, judge_trusted


def build(ledger_path: Path | list[Path] = ROOT / "runs" / "metrics.jsonl", out: Path = EVALS) -> dict[str, Any]:
    ledgers = ledger_path if isinstance(ledger_path, list) else [ledger_path]
    cfg = load_config()
    summaries: dict[str, dict] = {}
    deltas: dict[str, dict] = {}
    history: dict[str, dict] = {}
    for flow in ("replay", "discovery"):
        fd = out / flow
        vs = variants(fd) if fd.exists() else []
        if not vs:
            continue
        summaries[flow] = summarize_flow(fd, vs[-1])
        if len(vs) > 1:
            history[flow] = {v: summarize_flow(fd, v) for v in vs}
        if vs[-1] != "baseline":
            deltas[flow] = {m: paired_delta(fd, vs[-1], m) for m in summaries[flow]["metrics"]}
    cal_dir = out / "judge_calibration"
    cal_vs = variants(cal_dir) if cal_dir.exists() else []
    cal_path = cal_dir / cal_vs[-1] / "calibration.json" if cal_vs else None
    calibration = json.loads(cal_path.read_text()) if cal_path and cal_path.exists() else None
    if calibration:
        calibration["variant"] = cal_vs[-1]
    ledger = ledger_summary([row for p in ledgers for row in read(p)])
    breaches, alerts, trusted = evaluate(summaries, ledger, calibration, cfg)
    prev_path = out / "scorecard.json"
    previous = json.loads(prev_path.read_text()) if prev_path.exists() else None
    card = {"flows": summaries, "deltas_vs_baseline": deltas, "history": history, "judge_calibration": calibration,
            "judge_trusted": trusted, "ledger": ledger, "gate_breaches": breaches, "alerts": alerts,
            "ledger_path": ", ".join(str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in ledgers)}
    card["trend"] = _trend(previous, card)
    out.mkdir(parents=True, exist_ok=True)
    prev_path.write_text(json.dumps(card, indent=2, default=str))
    (out / "SCORECARD.md").write_text(render(card))
    return card


def _headlines(card: dict) -> dict[str, float | None]:
    f = card.get("flows", {})
    g = lambda flow, m: ((f.get(flow) or {}).get("metrics", {}).get(m) or {}).get("mean")  # noqa: E731
    lr = (card.get("ledger") or {}).get("replay") or {}
    return {"replay.correct": g("replay", "correct"), "discovery.task_success": g("discovery", "task_success"),
            "discovery.replayability": g("discovery", "replayability"),
            "ledger.unattended_success": lr.get("unattended_success"),
            "ledger.locator_fallback_rate": lr.get("locator_fallback_rate")}


def _trend(previous: dict | None, card: dict) -> dict[str, Any]:
    if not previous:
        return {}
    now, before = _headlines(card), _headlines(previous)
    return {k: {"before": before[k], "now": v, "delta": round(v - before[k], 3)}
            for k, v in now.items() if v is not None and before.get(k) is not None}


def render(card: dict) -> str:
    L = ["# Eval Scorecard", "",
         f"Gate breaches: **{len(card['gate_breaches'])}**. Runtime alerts: **{len(card['alerts'])}**. "
         f"Judge trusted for gating: **{'yes' if card['judge_trusted'] else 'no (advisory)'}**.", ""]
    L += [f"- BREACH {b}" for b in card["gate_breaches"]] + [f"- ALERT {a}" for a in card["alerts"]]
    for flow, s in card["flows"].items():
        L += ["", f"## {flow.title()} eval ({s['variant']}: {s['cases']} cases, {s['rows']} rows, "
                  f"errors {s['errors'] or 0})", "", "| Metric | Mean | 95% CI | n |", "|---|---|---|---|"]
        for mid, m in s["metrics"].items():
            ci = m.get("ci95")
            L.append(f"| {m['label']} | {m['mean']:.3f} | {f'{ci[0]:.2f} to {ci[1]:.2f}' if ci else ''} | {m['n']} |")
        if s.get("cost_usd"):
            c = s["cost_usd"]
            L.append(f"\nCost: ${c['total']:.3f} total, ${c['per_run']:.3f} per run under test; judge ${c['judge_total']:.3f}.")
        if s.get("confusion"):
            per = s["confusion"]["per_bucket"]
            L += ["", "Per-bucket precision / recall: " + "; ".join(
                f"{b} {v['precision']}/{v['recall']} (n={v['n']})" for b, v in per.items() if v["n"])]
    for flow, hist in (card.get("history") or {}).items():
        vs = list(hist)
        L += ["", f"## {flow.title()} eval: variant history", "",
              "| Metric | " + " | ".join(vs) + " | latest vs baseline (paired) |",
              "|---|" + "---|" * len(vs) + "---|"]
        mids = list(next(iter(hist.values()))["metrics"])
        for v in vs[1:]:
            mids += [m for m in hist[v]["metrics"] if m not in mids]
        for mid in mids:
            cells = [f"{hist[v]['metrics'][mid]['mean']:.3f}" if mid in hist[v]["metrics"] else "" for v in vs]
            d = (card["deltas_vs_baseline"].get(flow) or {}).get(mid)
            dtxt = f"{d['mean']:+.3f} (95% CI {d['ci95'][0]:+.2f} to {d['ci95'][1]:+.2f}{', significant' if d['significant'] else ''})" if d else ""
            label = next((hist[v]["metrics"][mid]["label"] for v in vs if mid in hist[v]["metrics"]), mid)
            L.append(f"| {label} | " + " | ".join(cells) + f" | {dtxt} |")
        costs = [f"{v}: ${(hist[v].get('cost_usd') or {}).get('per_run', 0):.3f}/run" for v in vs]
        L.append("\nCost per run under test: " + ", ".join(costs) + ".")
    cal = card.get("judge_calibration")
    if cal:
        L += ["", "## Judge calibration", "",
              f"{cal['judge_model']} on {cal['items']} labelled items: agreement **{cal['agreement']}**, "
              f"repeat consistency {cal['repeat_consistency']}.", "", "| Criterion | Agreement | n | errors |",
              "|---|---|---|---|"]
        L += [f"| {c} | {p['agreement']} | {p['n']} | {p['errors']} |" for c, p in sorted(cal["per_criterion"].items())]
    lr, ld = card["ledger"]["replay"], card["ledger"]["discovery"]
    L += ["", f"## Runtime ledger (`{card['ledger_path']}`)", ""]
    if "evals/" in card["ledger_path"]:
        L += ["> This view includes **eval traffic**, where failures are injected on purpose (the stress scenarios "
              "include allowlist redirects, app errors, and drift). Alerts here show the alerting works; for "
              "production, run `cua eval scorecard` without `--include-evals` so only `runs/metrics.jsonl` counts.", ""]
    if lr.get("runs"):
        L += [f"- Replay runs: {lr['runs']}; buckets {lr['buckets']}",
              f"- Unattended success: {lr['unattended_success']:.0%}; escalation rate {lr['escalation_rate']:.0%}",
              f"- **Policy violations** (allowlist): {lr['policy_violation_runs']} runs, by stage {lr['policy_violations'] or {}}",
              f"- Locator fallback rate: {lr['locator_fallback_rate']:.1%} (coordinate resolutions {lr['coordinate_resolutions']}); "
              f"ambiguous targets {lr['ambiguous']}",
              f"- Failures by reason: {lr['reasons']}",
              f"- Recoveries: {lr['recoveries']}; approvals: {lr['approvals']}; acts rejected while human held token: "
              f"{lr['acts_rejected']}",
              f"- Duration p50 {lr['duration_p50_s']}s, p95 {lr['duration_p95_s']}s"]
    if ld.get("runs"):
        L += [f"- Discovery runs: {ld['runs']} ({ld['drafts']} drafts, {ld['stops']} stops); turns p50 {ld['turns_p50']}; "
              f"tool calls {ld['tool_calls']}; tool error rate {ld['tool_error_rate']:.0%}; "
              f"policy blocks {ld['policy_blocked'] or {}}; restarts {ld['restarts']}",
              f"- Discovery cost ${ld['cost_total_usd']:.3f} total, ${ld['cost_per_draft_usd'] or 0:.3f} per draft; "
              f"cache-read share {ld['cache_read_share']:.0%}",
              f"- **Model fallback**: {ld['fallback_turn_share']:.0%} of turns served by a fallback model; "
              f"served {ld['served_models']}"]
    if card.get("trend"):
        L += ["", "## Trend vs previous scorecard", ""]
        L += [f"- {k}: {v['before']} -> {v['now']} ({v['delta']:+})" for k, v in card["trend"].items()]
    return "\n".join(L) + "\n"
