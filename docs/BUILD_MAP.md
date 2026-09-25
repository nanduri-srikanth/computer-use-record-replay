# Build Map

This document turns the diagrams in [WORKFLOW.md](WORKFLOW.md) (D1 to D10) into four lists: setup items, sample test data, the tests that must pass, and the critical backbone. Names match WORKFLOW.md exactly. "Assumes OQ#n" marks a test whose expected result depends on an Open question in WORKFLOW.md. When that question is answered, update the test.

**Guiding decision:** hand-author "golden" APPROVED artifacts first, so that replay, classification, handoff, and safety are testable with no LLM and no API key. Discovery is then judged by whether it produces an artifact that replays the same way as the golden one.

## 1. Primary setup items

| # | Item | What it contains | Diagrams |
|---|---|---|---|
| S1 | Python toolchain | Python 3.12, `uv`, Pydantic v2, Playwright (sync API) + Chromium, pytest, `anthropic` SDK. mermaid-cli is already installed. | all |
| S2 | Secrets and env | `ANTHROPIC_API_KEY` (used by discovery only), mock app operator credentials in `.env` (never written to artifacts), `BANK_BASE_URL`, `TENANT_ID` | D2, D10 |
| S3 | MockBankApp | Small Flask/FastAPI server with Jinja templates. Legacy style: framesets (nav + content frame), table layouts, no ids or test ids. Pages: login, member search, member summary, savings detail, open sub-account form, review, confirm, confirmation. Tenant id and app version shown in the footer. | D3, D8, D10 |
| S4 | Fault injection | Named fault profiles chosen per run (query param or admin endpoint): see fault profiles F1 to F10 in section 2 | D3, D4 |
| S5 | Pydantic contracts | `Capability`, `Step`, `TargetDescriptor` + ranked `LocatorCandidate`s, `Checkpoint`, `OutputSpec`, `Provenance`, `RiskTier`, `ArtifactStatus`, `TenantOverlay` (label maps and locator overrides only, `extra="forbid"`), `RunResult` as a union of 4 buckets, `Failure(step, expected, observed, evidence_ref)` | D1, D3, D8, D9 |
| S6 | Policy config | `policy.yaml`: allowed domains (localhost:port), routes, action types. Risk tier per step type. Approval token settings. | D2, D3, D7 |
| S7 | Redaction rules | Regex and field rules for member id (keep last 4), SSN, DOB, address, credentials, and typed values in human events. Applied to logs, artifacts, evidence, and intervention payloads. | D6, D7, D10 |
| S8 | Tenant registry | `tenants.yaml`: tenant id, expected app version, overlay path | D8 |
| S9 | Storage layout | `artifacts/<capability>/v<N>.json` (immutable), `overlays/<tenant>.json`, `runs/<run_id>/` (events.jsonl, masked screenshots, result.json) | D9, D10 |
| S10 | Operator channel | `OperatorConsole` mock with two modes: interactive CLI prompt (demo) and a scripted operator (tests). A headed browser is used for real human takeover. | D5, D6, D7 |
| S11 | CLI | `discover`, `approve`, `deprecate`, `replay`, `list`, `propose-revision` | D1, D9, D10 |
| S12 | Timeouts and budgets config | max discovery steps, discovery wall clock, slow-load wait budget, retry budget, claim timeout, hold timeout, checkpoint retry limit (assumes OQ#5) | D2, D3, D5 |

---

## 2. Sample test data

**Members (seeded into MockBankApp)**

| Id | Scenario | Data |
|---|---|---|
| M1001 | Happy path | Savings 2,450.17 USD. Carries SSN 123-45-6789 and a DOB on screen as redaction canaries. |
| M1002 | Ambiguous target | Two rows both labelled "Savings" |
| M1003 | Edge output | Savings 0.00 USD (a falsy value must still be SUCCESS) |
| M1004 | Formatting | Savings 1,234,567.89 USD (parsing commas) |
| M1005 | Permission denied | Operator role not entitled, so the app shows "Not authorized" |
| M1006 | App validation | Frozen member, so opening a sub-account is rejected by the app |
| M9999 | Not found | Absent, so the app shows "No member found" |

**open_sub_account inputs**
- Valid: (M1001, `MONEY_MARKET`, 100.00). The confirmation number is deterministic (`CNF-` + 8 digits, seeded).
- App-rejected: deposit 5.00, below the minimum, gives `VALIDATION_REJECTED` (assumes OQ#3).
- Preflight-rejected: member_id `"abc"`, deposit -10, account_type `"CRYPTO"`, all give `VALIDATION_ERROR`.

**Tenants and overlays**
- `tenant_a`: v4.2.1, base labels. No overlay needed.
- `tenant_b`: v4.2.1, labels "Share Savings" / "Member #", plus one locator override. Needs its overlay.
- `tenant_c`: v4.3.0, so the fingerprint mismatches and gives `DRIFT_DETECTED`.
- `overlay_invalid.json`: tries to add a step, so it must be rejected.

**Fault profiles**
- F1 clean
- F2 known interstitial ("Scheduled maintenance notice")
- F3 unknown dialog
- F4 slow load 2s (within budget)
- F5 slow load past the budget
- F6 transient 500 once
- F7 persistent 500
- F8 session expires at step N
- F9 duplicate rows (used with M1002)
- F10 version bump

**Golden artifacts (committed fixtures)**
- `get_savings_balance` v1 APPROVED
- `open_sub_account` v1 APPROVED
- a DRAFT copy (for the "unattended DRAFT refused" test)
- a DEPRECATED copy
- a tampered copy with a step outside the allowlist

**Scripted operator scenarios**
- approve
- deny
- claim, fix, resume
- claim, then cancel
- never claim (timeout)
- resume without fixing (checkpoint fails), then fix and resume again

---

## 3. Tests that must pass

**3a. Acceptance matrix (integration: Playwright against MockBankApp, golden artifacts, no LLM)**

| # | Input + fault | Expected | Also assert |
|---|---|---|---|
| A1 | M1001, F1 | SUCCESS {2450.17, USD} | trace matches golden |
| A2 | M1003, F1 | SUCCESS {0.00, USD} | |
| A3 | M9999 | BUSINESS_OUTCOME MEMBER_NOT_FOUND | |
| A4 | M1001, F2 | SUCCESS | recovery logged, no failure |
| A5 | M1001, F4 | SUCCESS | wait logged |
| A6 | M1001, F5 | FAILURE TIMEOUT | evidence_ref exists |
| A7 | M1001, F6 | SUCCESS | retry logged |
| A8 | M1001, F7 | FAILURE APP_ERROR | |
| A9 | M1002, F9 | FAILURE AMBIGUOUS_TARGET | no later candidate tried, no click |
| A10 | tenant_c | FAILURE DRIFT_DETECTED | zero UI actions after the fingerprint read |
| A11 | bad inputs | FAILURE VALIDATION_ERROR | zero UI actions |
| A12 | M1005 | FAILURE PERMISSION_DENIED (assumes OQ#2) | |
| A13 | F3 + claim, fix, resume | ESCALATED with outputs (assumes OQ#1) | token log shows the full cycle |
| A14 | F3 + never claim | FAILURE (handoff FAILED) | |
| A15 | F8 + human re-login | ESCALATED | no credentials in any log |
| A16 | open_sub_account + approve | SUCCESS confirmation_number | app DB has +1 account |
| A17 | open_sub_account + deny | BUSINESS_OUTCOME DECLINED_BY_OPERATOR | app DB has +0 accounts |
| A18 | deposit 5.00 | BUSINESS_OUTCOME VALIDATION_REJECTED | |
| A19 | tenant_b + overlay | SUCCESS | |
| A20 | overlay_invalid | FAILURE, overlay rejected | |
| A21 | DRAFT artifact, unattended | FAILURE at preflight | |
| A22 | Tampered, off-allowlist step | FAILURE at preflight | |

**3b. Unit tests (no browser)**
- **Contracts:** artifacts round-trip through JSON. Unknown fields are rejected. The overlay schema can't hold steps, risk tiers, or permissions.
- **Locator ladder:** exactly 1 match resolves. 0 matches moves to the next candidate. More than 1 stops immediately. Coordinates are always tried last.
- **PolicyEngine:** domain, route, and action allow and deny. IRREVERSIBLE without a token is refused. A token is single use and bound to its run and step, so a replayed or cross-run token is refused.
- **Token state machine:** every valid transition in D5 works. Invalid transitions raise. `act()` is rejected in all non-AUTOMATION states. Claim and hold timeouts behave as specified.
- **Classifier:** every exit maps to exactly one bucket, and precedence follows OQ#1.
- **Lifecycle:** only DRAFT to APPROVED to DEPRECATED is allowed. Approving vN+1 deprecates vN. A revision creates a new version that points to its parent.
- **Overlay merge:** deterministic, and the same inputs give the same hash.
- **Redactor:** pattern cases, and typed values are never kept in human events.

**3c. Cross-cutting invariants (run over the whole suite)**
- **No LLM in replay:** `ReplayEngine`'s import graph has no `anthropic`. The replay suite passes with `ANTHROPIC_API_KEY` unset and network access blocked except localhost.
- **Determinism:** the same artifact, inputs, and fault profile run 5 times give identical step traces.
- **PII scan:** after the suite, grep every file under `runs/` and `artifacts/` for the canary strings (SSN, DOB, full member id, password). Zero hits.
- **Exactly one bucket:** every `result.json` validates against the `RunResult` union.

**3d. Discovery tests (`@pytest.mark.live`, need an API key, not in the default run)**
- A goal for the savings balance gives a DRAFT. After approval, it replays A1 successfully.
- A max-steps stop gives "no artifact".
- The action gateway blocks navigation off the allowlist, and the reason is fed back to the model.
- An irreversible step during discovery requests approval.
- Human events from a handoff produce a proposed DRAFT revision.

---

## 4. Critical backbone items (from the diagrams)

**Backbone (must exist for any diagram to work, in build order):**
1. **Pydantic contracts (S5):** everything reads or writes them. (D1, D3, D8, D9)
2. **Surface interface + PlaywrightSurface:** observe, resolve, act, snapshot. This is the only code that touches Playwright. (D10)
3. **Locator ladder resolver:** it carries the determinism and ambiguity guarantees. (D3)
4. **ReplayEngine step loop:** pre-checkpoint, resolve, act, outcome detectors, post-checkpoint. (D3)
5. **Result classifier + FAILURE evidence:** exactly 4 buckets. (D1, D3, D4)
6. **PolicyEngine:** allowlist, risk gate, approval token. It is shared by discovery and replay. (D2, D3, D7)
7. **Preflight:** status, allowlist, fingerprint, input schema. (D3, D8)
8. **Recovery handler registry + outcome detectors:** these keep recoverable conditions out of FAILURE. (D3, D4)
9. **SessionController token state machine:** guards `act()`. (D5, D6)
10. **Redactor on every persistence path.** (D6, D7, D10)
11. **DiscoveryAgent + Recorder:** built last. It is checked against the golden artifacts. (D1, D2)

**Thin or deferrable (keep it minimal, but the diagram still has to hold):**
- `OperatorConsole`: a CLI plus a scripted mock
- Human-proposed DRAFT revision: store the events, then emit a minimal draft
- Overlays: one tenant (`tenant_b`)
- Desktop Surface: interface only
- Lifecycle: CLI commands, no UI

---

## 5. Traceability

Every diagram maps to the backbone items (section 4), setup items (section 1), and tests (section 3) that prove it.

| Diagram | Backbone items | Setup | Tests that prove it |
|---|---|---|---|
| D1 End-to-end pipeline | 1, 4, 5, 11 | S5, S9, S11 | A1, A21, no-LLM invariant, discovery-to-replay (3d) |
| D2 Discovery loop | 6, 11 | S1, S2, S6, S12 | all of 3d |
| D3 Replay execution | 2, 3, 4, 5, 6, 7, 8 | S3, S4, S5, S12 | A1 to A12, A21, A22, determinism invariant |
| D4 Error taxonomy | 5, 8 | S4 | see the table below |
| D5 Control-token state machine | 9 | S10, S12 | token state machine unit tests, A13, A14 |
| D6 Handoff sequence | 9, 10 | S7, S10 | A13, A15, PII scan invariant |
| D7 Irreversible action gate | 6, 10 | S6, S10 | A16, A17, token binding unit test |
| D8 Multi-tenant resolution | 1, 7 | S8 | A10, A19, A20, overlay merge unit test |
| D9 Artifact lifecycle | 1 | S9, S11 | lifecycle unit tests, A21 |
| D10 Component map | 2, 10 | S1, S3 | no-LLM import graph invariant, PII scan invariant |

**Error taxonomy coverage (D4)**

| Condition | Acceptance test |
|---|---|
| Input validation error | A11 |
| App field validation error | A18 |
| Record not found | A3 |
| Permission denied | A12 |
| Known dialog | A4 |
| Unknown dialog | A13 (handoff succeeds), A14 (handoff fails) |
| Session timeout | A15 |
| Slow load | A5 (within budget), A6 (budget exhausted) |
| App error | A7 (transient), A8 (persistent) |
| Ambiguous target | A9 |
| Drift detected | A10 |
| Irreversible step pending approval | A16 (approve), A17 (deny) |

---

## 6. Implementation status

Everything above is built. `make test` runs 162 tests offline (no LLM, no API key) in about 2 minutes. Beyond the acceptance matrix, `tests/scenarios.py` defines 38 stress scenarios across seven categories. They run both as tests and through `scripts/stress.py`, which also runs a flakiness study and a determinism check and writes `evidence/stress/`. Five real Claude discovery runs and their replays are in `evidence/` (see `evidence/README.md`).

| Plan item | Where it lives | Proven by |
|---|---|---|
| S3, S4 MockBankApp + faults | `mockbank/app.py`, `mockbank/data.py` | every acceptance test |
| S5 contracts | `src/cua/contracts.py` | `tests/test_units.py` (contracts) |
| S6, S12 policy, budgets | `config/policy.yaml`, `config/settings.yaml`, `src/cua/policy.py` | policy unit tests, A22 |
| S7 redaction | `src/cua/redactor.py` | redactor unit tests, PII scan invariant |
| S8 tenants + overlays | `config/tenants.yaml`, `overlays/`, `src/cua/overlay.py` | A19, A19b, A20, overlay unit tests |
| S9 storage + lifecycle | `src/cua/store.py`, `artifacts/` | lifecycle unit tests |
| S10 operator channel | `src/cua/session.py`, `src/cua/operator.py` | A13 to A17, token unit tests |
| S11 CLI | `src/cua/cli.py` | manual run (see README) |
| Backbone 2 Surface | `src/cua/surface/` | all integration tests |
| Backbone 3 locator ladder | `src/cua/locators.py` | ladder unit tests, A9 |
| Backbone 4, 5, 7, 8 replay | `src/cua/replay/engine.py`, `src/cua/replay/detectors.py`, `config/detectors/coreone.yaml` | A1 to A22 |
| Backbone 11 discovery | `src/cua/discovery/agent.py`, `src/cua/discovery/recorder.py` | `tests/test_discovery.py` (scripted model), live test with `make test-live` |
| Human-proposed revision | `src/cua/revision.py` | `test_human_proposed_revision_creates_new_draft` |

Additions made while building, beyond sections 1 to 5:
- A13b (checkpoint fails, human fixes it on a second round), A14b (operator cancels, so ESCALATED), A14c (a blocker with no operator configured, so FAILURE `UNRECOVERABLE_BLOCKER`), A17b (approval timeout commits nothing), and A17c (irreversible step with no operator is refused).
- Discovery drops a locator candidate that isn't unique at record time. The ambiguous "View" role+name candidate is never stored, so replay's stop-on-ambiguity rule only fires on real ambiguity (A9).
- Discovery never records dialog dismissals as steps; replay's recovery handlers own them.
- Discovery is tested offline with a scripted model client (`tests/fake_llm.py`). The discovered artifact is checked against the golden one on three members (success, not found, ambiguous).

**Evals (added later).** Runtime telemetry (`src/cua/metrics.py`), the replay and discovery evals, the LLM judge and its calibration (`src/cua/evals/`), and the scorecard are described in [EVALS.md](EVALS.md). Allowlist refusals now have their own reason code, `POLICY_VIOLATION`, with a stage.
