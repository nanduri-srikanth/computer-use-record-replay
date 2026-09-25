"""Writes evidence/README.md from evidence/live_summary.json and evidence/stress/summary.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "evidence"


def main() -> None:
    live = json.loads((EV / "live_summary.json").read_text())
    stress = json.loads((EV / "stress" / "summary.json").read_text())
    m = stress["matrix"]
    lines = [
        "# Evidence", "",
        "Everything here was produced by running the system, not written by hand:",
        "",
        "- `discovery/`: real LLM-driven discovery runs against the live mock app (`scripts/live_evidence.py`). "
        "Requested model `claude-opus-5` with server-side refusal fallback enabled; the table shows which model "
        "actually served each run.",
        "- `artifacts/`: the capability artifacts those runs recorded (typed, versioned, reviewable).",
        "- `replay/`: deterministic replays of the discovered artifacts, with no LLM involved, including error and "
        "exceptional states.",
        f"- `stress/`: the stress matrix ({sum(r['pass'] for r in m)}/{len(m)} scenarios matched), a flakiness "
        "study, and a determinism check (`scripts/stress.py`). See [stress/SUMMARY.md](stress/SUMMARY.md).",
        "",
        "**What is mocked.** The operator is scripted in these runs so they can run unattended: it approves or "
        "declines commits, acknowledges dialogs, and signs in on the live page. Its actions still go through the "
        "real control-token handoff and are captured like a human's. Interactively, `cua replay` / `cua discover` "
        "use the terminal operator console with a visible browser.",
        "",
        "## How to read a run", "",
        "- `events.jsonl`: structured log. Discovery lines of kind `tool` carry the model's action *and its reason*; "
        "replay lines show `resolved` (which locator candidate matched), `acted`, `recovered`, `failure`, and "
        "handoff events (`intervention_requested`, `human_event`, `handoff_ended`). Token usage is in `model_usage`.",
        "- `result.json`: the result contract (one of SUCCESS, BUSINESS_OUTCOME, ESCALATED, FAILURE) with "
        "recoveries and handoffs. Sensitive outputs are masked on disk.",
        "- `evidence-*.png` / `.txt`: masked screenshot and redacted page text, captured on failure, intervention, "
        "approval, and discovery stop.",
        "",
        "## Discovery runs (real model)", "",
        "| Run | Condition during discovery | Outcome | Turns | Handoff | Served by | Tokens (cached in / out) | Artifact |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in live["discoveries"]:
        cond = ", ".join(f"{k}={v}" for k, v in d["faults"].items()) or "clean"
        hand = f"{d['handoffs']} (restarted)" if d["restarted"] else str(d["handoffs"])
        art = f"[{d['artifact']}]({d['artifact']})" if d.get("artifact") else "none"
        served = ", ".join(d.get("served_models", [])) or "?"
        if d.get("fallback_categories"):
            served += f" (fallback: {', '.join(d['fallback_categories'])})"
        lines.append(f"| [{d['label']}]({d['run']}/) | {cond} | {d['status']}: {d['reason']} | {d['turns']} | {hand} | "
                     f"{served} | {d['cache_read']:,} / {d['output_tokens']:,} | {art} |")
    lines += ["", "Notable behaviour (derived from each run's events):"]
    for d in live["discoveries"]:
        ev = [json.loads(x) for x in (EV / d["run"] / "events.jsonl").read_text().splitlines()]
        notes = []
        n = sum(e["kind"] == "dialog_dismissed_not_recorded" for e in ev)
        if n:
            notes.append(f"dismissed {n} dialog(s) itself; kept out of the artifact because replay handles dialogs")
        asks = [e["input"]["reason"] for e in ev if e["kind"] == "tool" and e["tool"] == "request_human"]
        if asks:
            notes.append("asked a human: \"" + asks[0][:110] + "\"")
        if any(e["kind"] == "human_closed_dialog" for e in ev):
            notes.append("the human closed the dialog and discovery continued (no restart: nothing was missing)")
        if any(e["kind"] == "discovery_restarted" for e in ev):
            notes.append("the human changed the page, so discovery restarted from the entry point and the "
                         "artifact contains only recorder-verified steps")
        waits = sum(e["kind"] == "tool" and e["tool"] == "wait" for e in ev)
        if waits:
            notes.append(f"waited {waits} time(s) for the page to load")
        if notes:
            lines.append(f"- **{d['label']}**: " + "; ".join(notes) + ".")
    lines += [
        "", "## Replays of the discovered artifacts (no LLM)", "",
        "| Artifact | Scenario | Result | Recoveries | Run |", "|---|---|---|---|---|",
    ]
    for r in live["replays"]:
        rec = ", ".join(r["recoveries"]) or "none"
        code = r["code"] if len(r["code"]) < 40 else r["code"][:37] + "..."
        lines.append(f"| {r['artifact']} | {r['scenario']} | {r['bucket']} {code} | {rec} | [{r['run'].split('/')[-1]}]({r['run']}/) |")
    st, det = stress["stability"], stress["determinism"]
    lines += [
        "", "## Stress headline", "",
        f"- Matrix: **{sum(r['pass'] for r in m)}/{len(m)}** scenarios produced exactly the expected result, "
        "across baseline, business outcomes, recoverable conditions, escalation, hard failures, drift, and safety.",
        f"- Flakiness: with {int(st['flaky_rate'] * 100)}% of backend requests failing at random, "
        f"{st['success_rate']:.0%} of {st['repeats']} runs still succeeded via bounded retries.",
        f"- Determinism: {det['repeats']} baseline replays produced {det['distinct_traces']} distinct trace(s).",
        f"- Data handling: PII and secret scan over all evidence: {'clean' if not stress['pii_hits'] else 'HITS'}.",
        "", "Regenerate: `make evidence` (stress, no key needed) and `make evidence-live` (needs an Anthropic key).",
    ]
    (EV / "README.md").write_text("\n".join(lines) + "\n")
    print("wrote evidence/README.md")


if __name__ == "__main__":
    main()
