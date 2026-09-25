# Report: Computer-Use Automation for Legacy Back-Office Apps

The model discovers once; the artifact becomes a reviewed capability; deterministic replay is what an agent invokes in production. The design is drawn in [docs/WORKFLOW.md](docs/WORKFLOW.md) (10 diagrams), and the evidence is in [evidence/](evidence/README.md).

## 1. Architecture

A single Python process with clear module seams. Keeping it simple suits a take-home, and each seam is where a service boundary would go later.

- **Target.** `mockbank/` is a deliberately hostile "CoreOne Banking" app: framesets, nested tables, labels in neighbouring cells, no ids or test ids, div modals, native `alert`/`confirm`, three tenants, and 20+ injectable faults. A public site would not let me inject session expiry, 500s, or drift on demand.
- **Surface seam** (`cua/surface`). A `Surface` protocol has four jobs: observe, resolve, act, and snapshot. `PlaywrightSurface` is the only module that imports Playwright. Everything else (discovery, replay, session control) talks to the protocol and sees only abstract `TargetDescriptor`s.
- **Discovery** (`cua/discovery`). A manual tool loop on Claude (`claude-opus-5`, adaptive thinking, prompt caching, refusal fallbacks). It is manual rather than the SDK tool runner because every proposed action must pass a deterministic gateway (allowlist, risk tier, approval), be verified, and be recorded before the next turn. The model proposes; code decides.
- **Replay** (`cua/replay`). Deterministic and never imports the LLM client; a test checks the import graph. It is config-driven through a per-product *detector pack* (`config/detectors/coreone.yaml`) holding business outcomes, known dialogs, blockers, and fingerprint rules. Product knowledge lives in data, not engine code.
- **Cross-cutting.**
  - `PolicyEngine`: shared by discovery and replay.
  - `SessionController`: control token and handoff.
  - `Redactor`: on every persistence path and on everything sent to the model.
  - `EvidenceSink` and `ArtifactStore`: lifecycle `DRAFT -> APPROVED -> DEPRECATED`, and only APPROVED runs unattended.

- **Evals and telemetry** ([docs/EVALS.md](docs/EVALS.md)):
  - every run appends a metrics row, covering allowlist refusals by stage, locator fallback rate, escalations, and cost;
  - the replay eval gates CI;
  - a discovery eval grades whether discovered artifacts replay like the golden ones, plus a Sonnet 5 judge calibrated at 100% agreement on 35 labelled items. The first run found false passes, a navigate-then-stop safety gap, a missing wait tool, and silent model fallback, and all four were fixed (baseline 0.82 → v2 1.00 task success);
  - [evals/SCORECARD.md](evals/SCORECARD.md) combines them.

**Trade-offs.**
- DOM-assisted observation is precise on web. The cost is that the model also gets a masked screenshot plus a `click_point` tool, so a no-ref path exists (section 4).
- Synchronous and single-browser: fine for one session. Queued workers are a deployment concern, not a design one.

## 2. Artifact schema

`cua/contracts.py` defines the schema, a Pydantic model with `extra="forbid"` everywhere. An example discovered by the live model is [evidence/artifacts/get_savings_balance/v4.json](evidence/artifacts/get_savings_balance/v4.json).

- **Contract first.**
  - `name`, `version`, `status`, and `goal`.
  - `inputs` and `outputs` as typed `FieldSpec`s: string / decimal / enum, pattern, minimum, and a `sensitive` flag.
  - `success`: a capability-level checkpoint, verified after the last step.
  - `compatible_versions` of the vendor product.
  - `provenance`: discovery run id, model, parent version.
  - A calling agent can read what it needs and what it gets back without reading the steps.
- **Steps.** Each step has an `action` (CLICK/FILL/SELECT/EXTRACT), a `target`, a `risk` tier, `pre` and `post` checkpoints, and either `value_from: inputs.<name>` (writes) or `output` (reads). Literal values are impossible by construction: the model fills fields *by input name*, so the artifact is parameterized and carries no customer data.
- **Targets.** A `TargetDescriptor` holds a frame plus a ranked ladder of `LocatorCandidate`s: `ROLE_NAME` → `LABEL_PROXIMITY` (the label in the neighbouring cell) → `TABLE_ANCHOR` (static text in the same row + column index) → `COORDINATES`. Ladder order is validated. It follows from robustness on legacy UIs:
  - Accessible name is the most meaning-bearing signal.
  - Label adjacency survives restyling.
  - Row anchors identify a row by what it *is* ("Savings"), not by where it is.
  - Coordinates are a last resort.
  - The recorder keeps only candidates that match **exactly one** element at record time, and uses only static text. Anything with digits or anything the Redactor would change is rejected, so ids, amounts, and PII never become anchors.
- **Reviewable.** Artifacts are plain JSON with human descriptions and a content hash that excludes status. Versions are immutable except for status; a revision is a new version with a parent pointer.

