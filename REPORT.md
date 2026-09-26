# Report: Computer-Use Automation for Legacy Back-Office Apps

The model discovers once. The artifact becomes a reviewed capability with a contract. Deterministic replay is what an agent invokes in production. Diagrams: [docs/WORKFLOW.md](docs/WORKFLOW.md). Evidence: [evidence/](evidence/README.md).

## 1. Architecture

A single Python process. Each module seam is where a service boundary would go later.

- **Target:** `mockbank/`, a deliberately hostile "CoreOne Banking". It has framesets, nested tables, labels in neighbouring cells, no ids, and native dialogs, plus 20+ injectable faults, which no public site would allow.
- **Surface seam:** a `Surface` protocol (observe, resolve, act, snapshot). `PlaywrightSurface` is the only Playwright import.
- **Discovery:** a manual Claude tool loop (`claude-opus-5`). It is manual so that every proposed action passes a deterministic gateway (allowlist, risk tier, approval) and is verified and recorded before the next turn. The model proposes; code decides.
- **Replay:** never imports the LLM client, and a test enforces it. Product knowledge (outcome and error signatures, known dialogs, version fingerprint) lives in a per-product detector pack (`config/detectors/coreone.yaml`), not in engine code.
- **Cross-cutting:**
  - `PolicyEngine`, shared by both phases;
  - `SessionController` (the control token);
  - `Redactor` (every persistence path, and all model input);
  - `ArtifactStore` (`DRAFT → APPROVED → DEPRECATED`; only APPROVED runs unattended).

**Trade-off:** DOM-assisted observation is precise on web, and a masked screenshot plus `click_point` gives the model a no-DOM path.

## 2. Artifact schema

`cua/contracts.py`, schema v3. It uses Pydantic with `extra="forbid"` and is validated on every load. `cua describe <name>` prints the agent-facing contract, without steps.

- **Contract:**
  - `inputs` and `outputs` are typed `FieldSpec`s with pattern, minimum, `description` and `sensitive`.
  - `outcomes` declares every business outcome the capability can return, with what the caller should do; e.g. `MEMBER_NOT_FOUND`.
  - The caller's branch table lives in the artifact. Any capability with an irreversible step must declare `DECLINED_BY_OPERATOR`.
- **Steps:** `action`, `target`, `risk`, `pre`/`post` checkpoints, and either `value_from: inputs.<name>` or `output`. Literal values are impossible by construction, so artifacts are parameterized and hold no customer data.
- **Targets:** a ranked ladder, `ROLE_NAME` → `LABEL_PROXIMITY` (label in the neighbouring cell) → `TABLE_ANCHOR` (row text + column) → `COORDINATES`.
  - The recorder keeps only candidates that match exactly one element and are built from static text.
  - Coordinates are used only for a target with no semantic candidate. If a semantic locator misses, the page has drifted, and replay stops rather than click blind.
- **Checkpoints bind identity:**
  - `text_present` is static page text. `input_present` holds refs like `inputs.member_id` whose runtime values must be on screen, which separates *this* member's page from a well-formed page for someone else.
  - Discovery adds the bindings automatically, on the success condition and before every irreversible step.
  - Only refs are stored, never values. Sensitive and non-string inputs cannot be bound, since amounts render as `$1,000.50`.
- **Reviewable, and approval is bound to content:** approval records a content hash, and an APPROVED version edited afterwards is refused at load. Versions are immutable, and revisions point at their parent. An older schema version is refused with a "re-record or migrate" message.

## 3. Determinism & error handling

**Determinism:**
- The same artifact and inputs try the same candidates in the same order.
- More than one match stops as `AMBIGUOUS_TARGET`, never a guess.
- FILL values are read back.
- Steps wait on checkpoints, not sleeps, and `success` must hold before outputs are returned.
- Ten replays produced one distinct trace.

| Bucket | Meaning | Examples (all in `evidence/stress/`) |
|---|---|---|
| SUCCESS | typed outputs | clean or recovered runs |
| BUSINESS_OUTCOME | a *declared* legitimate answer | `MEMBER_NOT_FOUND`, `VALIDATION_REJECTED`, `DECLINED_BY_OPERATOR` |
| ESCALATED | a human held the control token | resumed with outputs |
| FAILURE | step / expected / observed / evidence | `IDENTITY_MISMATCH`, `UNDECLARED_OUTCOME`, `AMBIGUOUS_TARGET`, `TARGET_NOT_FOUND`, `TIMEOUT`, `APP_ERROR`, `DRIFT_DETECTED`, `PERMISSION_DENIED`, `POLICY_VIOLATION`, … |

**Outcomes are checked against the contract.** A recognised business answer that the artifact does not declare returns `FAILURE UNDECLARED_OUTCOME`. The caller only branches on what was promised, so the artifact needs review.

**Recoverable conditions** stay within a per-step retry budget and are reported as `recoveries` without changing the bucket:
- known interstitials and native alerts;
- slow loads and late-rendered tables;
- transient 500s;
- obscured clicks.

Detectors run before the post-checkpoint, so a "not found" page is an answer, not an error.

