# Independent Evaluation: Assignment A (Computer-Use Automation System)

This evaluation scores the submission against [RUBRIC.md](RUBRIC.md), which was written before the code was read.
I judged it from the code and from tests I ran myself. I did not take README, REPORT or docs claims on trust.
All runs were offline: no live LLM calls, no key scripts, no Keychain access.

## Verdict

**Weighted total: 80.4 / 100. Hire (strong), and discuss the safety findings below in the interview.**

This is a strong, coherent vertical slice that touches every requirement in spec section 3:
- the artifact schema is deliberate and typed;
- replay never reaches the LLM, and a test proves it;
- the result contract has real buckets, and a data-driven detector pack sits behind it;
- control-token handoff runs on the same live session;
- the live discovery evidence is genuine.

It is probably near the top of a typical pool.

The main weakness is one under-examined design choice. **Every discovered artifact gets a COORDINATES fallback (recorder.py:52-53).** Replay then trusts that fallback blindly. On that path three protections are lost:
- the ambiguity guard;
- the FILL read-back;
- the pre-click allowlist check.

So the headline safety and determinism claims hold for the hand-written golden artifacts that the stress matrix uses, but not for the artifacts that discovery actually produces. A second gap: approval is not bound to artifact content, so an APPROVED file edited on disk still runs unattended.

## Test runs

| Suite | Result |
|---|---|
| Existing offline suite (`make test`, before adding tests) | **162 passed**, 1 deselected (live), in 151 s. This matches the README claim. |
| New independent tests (`tests/independent/`) | **30 passed, 18 xfail** (each xfail is a confirmed finding), across 48 test items |
| Full suite with the new tests (`make test`) | 192 passed, 18 xfailed, 1 deselected: green |

I confirmed that every xfail fails for the stated reason by re-running with `--runxfail` and reading each assertion.

## Score table

Half points are used where the evidence falls between two rubric anchors.

| # | Criterion | Weight | Score | Weighted |
|---|---|---:|---:|---:|
| C1 | Artifact schema and capability contract | 14 | 4.0 | 11.2 |
| C2 | Core loop fidelity | 14 | 5.0 | 14.0 |
| C3 | Replay robustness (taxonomy, locators, drift) | 14 | 3.5 | 9.8 |
| C4 | Human-in-the-loop and control transfer | 12 | 4.5 | 10.8 |
| C5 | Safety (allowlist, irreversible, approvals, redaction) | 12 | 3.0 | 7.2 |
| C6 | Test quality | 9 | 4.0 | 7.2 |
| C7 | Architecture and code quality | 8 | 4.0 | 6.4 |
| C8 | Heterogeneity and multi-tenant | 6 | 4.5 | 5.4 |
| C9 | Operator/reviewer experience and docs | 5 | 4.0 | 4.0 |
| C10 | Honesty of claims | 4 | 4.0 | 3.2 |
| C11 | Product judgement and scope | 2 | 3.0 | 1.2 |
| | **Total** | **100** | | **80.4** |

## Evidence per criterion

### C1 Artifact schema: 4.0
**Strengths.**
- Pydantic with `extra="forbid"` throughout (contracts.py:25-26).
- `LocatorCandidate` validates the fields each kind needs (contracts.py:106-117).
- Ladder order is enforced (contracts.py:125-130).
- Writes can only reference inputs (`value_from` pattern, contracts.py:190, 201-212), so literal customer data cannot enter an artifact.
- Cross-reference checks: every declared output is extracted, and no step uses an unknown input (contracts.py:239-254).
- Each step carries a risk tier and pre/post checkpoints. The capability has a `success` checkpoint, `compatible_versions`, provenance, and a `sensitive` flag on fields.
- My malformed-artifact probes (6 variants) were all rejected at load.

**Why not 5.**
- `content_hash()` (contracts.py:256-259) is informational only. It is never bound to approval (see C5).
- Robustness reasoning lives in the REPORT, not in the artifact.
- Business outcomes live in the per-product detector pack rather than in the capability contract. That is defensible, but a calling agent cannot see from the artifact which business codes it may receive.
- There is no agent-facing tool-schema projection.

