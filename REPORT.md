# Report: Computer-Use Automation for Legacy Back-Office Apps

The model discovers once; the artifact becomes a reviewed capability; deterministic replay is what an agent invokes in production. The design is drawn in [docs/WORKFLOW.md](docs/WORKFLOW.md) (10 diagrams), and the evidence is in [evidence/](evidence/README.md).

## 1. Architecture

A single Python process; each module seam is where a service boundary would go later.

- **Target.** `mockbank/` is a deliberately hostile "CoreOne Banking" app: framesets, nested tables, labels in neighbouring cells, no ids or test ids, div modals, native `alert`/`confirm`, three tenants, and 20+ injectable faults (a public site would not let me inject session expiry, 500s, or drift on demand).
- **Surface seam** (`cua/surface`). A `Surface` protocol (observe, resolve, act, snapshot). `PlaywrightSurface` is the only module that imports Playwright; discovery, replay, and session control see only abstract `TargetDescriptor`s.
- **Discovery** (`cua/discovery`). A manual tool loop on Claude (`claude-opus-5`, adaptive thinking, prompt caching, refusal fallbacks). Manual rather than the SDK tool runner because every proposed action must pass a deterministic gateway (allowlist, risk tier, approval), be verified, and be recorded before the next turn. The model proposes; code decides.
- **Replay** (`cua/replay`). Deterministic and never imports the LLM client (a test checks the import graph). Product knowledge lives in a per-product *detector pack* (`config/detectors/coreone.yaml`: business outcomes, known dialogs, blockers, fingerprint rules), not in engine code.
- **Cross-cutting.** `PolicyEngine` (shared by both phases), `SessionController` (control token, handoff), `Redactor` (every persistence path and everything sent to the model), `EvidenceSink`, and `ArtifactStore` (`DRAFT -> APPROVED -> DEPRECATED`; only APPROVED runs unattended).
- **Evals and telemetry** ([docs/EVALS.md](docs/EVALS.md)). Every run appends a metrics row (allowlist refusals by stage, locator fallback rate, escalations, cost); the replay eval gates CI; a discovery eval checks that discovered artifacts replay like the golden ones, with a Sonnet 5 judge calibrated at 100% agreement on 35 labelled items. Its first run found four real defects, all fixed (task success 0.82 → 1.00).

**Trade-offs.** DOM-assisted observation is precise on web; the model also gets a masked screenshot and a `click_point` tool, so a no-ref path exists (section 4). Synchronous and single-browser is fine for one session; queued workers are a deployment concern.

## 2. Artifact schema

`cua/contracts.py`: Pydantic with `extra="forbid"` everywhere. A live-discovered example: [evidence/artifacts/get_savings_balance/v4.json](evidence/artifacts/get_savings_balance/v4.json).

- **Contract first.** `name`, `version`, `status`, `goal`; typed `inputs`/`outputs` (`FieldSpec`: string / decimal / enum, pattern, minimum, `sensitive`); a capability-level `success` checkpoint; `compatible_versions`; `provenance`. A calling agent knows what it supplies and gets back without reading the steps.
- **Steps.** `action` (CLICK/FILL/SELECT/EXTRACT), `target`, `risk`, `pre`/`post` checkpoints, and either `value_from: inputs.<name>` or `output`. Literal values are impossible by construction, so artifacts are parameterized and carry no customer data.
- **Targets.** A frame plus a ranked, validated ladder of `LocatorCandidate`s: `ROLE_NAME` → `LABEL_PROXIMITY` (label in the neighbouring cell) → `TABLE_ANCHOR` (static text in the same row + column) → `COORDINATES`. Accessible name carries the most meaning, label adjacency survives restyling, and row anchors identify a row by what it *is* ("Savings"). The recorder keeps only candidates that match **exactly one** element, built from static text only (nothing with digits or anything redactable), so ids, amounts, and PII never become anchors.
- **Coordinates are not a fallback.** They are used only for a target with no semantic candidate (the screenshot path, visible to the reviewer). Once a semantic locator exists and misses, the page has drifted; a blind click at a stored point cannot see a second matching row or a moved column, so replay stops as `TARGET_NOT_FOUND`.
- **Reviewable, and approval is bound to content.** Plain JSON with human descriptions and a content hash that excludes status. `approve` records the hash in `approvals.json`; an APPROVED version edited after review, or flipped to APPROVED on disk with no recorded approval, is refused at load. Versions are immutable except for status; a revision is a new version with a parent pointer.

## 3. Determinism & error handling

