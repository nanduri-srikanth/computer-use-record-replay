# Legacy back-office automation: LLM discovery, deterministic replay

[![test](https://github.com/nanduri-srikanth/interface-ai-takehome/actions/workflows/test.yml/badge.svg)](https://github.com/nanduri-srikanth/interface-ai-takehome/actions/workflows/test.yml)

Automates legacy bank back-office apps that have no API. Claude drives the real UI once to discover a procedure; the successful run is recorded as a typed, versioned capability artifact; after human approval it replays deterministically with no LLM in the loop. Each artifact declares its typed inputs, outputs, and the business outcomes it can return. Every replay ends in exactly one of SUCCESS, BUSINESS_OUTCOME, ESCALATED, or FAILURE, and a human can take over the live browser session when the system is stuck.

- Video walkthrough (4 min, narrated): [docs/walkthrough/walkthrough.mp4](docs/walkthrough/walkthrough.mp4), script in [narration.md](docs/walkthrough/narration.md); interactive replay simulator: [docs/walkthrough/playground.html](docs/walkthrough/playground.html) (open in a browser)
- Write-up: [REPORT.md](REPORT.md)
- Evidence (real discovery runs, replays, stress matrix): [evidence/README.md](evidence/README.md)
- Design diagrams: [docs/WORKFLOW.md](docs/WORKFLOW.md) (browse `docs/diagrams/index.html`)
- Build plan and test map: [docs/BUILD_MAP.md](docs/BUILD_MAP.md); code review and resolutions: [docs/REVIEW.md](docs/REVIEW.md)

## Setup

Requires macOS or Linux, [uv](https://docs.astral.sh/uv/), and Node (only for re-rendering diagrams).

```bash
make setup                      # Python 3.12 venv, deps, Chromium, mermaid-cli
```

**API key (discovery only).** Replay, tests, and the stress harness need no key. For discovery, store an Anthropic key in the macOS Keychain; it is read per command and never written to disk or logs:

```bash
scripts/store_api_key.sh        # prompts with hidden input
```

Other config lives in `config/`: `policy.yaml` (allowlist, risk rules), `tenants.yaml`, `detectors/coreone.yaml` (business outcomes, known dialogs), `settings.yaml` (budgets and timeouts). The mock app's operator credentials default to the values in `mockbank/data.py`; override with `CUA_BANK_USERNAME` / `CUA_BANK_PASSWORD`.

## Run without live services

```bash
make test                       # 225 tests: acceptance, 41 stress scenarios, units, discovery (scripted model), metrics, evals, invariants, independent review
make evidence                   # regenerate evidence/stress: matrix, flakiness study, determinism check
```

## Demo path: goal → discovery → artifact → replay

All commands from the repo root. First define the shorthand used below:

```bash
alias cua='PYTHONPATH=src:. .venv/bin/python -m cua.cli'
```

```bash
# terminal 1: the target app
make mock                                                   # mock CoreOne Banking on http://127.0.0.1:5055

# terminal 2: discover (real LLM). A browser opens; you are the operator in this terminal.
scripts/with_api_key.sh env PYTHONPATH=src:. .venv/bin/python -m cua.cli discover specs/get_savings_balance.yaml
#   -> "saved get_savings_balance v2 as DRAFT"  (v1 is the hand-written golden reference)

# review artifacts/get_savings_balance/v2.json, then approve it (v1 becomes DEPRECATED)
cua approve get_savings_balance 2
cua describe get_savings_balance     # the agent-facing contract: inputs, outputs, declared outcomes (no steps)

# replay with typed inputs, no LLM
cua replay get_savings_balance --inputs '{"member_id": "M1001"}' --unattended   # SUCCESS {balance, currency}
cua replay get_savings_balance --inputs '{"member_id": "M9999"}' --unattended   # BUSINESS_OUTCOME MEMBER_NOT_FOUND
cua replay get_savings_balance --inputs '{"member_id": "abc"}'   --unattended   # FAILURE VALIDATION_ERROR (UI never touched)
cua replay get_savings_balance --inputs '{"member_id": "M1002"}' --unattended   # FAILURE AMBIGUOUS_TARGET

# the app serves a well-formed page for a different member: identity checkpoint refuses it
curl -s -XPOST localhost:5055/__admin/reset -H 'content-type: application/json' -d '{"faults": {"wrong_member": "M1003"}}'
cua replay get_savings_balance --inputs '{"member_id": "M1001"}' --unattended   # FAILURE IDENTITY_MISMATCH
curl -s -XPOST localhost:5055/__admin/reset -H 'content-type: application/json' -d '{}'
```

The goal, typed inputs, and typed outputs for discovery come from `specs/*.yaml`. `specs/open_sub_account.yaml` exercises the irreversible path: the terminal asks you to approve the commit, with a redacted summary.

**Human takeover.** Replay without `--unattended` attaches the terminal operator and opens a visible browser. To see a handoff, inject a fault and replay:

```bash
curl -s -XPOST localhost:5055/__admin/reset -H 'content-type: application/json' -d '{"faults": {"unknown_dialog": true}}'
cua replay get_savings_balance --inputs '{"member_id": "M1001"}'
#   terminal: type c to take control, click "Acknowledge" in the browser, type r to resume -> ESCALATED with outputs
```

Faults available through `/__admin/reset` are listed in `mockbank/app.py` (`Faults`): interstitials, unknown dialogs, slow loads, transient and persistent 500s, session expiry, native alert/confirm, redirects off the allowlist, invisible click-eating overlays, input truncation, late-rendered tables, column reorder, label rename, frame rename, flaky backends, slow commits, and a summary page for the wrong member.

**Evidence from real runs** is regenerated with `make evidence-live` (about 5 discovery runs plus replays; cost is cents thanks to prompt caching).

## Evals

Continuous measurement lives in [docs/EVALS.md](docs/EVALS.md); the combined view is [evals/SCORECARD.md](evals/SCORECARD.md).

```bash
make eval                 # replay eval over the scenario catalog + report.html + scorecard (no key)
make eval-live            # judge calibration, then a discovery pilot that prints a measured cost estimate
make eval-live YES=1      # full discovery eval: 11 cases x 2 reps, Sonnet 5 judge
cua metrics               # runtime ledger summary (runs/metrics.jsonl): outcomes, policy violations, drift, cost
```

## CLI reference

| Command | What it does |
|---|---|
| `discover SPEC [--model M] [--unattended]` | LLM-driven discovery; saves a DRAFT artifact |
| `approve NAME VERSION` / `deprecate NAME VERSION` | lifecycle transitions; approving vN+1 deprecates vN |
| `replay NAME --inputs JSON [--version N] [--tenant T] [--attended] [--unattended]` | deterministic replay; `--attended` allows a DRAFT with an operator present |
| `propose-revision NAME VERSION runs/<run_id>` | new DRAFT from a run's captured human actions |
| `eval replay\|discovery\|calibrate-judge\|scorecard\|report` | evals (see [docs/EVALS.md](docs/EVALS.md)) |
| `metrics [--ledger]` | runtime metrics ledger summary |
| `list` | artifacts and status |
| `describe NAME [--version N]` | agent-facing contract: typed inputs/outputs with descriptions, declared outcomes |
| `serve-mock [--port]` | run the mock app |

## Layout

```
mockbank/                 hostile legacy mock app: framesets, tables, no ids, 20+ injectable faults, 3 tenants
src/cua/contracts.py      artifact schema and result contract (Pydantic)
src/cua/surface/          Surface protocol + PlaywrightSurface (the only Playwright import)
src/cua/locators.py       locator ladder (0 matches: next candidate; >1: AMBIGUOUS_TARGET)
src/cua/replay/           ReplayEngine + detectors (no LLM; enforced by a test)
src/cua/discovery/        DiscoveryAgent (Claude tool loop, action gateway) + Recorder
src/cua/session.py        SessionController: control token, handoff, approvals
src/cua/policy.py         allowlist, fail-closed risk tiers, signed single-use approval tokens
src/cua/redactor.py       pattern + label redaction on every persistence path and model input
config/  artifacts/  overlays/  specs/
tests/                    acceptance, stress scenarios (tests/scenarios.py), discovery, units, invariants
scripts/                  stress.py, live_evidence.py, evidence_index.py, key helpers, diagram renderer
evidence/                 generated evidence (see evidence/README.md)
```

## Notes

- macOS may mark the venv's `.pth` files hidden, which Python 3.12 then ignores. The Makefile sets `PYTHONPATH=src:.` so everything works regardless.
- `runs/` (local run logs) is git-ignored; curated runs live in `evidence/`.