### C2 Core loop: 5.0
- Goal spec leads to a discovery loop with a deterministic action gateway (agent.py:381-439), then the recorder (recorder.py), then a DRAFT, then approve, then replay (engine.py).
- The replay import graph excludes `anthropic` and `cua.discovery`, and a test verifies this (test_zz_invariants.py:46-53).
- The `/evidence` discovery runs look genuine:
  - `model_usage` events with plausible latencies and cache writes, for example `evidence/discovery/disc-2026-09-25T002942-aa7985/events.jsonl`;
  - 5 runs, including handoffs;
  - replays of the discovered artifacts, including error cases.
- A scripted fake LLM (tests/fake_llm.py) makes the discovery-to-replay round trip testable offline.

### C3 Replay robustness: 3.5
**Strengths.**
- Four buckets, discriminated by type (contracts.py:301-331).
- A detailed `FailureReason` enum. A catch-all turns anything unexpected into a structured FAILURE (engine.py:99-101).
- Detectors are data-driven and ordered (detectors.py:42-65).
- Retries are bounded (engine.py:333-382). Irreversible steps are never retried (engine.py:257-259, 351-353).
- FILL values are read back (engine.py:265-270).
- The fingerprint catches drift before any action (engine.py:160-168).
- My determinism probes passed: 3 repeats of each of 3 scenarios gave identical traces.

**Defects found.**
- **COORDINATES resolves blindly.** `resolve()` returns count 1 whenever anything is under the point (playwright_surface.py:276-281), and every discovered target ends with such a candidate. As a result:
  - the ambiguity guard is bypassed on a relabelled tenant: M1002 returns one of its two savings balances as SUCCESS (xfail `test_coordinate_fallback_does_not_bypass_the_ambiguity_guard`);
  - FILL read-back is skipped for coordinate matches (engine.py:265). A truncated input then becomes a false `BUSINESS_OUTCOME MEMBER_NOT_FOUND`, which is exactly the conflation the spec warns about (xfail `test_coordinate_fill_is_verified_like_any_other_fill`);
  - under column reorder, the live-discovered evidence artifact clicks a wrong cell and ends in TIMEOUT, not TARGET_NOT_FOUND (xfail `test_discovered_artifact_reports_column_reorder_as_target_not_found`).
- An existing test, test_metrics.py:40-53, asserts that a coordinate fallback after a rename is SUCCESS. That enshrines the risky behavior.
- Minor: UNEXPECTED_ERROR attributes the failure to the last *completed* step (engine.py:100). A crash in s7's approval is reported at s6, and a notify crash during s2's handoff is reported at s1.

### C4 HITL: 4.5
- The `ControlToken` state machine has explicit legal transitions (session.py:24-63).
- The guard is installed at the Surface seam (session.py:128-139, playwright_surface.py:340, 348, 390, 397).
- The human operates the same live page (`human_page`, playwright_surface.py:194-196). DOM listeners capture event types, never values (playwright_surface.py:88-105).
- Resume goes through checkpoint verification, can bounce back to the human, and has limits (session.py:204-223).
- There are claim and hold timeouts. Console errors fail the handoff (session.py:159-172).

My probes found that:
- automation `goto`, `reload` and dismiss calls are all rejected while the human holds the token;
- resuming without fixing anything bounces back to the human and then ends HANDOFF_FAILED;
- values the human types never reach disk.

**Gaps.**
- `console.notify` is unwrapped (session.py:222), so a broken channel becomes UNEXPECTED_ERROR (xfail).
- Replay checks only the top, main and step frames (engine.py:444-448). A frame the human leaves off the allowlist goes unnoticed (xfail, low).
- There is a single operator with no lease identity, which is acceptable for the scope.

### C5 Safety: 3.0
**Strengths.**
- Risk classification fails closed: an explicit rule, a commit keyword, or a POST submit all mean IRREVERSIBLE (policy.py:76-90).
- A declared tier can never lower the policy's classification (policy.py:92-95). My probe confirmed that editing every step to READ still forces approval.
- Approval tokens are HMAC-signed, single use, and bound to run and step (policy.py:97-115). My forgery and cross-engine probes were rejected.
- There is a pre-click URL check for real element matches (engine.py:233-237).
- Redaction covers every sink, including the model input (agent.py:166-192), and the store refuses PII (store.py:50-54).
- A canary leak scan runs in the suite.