**Determinism.** The same artifact and inputs try the same candidates in the same order. More than one match stops as `AMBIGUOUS_TARGET` instead of guessing a row. FILL values are read back and must equal the input, whatever locator found the field (truncation is `ACTION_FAILED`). Steps wait on checkpoints, not sleeps, and the `success` condition must hold before outputs are returned. Ten baseline replays in the stress harness produced one distinct trace.

**Result contract.** Exactly one bucket for every exit, including unanticipated exceptions (`FAILURE UNEXPECTED_ERROR`, attributed to the step that was running, with evidence).

| Bucket | Meaning | Examples (all exercised in `evidence/stress/`) |
|---|---|---|
| SUCCESS | typed outputs | clean runs, and runs that recovered |
| BUSINESS_OUTCOME | a legitimate answer | `MEMBER_NOT_FOUND`, app-side `VALIDATION_REJECTED`, `DECLINED_BY_OPERATOR` |
| ESCALATED | a human held the control token | resumed with outputs, or operator took over |
| FAILURE | stop, with step / expected / observed / evidence ref | `TIMEOUT`, `APP_ERROR`, `AMBIGUOUS_TARGET`, `TARGET_NOT_FOUND`, `ACTION_FAILED`, `DRIFT_DETECTED`, `PERMISSION_DENIED` (the app refused), `POLICY_VIOLATION` (our allowlist refused), `UNRECOVERABLE_BLOCKER`, `HANDOFF_FAILED`, `VALIDATION_ERROR` |

**Recoverable conditions** are handled within a per-step retry budget and listed on every result as `recoveries` without changing the bucket: dismiss a known interstitial, accept a known native alert, wait out a slow load or late-rendered table, reload after a transient 500, retry an obscured click. After each action, business-outcome and hard-failure detectors run first, then dialog checks, then the post-checkpoint, so a "not found" page is an answer, not an error.

**Two deliberate asymmetries.** Irreversible steps are never retried: a 500 or timeout after Confirm fails with "verify in the app" (X28 shows exactly one commit). An unknown native dialog is dismissed and the run stops, because it could be confirming anything.

**Drift** is secondary (the UIs are stable) but caught: preflight fingerprints tenant and app version (`DRIFT_DETECTED` before any action), and column reorders or renamed labels give `TARGET_NOT_FOUND` with evidence, on live-discovered artifacts too. The stress matrix passes 38/38; with 30% of backend requests failing at random, 20/20 runs still succeeded.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The flow references `TargetDescriptor`s, never Playwright objects, and the ladder maps to other surfaces: `ROLE_NAME` to UIA `ControlType`+`Name` or AX role+title, `LABEL_PROXIMITY` to labelled-by or spatial adjacency, `COORDINATES` to screen points. A desktop app is a new `Surface` (UIA / AX / screenshot) with the same four methods; schema, replay, policy, handoff, and redaction do not change. Discovery already has a screenshot-coordinate action (`click_point`) whose recorder output is the path a surface with no usable tree would take. Legacy web (framesets, table layouts, no test ids) is what the mock *is*.

**Tenants.** One **base artifact per vendor product and version**, plus a **tenant overlay** that can hold only label maps and locator overrides, so a tenant can re-point a control but cannot add steps, lower risk, or widen permissions. The effective artifact is deterministic (same inputs, same hash), and preflight reads tenant and version from the live UI. Demonstrated: `tenant_b` relabels "Member ID" → "Member #" and "Savings" → "Share Savings" and replays through the base plus a small overlay (X33); `tenant_c` on 4.3.0 is refused (X32). At scale, overlays are the per-tenant unit of review, and a version bump triggers rediscovery against the base.

## 5. Escalation & handoff

**Detecting "stuck".** Replay: an unknown dialog or a blocker such as session expiry. Discovery: the model calls `request_human`, or makes 3 turns without verified progress. Irreversible steps need a human decision (approval, below).

**Control transfer.** One `ControlToken` per live session: `AUTOMATION → PAUSE_REQUESTED → HUMAN_IN_CONTROL → RESUME_REQUESTED → VERIFYING_CHECKPOINT → AUTOMATION` (or `FAILED` / `CANCELLED`).

- Enforced at the Surface seam: every automation `act()` passes a guard, so acting while a human holds the token raises and is logged.
- The intervention request carries capability, step, reason, a masked screenshot, and redacted page text. The human works in **the same browser session** (the CLI forces a visible browser whenever an operator is attached).
- Human actions are captured as event types and targets; **values are never captured**, so a password typed during re-login is never recorded.
- Resume is verified, not trusted: checkpoints decide "human finished the step" (continue) or "step still needed" (retry); otherwise control returns to the human, up to a limit. A broken operator channel fails the handoff cleanly (`HANDOFF_FAILED`). An unclaimed request ends `HANDOFF_FAILED`; cancel or hold timeout ends `ESCALATED`.

