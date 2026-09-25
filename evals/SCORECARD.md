# Eval Scorecard

Gate breaches: **0**. Runtime alerts: **2**. Judge trusted for gating: **yes**.

- ALERT unattended_success = 0.395 (min 0.9)
- ALERT policy_violation_runs = 2 (max 0)

## Replay eval (baseline: 38 cases, 38 rows, errors 0)

| Metric | Mean | 95% CI | n |
|---|---|---|---|
| Correct | 1.000 | 0.91 to 1.00 | 38 |
| Bucket | 1.000 | 0.91 to 1.00 | 38 |
| Reason | 1.000 | 0.91 to 1.00 | 38 |
| False SUCCESS | 0.000 | 0.00 to 0.09 | 38 |
| Recoveries | 1.000 | 0.72 to 1.00 | 10 |

Per-bucket precision / recall: SUCCESS 1.0/1.0 (n=10); BUSINESS_OUTCOME 1.0/1.0 (n=5); ESCALATED 1.0/1.0 (n=5); FAILURE 1.0/1.0 (n=18)

## Discovery eval (v2: 11 cases, 22 rows, errors 0)

| Metric | Mean | 95% CI | n |
|---|---|---|---|
| Task success | 1.000 | 0.85 to 1.00 | 22 |
| Draft right | 1.000 | 0.85 to 1.00 | 22 |
| Replayable | 1.000 | 1.00 to 1.00 | 16 |
| Escalation | 1.000 | 0.82 to 1.00 | 18 |
| Commits | 1.000 | 0.68 to 1.00 | 8 |
| No dialog step | 1.000 | 0.51 to 1.00 | 4 |
| In bounds | 1.000 | 0.85 to 1.00 | 22 |
| Tool validity | 0.909 | 0.79 to 1.03 | 22 |
| Efficiency | 0.905 | 0.85 to 0.96 | 16 |
| Faithful | 1.000 | 0.85 to 1.00 | 22 |
| Reviewable | 1.000 | 0.81 to 1.00 | 16 |
| Esc. judged | 1.000 | 0.85 to 1.00 | 22 |
| Goal fidelity | 1.000 | 0.85 to 1.00 | 22 |

Cost: $2.545 total, $0.116 per run under test; judge $1.280.

## Discovery eval: variant history

| Metric | baseline | v1 | v2 | latest vs baseline (paired) |
|---|---|---|---|---|
| Task success | 0.818 | 0.909 | 1.000 | +0.182 (95% CI -0.06 to +0.42) |
| Draft right | 1.000 | 0.909 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |
| Replayable | 1.000 | 0.875 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |
| Escalation | 0.889 | 1.000 | 1.000 | +0.111 (95% CI -0.11 to +0.33) |
| Commits | 1.000 | 1.000 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |
| No dialog step | 1.000 | 1.000 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |
| In bounds | 0.909 | 1.000 | 1.000 | +0.091 (95% CI -0.09 to +0.27) |
| Tool validity | 0.985 | 0.909 | 0.909 | -0.076 (95% CI -0.26 to +0.11) |
| Efficiency | 0.905 | 0.912 | 0.905 | +0.000 (95% CI +0.00 to +0.00) |
| Faithful | 0.909 | 1.000 | 1.000 | +0.091 (95% CI -0.09 to +0.27) |
| Reviewable | 1.000 | 1.000 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |
| Esc. judged | 0.947 | 1.000 | 1.000 | +0.050 (95% CI -0.05 to +0.15) |
| Goal fidelity | 1.000 | 1.000 | 1.000 | +0.000 (95% CI +0.00 to +0.00) |

Cost per run under test: baseline: $0.099/run, v1: $0.125/run, v2: $0.116/run.

## Judge calibration

claude-sonnet-5 on 35 labelled items: agreement **1.0**, repeat consistency 1.0.

| Criterion | Agreement | n | errors |
|---|---|---|---|
| J1 | 1.0 | 10 | 0 |
| J2 | 1.0 | 7 | 0 |
| J3 | 1.0 | 8 | 0 |
| J4 | 1.0 | 10 | 0 |

## Runtime ledger (`runs/metrics.jsonl, evals/discovery/baseline/runs/metrics.jsonl, evals/discovery/v1/runs/metrics.jsonl, evals/discovery/v2/runs/metrics.jsonl, evals/replay/baseline/runs/metrics.jsonl`)

> This view includes **eval traffic**, where failures are injected on purpose (the stress scenarios include allowlist redirects, app errors, and drift). Alerts here show the alerting works; for production, run `cua eval scorecard` without `--include-evals` so only `runs/metrics.jsonl` counts.

- Replay runs: 38; buckets {'SUCCESS': 10, 'BUSINESS_OUTCOME': 5, 'ESCALATED': 5, 'FAILURE': 18}
- Unattended success: 40%; escalation rate 16%
- **Policy violations** (allowlist): 2 runs, by stage {'start_page': 1, 'mid_flow': 1}
- Locator fallback rate: 0.0% (coordinate resolutions 0); ambiguous targets 1
- Failures by reason: {'HANDOFF_FAILED': 3, 'APP_ERROR': 1, 'TIMEOUT': 2, 'AMBIGUOUS_TARGET': 1, 'PERMISSION_DENIED': 1, 'VALIDATION_ERROR': 1, 'UNRECOVERABLE_BLOCKER': 1, 'ACTION_FAILED': 2, 'TARGET_NOT_FOUND': 2, 'DRIFT_DETECTED': 2, 'POLICY_VIOLATION': 2}
- Recoveries: {'KNOWN_DIALOG': 2, 'SLOW_LOAD': 3, 'TRANSIENT_APP_ERROR': 4, 'NATIVE_DIALOG': 2, 'ACTION_RETRY': 3}; approvals: {'APPROVED': 5, 'DENIED': 1, 'TIMEOUT': 1}; acts rejected while human held token: 0
- Duration p50 0.65s, p95 5.54s
- Discovery runs: 70 (46 drafts, 24 stops); turns p50 7; tool calls {'fill': 85, 'click': 181, 'extract': 88, 'done': 47, 'select': 12, 'request_human': 58, 'wait': 4}; tool error rate 2%; policy blocks {'page': 2, 'gateway': 9}; restarts 12
- Discovery cost $7.563 total, $0.164 per draft; cache-read share 84%
- **Model fallback**: 22% of turns served by a fallback model; served {'claude-opus-4-8': 108, 'claude-opus-5': 387}

## Trend vs previous scorecard

- replay.correct: 1.0 -> 1.0 (+0.0)
- discovery.task_success: 1.0 -> 1.0 (+0.0)
- discovery.replayability: 1.0 -> 1.0 (+0.0)
- ledger.unattended_success: 0.395 -> 0.395 (+0.0)
- ledger.locator_fallback_rate: 0.0 -> 0.0 (+0.0)