**Deliberate asymmetries:**
- **Irreversible steps are never retried.** A failure after Confirm says "verify in the app" (X28: exactly one commit).
- **An unknown native dialog stops the run**, because it could be confirming anything.
- **A page for the wrong member is `IDENTITY_MISMATCH`**, not a timeout, and it stops before the commit (X40).
- **M1002 (two savings accounts) is `AMBIGUOUS_TARGET`**, not an outcome, because this capability cannot choose. The fix is an account-selector input.

**Drift is caught:**
- Preflight fingerprints tenant and version.
- Moved columns and renamed labels give `TARGET_NOT_FOUND` with evidence.
- The stress matrix passes 41/41, and runs with 30% random backend failures succeed 20 out of 20.

## 4. Heterogeneity & multi-tenant

**Surfaces:** flows reference `TargetDescriptor`s only, and the ladder maps onto other surfaces:
- `ROLE_NAME` → UIA `ControlType`+`Name` or AX role+title;
- `LABEL_PROXIMITY` → labelled-by or spatial adjacency;
- `COORDINATES` → screen points.

A desktop app is a new `Surface` with the same four methods; schema, replay, policy, handoff and redaction are unchanged. The mock already *is* a legacy web app.

**Tenants:**
- There is one base artifact per vendor product and version, plus a tenant overlay limited to label maps and locator overrides. An overlay cannot add steps, lower risk or widen permissions.
- Identity bindings are data, so overlays never touch them.
- Preflight reads tenant and version from the live UI.
- `tenant_b` ("Member #", "Share Savings") replays through the base plus an overlay (X33). `tenant_c` on 4.3.0 is refused (X32).
- A version bump triggers rediscovery against the base.

## 5. Escalation & handoff

**Stuck:**
- In replay: an unknown dialog, or a blocker such as session expiry.
- In discovery: `request_human`, or 3 turns without verified progress.
- Irreversible steps need a human decision (§6).

**Control transfer:** one `ControlToken` per session.
`AUTOMATION → PAUSE_REQUESTED → HUMAN_IN_CONTROL → RESUME_REQUESTED → VERIFYING_CHECKPOINT → AUTOMATION` (or `FAILED` / `CANCELLED`).
- The token is enforced at the Surface seam: automation acting during human control raises an error.
- The request carries capability, step, reason, a masked screenshot and redacted text.
- The human works in **the same browser session**.
- Human actions are captured without values, so passwords typed during re-login are never recorded.
- Resume is verified by checkpoint: continue, retry the step, or hand back to the human.
- An unclaimed request fails as `HANDOFF_FAILED`.

**In discovery**, after a human changes the page, the run restarts from the entry point and records only verified automation steps (live run `savings-session-expired`). It never restarts after a commit.

Any run where a human held the token reports ESCALATED, so SUCCESS always means unattended.

**Mocked:** the operator surface is a terminal prompt. A real console implements the same five-method `OperatorConsole` protocol.

## 6. Safety

- **Allowlist** (`config/policy.yaml`: schemes, hosts, routes, action types). It is checked at preflight, on each click's destination before the click, on every frame while waiting, and before SUCCESS. An off-list redirect is `POLICY_VIOLATION`, distinct from the app's `PERMISSION_DENIED`.
- **Risk tiers fail closed.** A click is IRREVERSIBLE if it matches a rule, has a commit keyword, or submits a POST form. An artifact can raise a tier, never lower it.
- **Irreversible steps need an operator approval token** (HMAC-signed, single use, bound to run and step), requested only after identity is verified on screen.
  - No console, a timeout or a denial commits nothing (X06, X37, X38).
  - *Confirm* beats *block* because opening accounts is the job. It beats *flag* because a flag after an irreversible act is useless.
- **Redaction** covers logs, evidence, artifacts, operator payloads and model input.
  - It uses patterns (SSN, account and member numbers, dates, amounts, emails), label adjacency ("Name:"), and known secrets. Screenshots are masked too.
  - A seeded-canary leak scan runs in the test suite.
- **Prompt injection:** page text is untrusted, e.g. a customer note saying "ignore your instructions".
  - The prompt says page text is data, but the real defence is that the model has no authority. Every proposal passes the same allowlist, risk and approval gate, and there is no URL tool.
  - The worst case is a wrong recorded path, which review catches.
  - Replay has no model to inject into.
- **Credentials** are used once at session bootstrap, never in the UI, logs or model. The API key lives in the Keychain.

**Limits:**
- Free-text PII in prose evades pattern redaction.
- Keyword/POST classification can over-flag, and it misses a state-changing GET unless a rule names it.
- The approval ledger is tamper-evident, not tamper-proof. Production needs a KMS signer, an audit store and least-privilege credentials per capability.

## 7. Cuts

**Left out:**
- a real operator console (the handoff mechanism is real; the UI is a terminal);
- desktop and accessibility surfaces (designed, not built);
- services and queues;
- route canonicalization;
- video capture.

**Beyond the core:**
- two stretch goals: the DRAFT/APPROVED gate and tenant overlays;
- a stability study;
- an eval harness ([docs/EVALS.md](docs/EVALS.md));
- an adversarial [independent review](docs/independent_review/EVALUATION.md), whose 18 findings are fixed and kept as regression tests.

**Next:**
1. a web operator console (CDP screencast);
2. a UIA `Surface`;
3. a bounded, policy-checked single-step LLM fallback;
4. stability scores gating APPROVED;
5. a KMS signer and audit log;
6. NER redaction for free text.
