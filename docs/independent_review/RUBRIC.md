# Independent Hiring Rubric: Assignment A (Computer-Use Automation System)

Written before an in-depth reading of the candidate's code. Every criterion traces back to the
assignment spec (sections 2, 3.1-3.7, 5, 6, 7), plus what we would expect from a strong senior
engineer who knows the spec is deliberately under-specified. The spec lists its evaluation order
as: system design, core-loop correctness, robustness, HITL, generalization, safety, code quality,
communication. The weights below follow that order, with extra weight for honesty and test
quality, because the pool is AI-assisted and self-reported claims are cheap.

Scores: 1 = weak, 2 = below bar, 3 = meets the bar, 4 = strong, 5 = exceptional / best in pool.
Weighted total = sum(score / 5 * weight), out of 100.

| # | Criterion | Weight |
|---|-----------|-------:|
| C1 | Artifact schema and capability contract | 14 |
| C2 | Core loop fidelity (goal, real LLM discovery, artifact, LLM-free replay, handoff, evidence) | 14 |
| C3 | Replay robustness: error taxonomy, checkpoints, locators, waits, drift | 14 |
| C4 | Human-in-the-loop escalation and control transfer | 12 |
| C5 | Safety: allowlist, irreversible actions, approvals, redaction | 12 |
| C6 | Test quality | 9 |
| C7 | Architecture and code quality | 8 |
| C8 | Heterogeneity and multi-tenant design | 6 |
| C9 | Operator/reviewer experience and docs (README, REPORT, evidence) | 5 |
| C10 | Honesty of claims versus reality | 4 |
| C11 | Product judgement and scope | 2 |
| | **Total** | **100** |

---

## C1. Artifact schema and capability contract (14)
Spec 3.2: ordered steps, how each target is identified (with robustness reasoning), typed inputs,
typed outputs, a success checkpoint, versioned and reviewable, decoupled from the transcript.

- **1**: A raw action list or transcript dump. No typed inputs or outputs, no version, and
  locators are single brittle CSS/XPath strings.
- **3**: A typed schema (pydantic/JSON-schema) with steps, typed inputs and outputs, a
  checkpoint, a version field, and a multi-strategy locator. Serialized to YAML/JSON and
  readable by a human.
- **5**: All of the above, plus: the schema validates itself (inputs are referenced, outputs
  are extracted, templates resolve); explicit business-outcome declarations; per-step risk
  classification; a content hash / provenance that ties an approved version to exact bytes;
  a lifecycle (draft to approved); the reasoning for each locator's robustness lives in the
  artifact; and an agent-facing projection (a tool schema).
- **Evidence**: the contracts module, sample specs/, the validators, tests that reject
  malformed artifacts.

## C2. Core loop fidelity (14)
Spec 2 and 5: goal, then a real LLM run, then a saved capability, then deterministic replay with
params, outputs and outcomes, then a human-escalation path on the live session, then evidence
for both runs.

- **1**: One or more links are missing or only described. The replay secretly calls the LLM,
  or discovery never really ran.
- **3**: Every link exists and runs end to end offline (with a fake model) and at least once
  live. The replay imports no LLM client.
- **5**: The thread is demonstrable with one or two commands. There is a structural guarantee
  that replay cannot reach the LLM (an import boundary or a test). Discovery output is compiled
  (parameterized, canonicalized), not just transcribed. Real discovery evidence is committed
  in /evidence.
- **Evidence**: the CLI, the discovery agent/recorder, the replay engine's imports, /evidence
  contents, tests that go from discovery to replay.

## C3. Replay robustness (14)
Spec 3.3: business outcome vs recoverable vs hard failure; validation errors, not-found,
permission denial, unexpected dialog, session timeout, slow or failed loads; a failure result
that says which step, what was expected and what was observed.

- **1**: Happy path only. Exceptions bubble up, or every failure is the same type.
- **3**: A three-way result taxonomy. Detectors for most of the listed conditions, bounded
  retries/waits, checkpoints verified after steps, failure detail with the step, expected and
  observed.
- **5**: A taxonomy enforced by type. Detectors are data-driven and ordered. Locators are
  multi-strategy, with an ambiguity guard (a strict single match, never "click the first").
  Retries are only applied to idempotent steps. Recovery is bounded. Drift is detected and
  reported distinctly from runtime errors. Determinism across repeated runs is measured.
  Injected-failure evidence exists.
- **Evidence**: replay/engine, detectors, locators; the mock app's failure modes; tests that
  inject each condition; the stress/flakiness output.

## C4. Human-in-the-loop escalation and control transfer (12)
Spec 3.6: detect and route with context; a human operates the same live session; hand back;
record what the human did; know who is in control.