**Discovery handoffs.** Captured events have no values, so they cannot become steps. After a human changes the page, discovery restarts from the entry point and the model redoes what the human did; only verified automation steps are recorded. The live run `savings-session-expired` shows this (Claude asked for help because it has no credentials; the operator signed in; discovery restarted and produced a complete artifact). No restart after a commit.

**Precedence.** Any run where a human held the token reports ESCALATED (with outputs if it finished), so SUCCESS always means fully unattended. Approval uses the same channel but does **not** transfer the token. **Mocked:** the operator surface is a terminal prompt (`CLIOperatorConsole`), and a scripted operator in tests; a real console implements the same five-method `OperatorConsole` protocol.

## 6. Safety

- **Allowlist** (`config/policy.yaml`): schemes (http/https only), hosts, routes (dot segments resolved first), and action types. Checked at preflight for every step; on the destination of every click before it happens, coordinate clicks included; on **every frame** before each action and while waiting on every checkpoint (before and after the read); and once more before SUCCESS is returned. A redirect off-list stops as `POLICY_VIOLATION` with its stage (X34, X35), counted separately from the app's `PERMISSION_DENIED`.
- **Risk tiers fail closed.** A click is IRREVERSIBLE if it matches an explicit rule, its label contains a commit keyword, or **it submits a POST form** (a structural signal that works without semantic markup). An artifact can raise its tier but never lower it below the policy's classification.
- IRREVERSIBLE needs an operator **approval token** (HMAC-signed, single use, bound to run and step). No console, a timeout, or a denial commits nothing (X06, X37, X38). I chose *require confirmation* over *block* because opening accounts is the job, and over *flag* because a flag after an irreversible action is useless.
- **DRAFTs never run unattended.** Only an APPROVED artifact whose content matches its recorded approval runs without an operator; `--attended` requires an attached operator and cannot be combined with `--unattended`.
- **Redaction** in logs, evidence, artifacts, intervention and approval payloads, and model input: patterns (SSN, card numbers, dates, emails, account numbers, member ids, amounts), case-insensitive labels (the cell next to "Name:" or "Address:"), and known secrets. Screenshots mask the same plus password fields. Fields marked `sensitive` are masked outright in anything persisted, inputs and outputs alike (outputs are still returned to the caller). The store refuses to save a redactable artifact.
- Credentials are used once at session bootstrap over HTTP and never touch the UI, logs, or model; the API key lives in the macOS Keychain. A seeded-canary leak scan over all evidence is part of the test suite; it caught two real leaks during development (balances quoted by the model, names on screen), both fixed.

**Limits.** Pattern and label redaction misses free-text PII in prose, and whatever screenshot masks miss goes to the API. Keyword/POST classification can over-flag (safe but noisy) and cannot see a GET that mutates state; explicit rules cover those. Tokens are process-local, and the approval ledger is tamper-evident, not tamper-proof (someone who can rewrite both files can forge it); production needs a shared signer (KMS) and audit store.

## 7. Cuts

**Left out.** A real operator console (the handoff mechanism is real; the UI is a terminal). Desktop and accessibility surfaces (designed, section 4; not built). Service boundaries, queues, multi-browser concurrency. Route canonicalization (replay never navigates by URL). Video recording of runs (masking applies to screenshots, not video frames).

**Built beyond the core.** The DRAFT/APPROVED gate and cross-tenant overlays (two stretch goals); `propose-revision` and signed approval tokens (small extras). The evals layer, walkthrough video, and playground are presentation aids, not part of the graded core.

**Next.** (1) A web operator console on the same protocol (CDP screencast). (2) A UIA `Surface` for one desktop app. (3) A bounded, policy-checked LLM fallback for a single failed replay step, recorded as evidence. (4) Stability scoring from the flakiness harness to gate APPROVED. (5) A shared signer for tokens and approvals, with an audit log. (6) NER-based redaction for free-text PII.

An independent review ([docs/independent_review/](docs/independent_review/EVALUATION.md)) wrote 48 adversarial tests; the 18 defects it found (coordinate fallback bypassing ambiguity and allowlist checks, approval not bound to content, sensitive inputs persisted, and smaller ones) are fixed and those tests now pass as regressions.