**Defects.**
1. **Approval is not bound to content** (store.py:37-38, 74-79). An APPROVED file edited on disk replays unattended; my probe re-pointed Savings to Checking and got SUCCESS with the wrong balance. A DRAFT whose status is flipped on disk also runs unattended. Two xfails.
2. **A coordinate click reaches an off-allowlist page and reports SUCCESS**, 3/3 runs. Three things combine:
   - `target_url` is never set for coordinate matches, so the pre-click check is skipped (engine.py:233);
   - the post-wait checks URLs *before* reading the checkpoint, while `frame_text` auto-waits into the new document (engine.py:298-299, a TOCTOU race);
   - there is no URL check before SUCCESS (engine.py:177-186).

   There is also no network-level choke point such as `page.route`.
3. `check_url` fails open for URLs with no netloc, such as `file:` and `javascript:` (policy.py:54-55). The route prefix check does not normalise dot-segments (policy.py:48-50). Both are defense-in-depth gaps.
4. `--attended --unattended` together run a DRAFT with no operator (cli.py:72, engine.py:126).
5. The `sensitive` flag applies to outputs only. A sensitive input is persisted verbatim in `result.json` (engine.py:111).
6. Redaction patterns miss space-grouped card numbers and upper-case labels (redactor.py:16, 19-28). The REPORT acknowledges this class of limit.

### C6 Test quality: 4.0
**Strengths.**
- 162 behavioral tests against real Chromium and a hostile mock with more than 20 injectable faults.
- Invariants: exactly one bucket per run, no LLM import, canary scan.
- A stress matrix of 38 scenarios, and an offline scripted-model discovery suite.
- Tests assert on the contract, not on internals, and the suite runs in about 2.5 minutes.

**Why not 5.**
- The stress and drift tests all use hand-written golden artifacts with no coordinate candidates. The failure modes of *discovered* artifacts are therefore untested, and one test enshrines the risky fallback (test_metrics.py:40-53).
- There are no tamper, integrity or CLI error-path tests.

### C7 Architecture: 4.0
**Strengths.**
- A real Surface protocol (surface/__init__.py:93-123). Playwright is isolated to one module.
- Per-run context objects. Product knowledge lives in config.
- Readable, typed code of reasonable size (about 3.3k lines of core).

**Deductions.**
- The coordinate handle is a tuple that leaks into the engine (`isinstance(match.handle, tuple)`, engine.py:265). That breaks the seam and is the root of the read-back bypass.
- The exception-driven control flow in `_execute`/`_recover` (engine.py:190-246, 333-399) is dense and hard to reason about.

### C8 Heterogeneity and multi-tenant: 4.5
- Overlays can only relabel or override locators; they cannot express steps or permissions (contracts.py:262-268, overlay.py:32-52).
- The tenant_b overlay is demonstrated. Tenant and version drift is caught by the fingerprint.
- A screenshot-coordinate discovery path exists. The REPORT maps the ladder onto UIA/AX credibly.
- Not 5: per-vendor-version base management is described only, and the coordinate path it relies on for "no DOM" is the weak spot in C3.

### C9 Reviewer experience: 4.0
**Strengths.**
- The README has setup, the key story, an offline mode, exact demo commands, and fault injection instructions.
- The evidence is indexed with per-run tables. There are diagrams and a walkthrough.

**Deductions.**
- REPORT.md is about 2,300 words, about 4-5 pages against the 1-3 page guide.
- CLI error paths print raw tracebacks: bad `--inputs` JSON, a missing version, an illegal transition (xfail).
- The git repo has no commits yet (`git log`: "does not have any commits yet"). The spec requires a public repo, so this must be fixed before submission.

### C10 Honesty: 4.0
Most claims hold; see the table below. The REPORT's limits section is candid. The mismatches all come from the golden-versus-discovered gap and look like blind spots rather than spin.

### C11 Product judgement: 3.0
The core is focused and the cuts are well argued. However, a lot of work went into breadth the spec explicitly says it will not reward:
- an eval framework with an LLM judge and calibration (about 1.4k lines);
- a scorecard;
- a narrated video with generated music;
- an HTML playground.

Meanwhile a load-bearing path (the coordinate fallback) went un-threat-modeled.