## 3. Determinism & error handling

**Determinism.**
- Same artifact and inputs give the same candidates tried in the same order.
- More than one match stops as `AMBIGUOUS_TARGET` instead of guessing a row.
- FILL values are read back and must equal the input; a truncated field is `ACTION_FAILED`.
- Every step waits on checkpoints, not sleeps, and the capability's `success` condition must hold before outputs are returned.
- In the stress harness, 10 baseline replays produced one distinct trace.

**Result contract.** Exactly one bucket for every exit, including unanticipated exceptions, which become `FAILURE UNEXPECTED_ERROR` with evidence.

| Bucket | Meaning | Examples (all exercised in `evidence/stress/`) |
|---|---|---|
| SUCCESS | typed outputs | clean runs, and runs that recovered |
| BUSINESS_OUTCOME | a legitimate answer | `MEMBER_NOT_FOUND`, app-side `VALIDATION_REJECTED`, `DECLINED_BY_OPERATOR` |
| ESCALATED | a human held the control token | resumed with outputs, or operator took over |
| FAILURE | stop, with step / expected / observed / evidence ref | `TIMEOUT`, `APP_ERROR`, `AMBIGUOUS_TARGET`, `TARGET_NOT_FOUND`, `ACTION_FAILED`, `DRIFT_DETECTED`, `PERMISSION_DENIED` (the app refused), `POLICY_VIOLATION` (our allowlist refused), `UNRECOVERABLE_BLOCKER`, `HANDOFF_FAILED`, `VALIDATION_ERROR` (bad caller input, rejected before the UI is touched) |

**Recoverable conditions** are handled inside replay within a per-step retry budget. They are listed on every result as `recoveries`, so the caller sees them without the bucket changing:
- dismiss a known interstitial;
- accept a known native alert;
- wait out a slow load or a late-rendered table;
- reload after a transient 500;
- retry an obscured click.

**Detection order after each action.** Business outcome and hard-failure detectors run first, then the known and unknown dialog checks, then the post-checkpoint. A "not found" page fails the checkpoint but is an answer, not an error.

**Two deliberate asymmetries.**
- **Irreversible steps are never retried.** A 500 or timeout after Confirm fails with "verify in the app" (stress X28 shows exactly one commit).
- **An unknown native dialog is dismissed and the run stops**, because it could be asking to confirm anything.

**Drift is secondary** because the UIs are stable, but it is caught:
- Preflight fingerprints tenant and app version, which gives `DRIFT_DETECTED` before any action.
- Column reorders or renamed labels give `TARGET_NOT_FOUND` with evidence.

The stress matrix passes 38/38. With 30% of backend requests failing at random, 20/20 runs still succeeded.

## 4. Heterogeneity & multi-tenant

**Surfaces.**
- The recorded flow references `TargetDescriptor`s, never Playwright objects, and the ladder kinds map naturally to other surfaces:
  - `ROLE_NAME` maps to UIA `ControlType` + `Name` or AX role + title.
  - `LABEL_PROXIMITY` maps to the labelled-by relationship or spatial adjacency.
  - `COORDINATES` maps to screen points.
- A desktop app becomes a new `Surface` implementation (UIA / AX / screenshot) with the same four methods. The artifact schema, replay engine, policy, handoff, and redaction do not change.
- The discovery loop already has a screenshot-coordinate action (`click_point`). The recorder turns whatever sits at that point into a locator ladder, which is the path a surface with no usable tree would take (tested offline).
- Legacy web is what the mock *is*: framesets, table layouts, and no test ids are handled today.

**Tenants.**
- One **base artifact per vendor product and version**, plus a **tenant overlay** that can only hold label maps and locator overrides. The overlay schema cannot express steps, risk tiers, or permissions, so a tenant can re-point a control but cannot widen what the capability does.
- The effective artifact is computed deterministically (same inputs give the same hash).
- Preflight reads tenant and version from the live UI; a mismatch is `DRIFT_DETECTED`, not a broken run.
- Demonstrated: `tenant_b` relabels "Member ID" to "Member #" and "Savings" to "Share Savings", and replays through the base artifact plus a small overlay file (X33). `tenant_c` on 4.3.0 is refused (X32).
- At scale: overlays are the per-tenant unit of review, and a version bump triggers rediscovery against the base, not per-tenant rebuilds.

## 5. Escalation & handoff

**Detecting "stuck".**
- **Replay:** an unknown dialog, or a blocker such as session expiry.
- **Discovery:** the model calls `request_human`, or makes 3 turns without progress. Progress means a verified effect or a successful extract; blocked or no-op actions don't count.
- **Irreversible steps** need a human decision (approval, below).

**Control-transfer model.** One `ControlToken` per live session:

`AUTOMATION → PAUSE_REQUESTED → HUMAN_IN_CONTROL → RESUME_REQUESTED → VERIFYING_CHECKPOINT → AUTOMATION` (or `FAILED` / `CANCELLED`)

