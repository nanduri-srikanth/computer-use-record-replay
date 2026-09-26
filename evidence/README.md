# Evidence

Everything here was produced by running the system, not written by hand:

- `discovery/`: real LLM-driven discovery runs against the live mock app (`scripts/live_evidence.py`). Requested model `claude-opus-5` with server-side refusal fallback enabled; the table shows which model actually served each run.
- `artifacts/`: the capability artifacts those runs recorded (typed, versioned, reviewable).
- `replay/`: deterministic replays of the discovered artifacts, with no LLM involved, including error and exceptional states.
- `stress/`: the stress matrix (41/41 scenarios matched), a flakiness study, and a determinism check (`scripts/stress.py`). See [stress/SUMMARY.md](stress/SUMMARY.md).

**What is mocked.** The operator is scripted in these runs so they can run unattended: it approves or declines commits, acknowledges dialogs, and signs in on the live page. Its actions still go through the real control-token handoff and are captured like a human's. Interactively, `cua replay` / `cua discover` use the terminal operator console with a visible browser.

## How to read a run

- `events.jsonl`: structured log. Discovery lines of kind `tool` carry the model's action *and its reason*; replay lines show `resolved` (which locator candidate matched), `acted`, `recovered`, `failure`, and handoff events (`intervention_requested`, `human_event`, `handoff_ended`). Token usage is in `model_usage`.
- `result.json`: the result contract (one of SUCCESS, BUSINESS_OUTCOME, ESCALATED, FAILURE) with recoveries and handoffs. Sensitive outputs are masked on disk.
- `evidence-*.png` / `.txt`: masked screenshot and redacted page text, captured on failure, intervention, approval, and discovery stop.

## Discovery runs (real model)

| Run | Condition during discovery | Outcome | Turns | Handoff | Served by | Tokens (cached in / out) | Artifact |
|---|---|---|---|---|---|---|---|
| [savings-clean](discovery/disc-2026-09-25T232334-440dcf/) | clean | DRAFT_SAVED: goal met | 6 | 0 | claude-opus-5 | 32,197 / 595 | [artifacts/get_savings_balance/v1.json](artifacts/get_savings_balance/v1.json) |
| [subaccount-clean](discovery/disc-2026-09-25T232357-ba8430/) | clean | DRAFT_SAVED: goal met | 9 | 0 | claude-opus-5 | 69,368 / 804 | [artifacts/open_sub_account/v1.json](artifacts/open_sub_account/v1.json) |
| [savings-interstitial](discovery/disc-2026-09-25T232431-588a2d/) | interstitial=True | DRAFT_SAVED: goal met | 7 | 0 | claude-opus-5 | 42,988 / 649 | [artifacts/get_savings_balance/v2.json](artifacts/get_savings_balance/v2.json) |
| [savings-unknown-dialog](discovery/disc-2026-09-25T232452-a54192/) | unknown_dialog=True | DRAFT_SAVED: goal met | 7 | 1 | claude-opus-5 | 48,758 / 714 | [artifacts/get_savings_balance/v3.json](artifacts/get_savings_balance/v3.json) |
| [savings-session-expired](discovery/disc-2026-09-25T232517-cfb9e6/) | session_expire_at=4 | DRAFT_SAVED: goal met | 9 | 1 (restarted) | claude-opus-5 | 71,378 / 766 | [artifacts/get_savings_balance/v4.json](artifacts/get_savings_balance/v4.json) |