## Claims check

| Claim | Where | Verdict |
|---|---|---|
| "162 tests" | README.md:30 | **Holds.** 162 passed. |
| Replay never imports the LLM client | REPORT.md:12 | **Holds.** The test and the import graph confirm it. |
| Stress matrix 38/38 | REPORT.md:84 | **Holds**, but only with golden artifacts. |
| Judge calibrated at 100% agreement on 35 items | REPORT.md:22 | **Holds** per `evals/judge_calibration/v2/calibration.json`. The set is self-labelled and small per criterion (n = 6-10). |
| Real discovery runs in /evidence | evidence/README.md | **Holds.** The telemetry looks genuine and there is no PII canary in evidence or runs. |
| "More than one match stops as AMBIGUOUS_TARGET instead of guessing" | REPORT.md:53 | **Partly false.** When upper rungs miss, COORDINATES guesses (xfail). |
| "Column reorders or renamed labels give TARGET_NOT_FOUND" | REPORT.md:82 | **False for discovered artifacts.** The evidence v4 artifact gives TIMEOUT after a wrong-cell click. True for the golden. |
| Allowlist checked "before each action, and while waiting on every checkpoint" | REPORT.md:130 | **Partly false.** Coordinate clicks skip the pre-check, and the checkpoint wait has a race. |
| "Coordinates are a last resort" | REPORT.md:45 | True in order, but the last resort has no safety net. |
| Content hash, "immutable except for status" | REPORT.md:47 | The hash exists but is never enforced; files can be edited freely. |
| Irreversible steps never retried; approval required | REPORT.md | **Holds**, including via the coordinate path and a lowered declared tier (my probes). |

## Top strengths
1. The result contract and error taxonomy separate business outcomes, recoverable conditions and hard failures cleanly. They are enforced by type and data-driven through the detector pack.
2. A real control-token handoff on the same live session: a guard at the seam, verify-on-resume, value-free capture of human actions, and timeouts.
3. Parameterization by input name. The model never sees or writes literal values, so artifacts cannot carry customer data.
4. Fail-closed risk classification (POST submit means IRREVERSIBLE), signed single-use approvals, and irreversible steps that are never retried.
5. Genuine live discovery evidence, plus offline scripted-model tests of the whole discovery-to-replay loop.

## Top weaknesses and risks
1. **The COORDINATES fallback on every discovered target** (recorder.py:52-53; playwright_surface.py:276-281; engine.py:233, 265). It silently defeats the ambiguity guard, the FILL read-back and the pre-click allowlist check. It turns drift into wrong-answer SUCCESS or false business outcomes. This is the most important finding.
2. **Approval is not bound to content** (store.py:37-38, 74-79). Edited or forged APPROVED files run unattended.
3. **The allowlist is not enforced at a choke point.** There is a TOCTOU race in the post-wait (engine.py:298-299), no URL check before SUCCESS (engine.py:177-186), the policy fails open on netloc-less URLs (policy.py:54-55), and replay leaves some frames unchecked (engine.py:444-448).
4. The stress and drift evidence was produced with golden artifacts, so the robustness claims were not validated on what discovery produces.
5. Smaller issues:
   - the `sensitive` flag is ignored for inputs (engine.py:111);
   - `--attended --unattended` together run a DRAFT (cli.py:72);
   - CLI tracebacks;
   - step misattribution on UNEXPECTED_ERROR (engine.py:100);
   - `notify` is unwrapped (session.py:222);
   - scope sprawl;
   - REPORT over length;
   - no commits in the repo.

## New test results (`tests/independent/`)