- **1**: A TODO or a print("ask human"). Or a fresh browser is opened for the human.
- **3**: An intervention request with context (capability, step, screenshot, reason). The
  same browser session is exposed to the human. An explicit resume signal. Human actions are
  captured in the evidence.
- **5**: An explicit control-state machine (automation / human / released), with tokens or
  leases so that only the holder can act, rejection of stale or forged tokens, timeouts, a
  re-verify-state-on-resume step (the human may have changed the page), and handling of the
  race where automation acts while the human holds control. It is tested.
- **Evidence**: session/operator modules, the control model, tests for token misuse and races.

## C5. Safety (12)
Spec 3.4: an explicit configurable allowlist (domains/routes, action types); reversible vs
irreversible classification; conservative handling of the risky class; no secrets or full PII in
artifacts or logs.

- **1**: No allowlist, or one that is only advisory. Irreversible actions run freely.
  Secrets appear in logs.
- **3**: Config-driven allowlist checks on navigation and actions, in both discovery and
  replay. Irreversible steps are blocked or need approval. Regex/field redaction is applied to
  logs and artifacts.
- **5**: Enforcement at a choke point that cannot be bypassed (the surface layer, including
  redirects and in-page navigation). Approval gates for irreversible steps that fail closed on
  timeout or denial. Redaction applied to every sink (logs, artifacts, screenshots/DOM
  snapshots, LLM prompts). Integrity checks (a hash mismatch means refusal). Unapproved
  artifacts cannot run unattended. Tested adversarially.
- **Evidence**: policy.py, redactor.py, the surface layer, the approval flow, tests.

## C6. Test quality (9)
- **1**: A handful of smoke tests, or tests that mock away everything interesting.
- **3**: An offline suite that covers the core loop against a real browser plus a mock app,
  and covers each error class and the safety checks. It runs green with one command.
- **5**: Behavioral tests at the right seams (the real Playwright surface, a fake LLM),
  invariant/property tests, negative and adversarial cases, determinism checks. Fast, hermetic,
  not flaky. Tests assert on contract outputs, not on internals.
- **Evidence**: tests/, `make test` results, the runtime, skips/xfails.

## C7. Architecture and code quality (8)
- **1**: A monolith script with tangled responsibilities and no types.
- **3**: Clear modules (surface / discovery / artifact / replay / policy / evidence), typed,
  readable, low duplication.
- **5**: Deep modules with small interfaces. The surface abstraction is a real seam. Failure
  handling is explicit. No dead code or feature sprawl. Easy to extend.
- **Evidence**: src/ layout, interfaces, size and complexity of key functions.

## C8. Heterogeneity and multi-tenant design (6)
Spec 3.7 (design, not necessarily build).
- **1**: Not addressed, or the core abstractions hard-code DOM/CSS so that desktop is impossible.
- **3**: A credible write-up. Locators are expressed semantically (role/name/label) so they
  map to accessibility trees. There is an overlay/override concept for tenants.
- **5**: The write-up plus a thin real demonstration: a tenant overlay applied to a variant of
  the app, drift detection per tenant/version, and a surface interface that a desktop backend
  could implement without changing the schema.
- **Evidence**: REPORT section 4, overlay/tenants code, the surface interface.

## C9. Operator/reviewer experience and docs (5)
Spec 6: README with setup, keys, offline mode and exact demo commands; REPORT with seven
headings in about 1-3 pages; /evidence with an artifact, discovery logs and replay logs,
including an error replay.
- **1**: Missing deliverables, or commands that don't work.
- **3**: All deliverables at the exact paths. The demo commands work. Evidence is present.
- **5**: A reviewer can go from clone to replay in minutes. There is a review aid (artifact
  diff/summary). The evidence is indexed and self-explanatory. The REPORT is concise and
  meets the length guide.
- **Evidence**: README, REPORT, evidence/, the CLI help.

## C10. Honesty of claims versus reality (4)
- **1**: The README/REPORT claims features that don't exist or don't work, or test counts or
  metrics are inflated.
- **3**: The claims are broadly accurate, and limitations are acknowledged.
- **5**: Every claim spot-checked holds. Limitations and mocks are called out precisely,
  including the uncomfortable ones.
- **Evidence**: grader spot-checks of each major claim against code and runs.

## C11. Product judgement and scope (2)
The spec says: depth over breadth, no scaling infrastructure, at most one or two stretch goals.
- **1**: Feature sprawl (lots of infrastructure, many stretch goals) at the expense of a solid
  core, or a thin core with no judgement shown.
- **3**: A focused core, with sensible cuts that are documented.
- **5**: Clear evidence of prioritization. Stretch goals are chosen because they reinforce the
  core (for example, approval gating). The cut list is honest and insightful.
- **Evidence**: the REPORT cuts section, the ratio of repo surface area to core.