Notable behaviour (derived from each run's events):
- **savings-interstitial**: dismissed 1 dialog(s) itself; kept out of the artifact because replay handles dialogs.
- **savings-unknown-dialog**: asked a human: "A security authorisation prompt must be confirmed by an operator before the member's accounts can be viewed."; the human closed the dialog and discovery continued (no restart: nothing was missing).
- **savings-session-expired**: asked a human: "Session expired and a sign-in is required; no credentials available."; the human changed the page, so discovery restarted from the entry point and the artifact contains only recorder-verified steps.

## Replays of the discovered artifacts (no LLM)

| Artifact | Scenario | Result | Recoveries | Run |
|---|---|---|---|---|
| artifacts/get_savings_balance/v1.json | success | SUCCESS  | none | [savings-clean-v1-success](replay/savings-clean-v1-success/) |
| artifacts/get_savings_balance/v1.json | not-found | BUSINESS_OUTCOME MEMBER_NOT_FOUND | none | [savings-clean-v1-not-found](replay/savings-clean-v1-not-found/) |
| artifacts/get_savings_balance/v1.json | bad-input | FAILURE VALIDATION_ERROR | none | [savings-clean-v1-bad-input](replay/savings-clean-v1-bad-input/) |
| artifacts/get_savings_balance/v1.json | ambiguous | FAILURE AMBIGUOUS_TARGET | none | [savings-clean-v1-ambiguous](replay/savings-clean-v1-ambiguous/) |
| artifacts/get_savings_balance/v1.json | app-error | FAILURE APP_ERROR | TRANSIENT_APP_ERROR, TRANSIENT_APP_ERROR, TRANSIENT_APP_ERROR | [savings-clean-v1-app-error](replay/savings-clean-v1-app-error/) |
| artifacts/get_savings_balance/v1.json | wrong-member | FAILURE IDENTITY_MISMATCH | none | [savings-clean-v1-wrong-member](replay/savings-clean-v1-wrong-member/) |
| artifacts/get_savings_balance/v1.json | unknown-dialog-handoff | ESCALATED completed after human intervention: U... | none | [savings-clean-v1-unknown-dialog-handoff](replay/savings-clean-v1-unknown-dialog-handoff/) |
| artifacts/open_sub_account/v1.json | approved | SUCCESS  | none | [subaccount-clean-v1-approved](replay/subaccount-clean-v1-approved/) |
| artifacts/open_sub_account/v1.json | declined | BUSINESS_OUTCOME DECLINED_BY_OPERATOR | none | [subaccount-clean-v1-declined](replay/subaccount-clean-v1-declined/) |
| artifacts/open_sub_account/v1.json | below-minimum | BUSINESS_OUTCOME VALIDATION_REJECTED | none | [subaccount-clean-v1-below-minimum](replay/subaccount-clean-v1-below-minimum/) |
| artifacts/open_sub_account/v1.json | wrong-member-before-commit | FAILURE IDENTITY_MISMATCH | none | [subaccount-clean-v1-wrong-member-before-commit](replay/subaccount-clean-v1-wrong-member-before-commit/) |
| artifacts/get_savings_balance/v2.json | interstitial-recovered | SUCCESS  | KNOWN_DIALOG | [savings-interstitial-v2-interstitial-recovered](replay/savings-interstitial-v2-interstitial-recovered/) |
| artifacts/get_savings_balance/v3.json | clean-after-dialog-discovery | SUCCESS  | none | [savings-unknown-dialog-v3-clean-after-dialog-discovery](replay/savings-unknown-dialog-v3-clean-after-dialog-discovery/) |
| artifacts/get_savings_balance/v4.json | clean-after-handoff-discovery | SUCCESS  | none | [savings-session-expired-v4-clean-after-handoff-discovery](replay/savings-session-expired-v4-clean-after-handoff-discovery/) |
| artifacts/get_savings_balance/v4.json | session-expired-handoff | ESCALATED completed after human intervention: S... | none | [savings-session-expired-v4-session-expired-handoff](replay/savings-session-expired-v4-session-expired-handoff/) |

## Stress headline

- Matrix: **41/41** scenarios produced exactly the expected result, across baseline, business outcomes, recoverable conditions, escalation, hard failures, drift, and safety.
- Flakiness: with 30% of backend requests failing at random, 100% of 20 runs still succeeded via bounded retries.
- Determinism: 10 baseline replays produced 1 distinct trace(s).
- Data handling: PII and secret scan over all evidence: clean.

Regenerate: `make evidence` (stress, no key needed) and `make evidence-live` (needs an Anthropic key).