| Test | What it probes | Result |
|---|---|---|
| test_offline_probes::test_malformed_artifact_on_disk_is_rejected_at_load [6 variants] | truncated JSON, missing success, unknown step field, literal value, un-extracted output, ladder order | pass (6) |
| test_offline_probes::test_non_http_schemes_are_not_on_the_allowlist [file, javascript] | allowlist fails open for netloc-less URLs (policy.py:54-55) | **xfail-finding (2)** |
| test_offline_probes::test_dot_segment_route_cannot_escape_an_allowed_prefix | `/member/../admin/audit` prefix bypass (policy.py:48-50) | **xfail-finding** |
| test_offline_probes::test_host_and_route_lookalikes_are_refused [4 variants] | lookalike hosts, off-list routes, `/memberx` | pass (4) |
| test_offline_probes::test_approval_token_forgery_and_cross_engine_reuse_rejected | a re-bound, altered or foreign-key token is refused; the genuine token works once | pass |
| test_offline_probes::test_env_secrets_and_secret_keys_are_redacted_in_evidence | env secret, key-based nested secrets in events.jsonl | pass |
| test_offline_probes::test_pii_shapes_on_a_legacy_page_are_redacted | name, SSN, DOB, email, account number, balance | pass |
| test_offline_probes::test_space_grouped_card_number_is_redacted | PAN pattern missing | **xfail-finding (low)** |
| test_offline_probes::test_upper_case_sensitive_label_is_redacted | case-sensitive label redaction | **xfail-finding (low)** |
| test_replay_adversarial::test_tampered_approved_artifact_is_refused | an approved file edited on disk returns the Checking balance as SUCCESS | **xfail-finding (high)** |
| test_replay_adversarial::test_status_forged_on_disk_does_not_grant_unattended_replay | a DRAFT flipped to APPROVED on disk runs unattended | **xfail-finding (high)** |
| test_replay_adversarial::test_deprecated_artifact_is_refused_unattended_without_touching_ui | lifecycle gate | pass |
| test_replay_adversarial::test_lowering_declared_risk_does_not_skip_the_commit_approval | all steps edited to READ; commit still gated, nothing created | pass |
| test_replay_adversarial::test_start_route_off_allowlist_refused_before_any_ui_action | preflight on start route | pass |
| test_replay_adversarial::test_role_name_click_toward_off_allowlist_page_refused_before_click | control case: pre-click check works for real matches | pass |
| test_replay_adversarial::test_coordinate_click_toward_off_allowlist_page_refused_before_click | coordinate click to /admin/audit gives SUCCESS | **xfail-finding (high)** |
| test_replay_adversarial::test_coordinate_fallback_does_not_bypass_the_ambiguity_guard | relabelled tenant + two savings rows gives SUCCESS | **xfail-finding (high)** |
| test_replay_adversarial::test_discovered_artifact_reports_column_reorder_as_target_not_found | REPORT claim tested on the evidence v4 artifact: TIMEOUT after a wrong click | **xfail-finding (claim)** |
| test_replay_adversarial::test_golden_truncated_fill_is_action_failed | control case: read-back works for real matches | pass |
| test_replay_adversarial::test_coordinate_fill_is_verified_like_any_other_fill | coordinate FILL skips read-back, giving a false MEMBER_NOT_FOUND | **xfail-finding (high)** |
| test_replay_adversarial::test_commit_reached_by_coordinate_fallback_still_needs_approval | coordinate-resolved Confirm with declared READ is still gated | pass |
| test_replay_adversarial::test_output_schema_violation_is_failure_without_leaking_values | OUTPUT_INVALID, no balance in the result | pass |
| test_replay_adversarial::test_unmet_success_condition_returns_no_outputs | SUCCESS_CONDITION_UNMET withholds outputs | pass |
| test_replay_adversarial::test_sensitive_input_is_never_persisted | a sensitive input is written verbatim to result.json | **xfail-finding (medium)** |
| test_replay_adversarial::test_repeated_replays_are_identical [F2, M9999, F6] | determinism under interstitial, not-found, transient 500 | pass (3) |
| test_handoff_and_cli::test_automation_cannot_act_while_the_human_holds_the_token | goto, reload and dismiss are all rejected mid-handoff; act_rejected is logged | pass |
| test_handoff_and_cli::test_resume_without_fixing_bounces_back_then_fails | verify-on-resume, bounce back, HANDOFF_FAILED, no automation acts | pass |
| test_handoff_and_cli::test_values_typed_by_the_human_are_never_captured | human keystroke values never persisted | pass |
| test_handoff_and_cli::test_session_left_off_allowlist_by_the_human_is_not_resumed | nav frame off the allowlist is unchecked on resume | **xfail-finding (low)** |
| test_handoff_and_cli::test_broken_approval_channel_fails_closed | approval console crash: FAILURE, nothing committed | pass |
| test_handoff_and_cli::test_broken_notify_channel_fails_the_handoff_not_the_run | notify is unwrapped, giving UNEXPECTED_ERROR | **xfail-finding (low)** |
| test_handoff_and_cli::test_cli_unknown_capability_exits_cleanly | exit 2, clean message | pass |
| test_handoff_and_cli::test_cli_replay_happy_path_and_masking | CLI replay under an isolated CUA_ROOT; balance masked on disk | pass |
| test_handoff_and_cli::test_cli_error_paths_have_no_traceback [bad_json, missing_version, illegal_transition] | raw tracebacks | **xfail-finding (3, low)** |
| test_handoff_and_cli::test_cli_draft_never_runs_without_an_operator | `--attended --unattended` runs a DRAFT with nobody present | **xfail-finding (medium)** |