- The token is enforced at the Surface seam: every automation `act()` passes a guard, so acting while a human holds the token raises and is logged.
- The intervention request carries capability, step, reason, a masked screenshot, and redacted page text.
- The human works in **the same browser session**. In interactive use the CLI forces a visible browser whenever an operator is attached.
- The human's actions are captured by an injected listener as event types and targets. **Values are never captured**, so a password typed during re-login is never recorded.
- Resume is not trusted blindly. Checkpoint verification decides "human finished the step" (continue) or "step still needed" (retry); if neither holds, control goes back to the human, up to a limit.
- Timeouts: an unclaimed request ends `HANDOFF_FAILED`; operator cancel or a hold timeout ends `ESCALATED`.

**Discovery handoffs.** Captured events have no values, so they cannot become replayable steps. After a human changes the page, discovery restarts from the entry point: the model is told what the human did and redoes it, and the recorder captures only verified automation steps. The live run `savings-session-expired` shows this. Claude asked for help because it has no credentials; the operator signed in, and discovery restarted and produced a complete artifact. There is no restart after a commit.

**Precedence.** Any run where a human held the token reports ESCALATED (with outputs if it finished), so SUCCESS always means fully unattended. The irreversible approval uses the same channel but does **not** transfer the token.

**Mocked.** The operator surface is a terminal prompt next to the headed browser (`CLIOperatorConsole`), and a scripted operator in tests and evidence. A real console would implement the same five-method `OperatorConsole` protocol.

## 6. Safety

- **Allowlist** (`config/policy.yaml`) covers hosts, routes, and action types. It is checked at preflight for every step, before each action, and *while waiting on every checkpoint*, so a mid-flow redirect off-list stops as `POLICY_VIOLATION` with its stage (X34, X35). Allowlist refusals are counted separately from the app's own `PERMISSION_DENIED`.
- **Risk tiers fail closed.** A click is IRREVERSIBLE if any of these hold:
  - it matches an explicit rule;
  - its label contains a commit keyword (confirm, submit, delete, transfer, and so on);
  - **it submits a POST form**, a structural signal that works on legacy apps with no semantic markup.
- An artifact can raise its tier but never lower it below the policy's classification.
- IRREVERSIBLE requires an operator **approval token**: HMAC-signed, single use, bound to run and step. Without a console, a timeout, or a denial, nothing is committed (X06, X37, X38).
- I chose *require confirmation* over *block* because opening accounts is the job. I chose it over *flag* because a flag after the fact is useless for an irreversible action.
- **Redaction.** The following are masked in logs, evidence text, artifacts, intervention and approval payloads, and everything sent to the model:
  - pattern-based: SSN, dates, emails, account numbers, member ids, currency amounts;
  - label-based: the cell next to "Name:" or "Address:" (works on table layouts without a clean DOM);
  - known secrets.
- Screenshots mask the same patterns and labels, plus password fields.
- Outputs marked `sensitive` (the balance) are returned to the caller but masked in `result.json`.
- The store refuses to save an artifact containing anything redactable.
- Credentials are used once at session bootstrap over HTTP. They never touch the UI, logs, or the model. The API key lives in the macOS Keychain.
- A leak scan with seeded canaries (SSNs, DOBs, names, ids, balances, password) runs over all evidence and is part of the test suite. It caught two real leaks during development: balances quoted by the model, and names on screen. Both are fixed.

**Limits.**
- Pattern and label redaction cannot recognise free-text PII outside those shapes; a name mentioned in prose is not caught.
- The model necessarily sees masked screenshots, and whatever the masks miss goes to the API.
- Keyword and POST classification can over-flag, which is safe but noisy, and cannot see a GET that mutates state; explicit rules cover those.
- Tokens are process-local; production needs a shared signer and audit store.

## 7. Cuts

**Deliberately left out.**
- A real operator console (co-browsing, queueing, auth). The handoff mechanism is real; the UI is a terminal.
- Desktop and accessibility surfaces. Covered by design (section 4) and the coordinate path; not built.
- Service boundaries, queues, and multi-browser concurrency.
- Canonicalizing routes into patterns. Not needed: replay never navigates by URL.
- Video recording, because masking applies to screenshots, not video frames.

**Built beyond the core, deliberately.** These are two stretch goals:
- The DRAFT/APPROVED gate on unattended replay.
- Cross-tenant reuse via overlays.

`propose-revision` (captured human actions become a DRAFT for review) and signed approval tokens are small extras.

**Next with more time.**
1. A web operator console on the same `OperatorConsole` protocol (live view via CDP screencast).
2. A UIA `Surface` for one desktop app.
3. A bounded, policy-checked LLM fallback for a single failed replay step, recorded as evidence.
4. Artifact stability scoring from the flakiness harness, to gate APPROVED.
5. A shared token signer and audit log.
6. NER-based redaction for free-text PII.