**Tally: 30 passed, 18 xfail findings.** Counted by root cause, the findings group as follows:
- **High (5 root causes):** approval not bound to content; coordinate click skips the allowlist, plus the post-wait race; coordinate bypasses the ambiguity guard; coordinate FILL skips read-back; status forgery. Status forgery shares its root cause with the first.
- **Medium (2):** sensitive inputs persisted; DRAFT runs via a flag combination.
- **Low or claim (rest).**

None of the xfails is a mistake in my own tests. I fixed one assumption of my own during the review: the broken-approval test originally expected `step == "s7"`. The product reports s6, which I now record as a low note rather than a test failure.

## Addendum: remediation (branch `fix/independent-review-findings`)

All 18 `xfail(strict)` findings were fixed, and their tests now run as ordinary regression tests (no xfail marks remain in `tests/independent/`).

| Finding | Fix |
|---|---|
| Coordinate fallback bypasses ambiguity, fill read-back and the column-reorder claim | `locators.py`: COORDINATES is tried only for targets with no semantic candidate; after semantic drift the result is `TARGET_NOT_FOUND`. `recorder.py` no longer appends coordinates to targets that have a semantic locator. The FILL read-back applies to every match kind (`playwright_surface.read_value` reads the control at the point). |
| Coordinate click leaves the allowlist and reports SUCCESS | Point matches carry the clickable ancestor's `target_url` and `submits_post`, so the pre-click check and risk classification apply. URLs are checked on every frame (`Surface.frame_urls`), before and after each post-checkpoint read, and before SUCCESS. |
| Approval not bound to content; status forgeable on disk | `store.py`: `approve` records the content hash in `<name>/approvals.json`; `load` raises `IntegrityError` if the content changed, or `NotApproved` if no approval is recorded; `latest` skips never-approved versions. Committed approved artifacts were backfilled; their hashes match the `artifact_hash` in the original replay evidence. |
| Sensitive inputs persisted | `engine._persistable_inputs` masks sensitive inputs in `result.json` and in approval payloads. |
| `--attended --unattended` runs a DRAFT with no operator | The CLI rejects the combination; the engine refuses a non-APPROVED artifact unless an operator console is attached. |
| `file:` / `javascript:` URLs and `..` routes | `check_url` allows only http(s) plus `about:blank` / `about:srcdoc`; routes are percent-decoded and dot-normalised before matching. |
| Frame left off-allowlist by the human | All frames are checked (see above). |
| Broken notify channel gives UNEXPECTED_ERROR | `session.py` wraps `notify` like the other console calls, so the failure is `HANDOFF_FAILED`. |
| Unexpected errors blamed on the previous step | They are attributed to the step that was running. |
| CLI tracebacks | Bad `--inputs`, a missing version, an illegal transition and integrity errors print one line and exit with code 2. |
| Redaction gaps | Card numbers (13–19 digits, grouped or not); sensitive labels matched case-insensitively. |

Tests changed because they encoded the old behaviour:
- `test_units.py` ladder tests: coordinates are no longer tried after semantic drift.
- `test_metrics.py`: drift is now shown with a second semantic rung.
- `test_discovery.py`: recorded targets carry no coordinate fallback; the revision test approves through the lifecycle.
- The independent coordinate-fill test is split in two: coordinate-only fill gives `ACTION_FAILED` through read-back, and a drifted label gives `TARGET_NOT_FOUND`.

Replay eval after the fixes: correct = 1.0, false SUCCESS = 0.
