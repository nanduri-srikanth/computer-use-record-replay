# Code Review vs. Assignment A

Two-axis review of the whole codebase against *Assignment A: Computer-Use Automation System* (the PDF brief). The repo is not under git yet, so the full codebase was reviewed against an empty baseline. **Standards** used the Fowler smell baseline, because the repo documents no coding standard. **Spec** used the PDF. The load-bearing claims below were re-checked against the source.

## Verdict on the core process

The core loop works, and the tests prove it: goal, then LLM discovery, then DRAFT artifact, then approval, then deterministic replay with typed inputs and outputs, then one of four result buckets, with a live-session handoff guarded by a control token. 84 offline tests pass, and one real-model discovery-then-replay test passed.

The process is **not yet submittable**. Three kinds of gap remain:

1. **Deliverables the brief mandates are missing**: `/evidence/` with a real discovery run, `/REPORT.md`, a git repo, and a discovered (not hand-authored) artifact.
2. **Correctness holes in the core loop**: some exceptions can escape replay without a result, discovery handoffs can silently produce an incomplete artifact, and the CLI handoff runs against an invisible browser.
3. **Safety fails open** for any irreversible control not listed in config.

## Requirement checklist

| PDF requirement | Status | Gap |
|---|---|---|
| §4 "the discovery run has to be real … evidence in /evidence/" | **Missing** | No `evidence/` dir. `runs/` is gitignored, and the live test wrote to pytest temp dirs. |
| §6.2 `/REPORT.md` with the 7 exact headings | **Missing** | Content exists in `docs/WORKFLOW.md` and `docs/BUILD_MAP.md`, but not at the required path or headings. |
| §6.1 public git repo; §9 keep secrets out | **Missing** | Not a git repo yet. Secrets are fine: the key is in the Keychain and `.env` is ignored. |
| §6.3 "a saved example artifact plus logs from both a discovery run and a replay run" | **Missing** | Both artifacts are `created_by: hand-authored`. |
| §6.1 "demo path: … run the agent on a goal, then replay the resulting artifact" | **Partial** | README never chains discover, approve v2, replay. Replay picks up the hand-authored v1. |
| §3.1 goal + target, observe/decide/act, stop conditions | Met | Stops on max steps, timeout, off-allowlist page, denied approval, refusal, and stuck with no operator. |
| §3.1 "Bias toward an approach that would still work when the surface has no clean DOM" | **Partial** | Observation is a DOM query (`playwright_surface.py:14-51`) and the model acts only by DOM ref. The screenshot is context only. The `Surface` seam allows an AX/desktop surface, but the brief asks for that argument, and it needs to be made in REPORT.md. |
| §3.2 steps, targeting, typed inputs and outputs, versioned, reviewable | Met | |
| §3.2 "how each target … is identified (with your reasoning about robustness)" | **Partial** | The ladder order encodes the reasoning, but the artifact carries no per-locator rationale. REPORT.md must explain it. |
| §3.2 "a checkpoint or success condition" | **Partial** | Per-step checkpoints only. There is no capability-level success condition; SUCCESS means "outputs validated" (`engine.py:149-154`). |
| §3.3 replay without LLM, stable targeting, outputs | Met | Enforced by an import-graph test. |
| §3.3 "Distinguish, in your result contract, … recoverable conditions" | **Partial** | Recoveries live on `RunReport`, not `RunResult`, and the CLI prints only `result` (`cli.py:63`), so callers never see them. |
| §3.3 failure with step / expected / observed | Met | Plus an evidence ref to a masked screenshot and redacted page text. |
| §3.4 configurable allowlist, enforced | Met, with one hole | Replay can crash instead of failing cleanly when the start page redirects off the allowlist (see S1). |
| §3.4 "handle the risky class conservatively" | **Wrong default** | `classify` (`policy.py:71-78`) returns READ for any click not listed in `irreversible_controls`, so an unlisted "Delete" or "Transfer" runs ungated. |
| §3.4 "Never persist secrets or raw sensitive data" | Mostly met | Events, evidence text, artifacts, and model input are redacted. `result.json` is written unredacted (`evidence.py:38-41`). It holds outputs and static detector text, not PII, but the rule should be stated or enforced. |
| §3.5 structured log of what the agent did **and why** | Met | Discovery logs each tool call with the model's `reason`. |
| §3.5 richer signal on failure | **Partial** | Replay captures a screenshot and page text. A discovery stop captures nothing. |
| §3.6 intervention request with context | Met | Capability, step, reason, masked screenshot, redacted page text. |
| §3.6 "operate the same live session … record what the human did" | **Partial** | Works in tests. The CLI defaults to headless yet attaches the interactive console (`cli.py:26,58,75`), so the operator is told "You have control of the browser" with no browser visible. In discovery, the human's actions are logged but not recorded as steps (`agent.py:232-236`), so the saved artifact can silently miss them. |
| §3.6 "a way to know who is (or should be) in control" | Met | `ControlToken`, with `act()` guarded at the Surface seam. |
| §3.7 surface abstraction and multi-tenant design | Met | Needs to be written up in REPORT.md §4. |

## Must-fix before submission (ranked)

1. **Produce `/evidence/`**: run real discovery through the CLI, then commit the discovered artifact, the discovery log, a success replay log, and at least one error replay log (M9999 not found, or F7 app error). Stop gitignoring what goes into evidence.
2. **Write `/REPORT.md`** with the seven exact headings, drawn from WORKFLOW.md and BUILD_MAP.md. It must include the locator-robustness reasoning, the no-clean-DOM argument, and a mocks-and-cuts section.
3. **`git init`** and make the first commit, then push to a public repo.
4. **Replay catch-all** (S1): wrap `run()` so any unexpected exception becomes `FAILURE` with evidence, and move `_check_urls` inside the handled path. "Exactly one bucket" must hold for every exit.
5. **Fail closed on risk**: for a click on an unrecognised control on a write-capable route, require approval (or treat it as REVERSIBLE_WRITE and flag it), instead of defaulting to READ. At minimum, state the limit in REPORT.md §6.
6. **CLI handoff**: force `--headed` whenever an operator console is attached, or refuse to escalate when headless.
7. **Discovery handoff**: either record the human's captured actions as steps, or stop and ask for rediscovery after a human takeover. Never save a silently incomplete artifact.
8. **Update the README demo path**: discover, `approve get_savings_balance 2`, replay, and an error replay.

## Standards

The repo documents no standard, so "hard" means a real defect; everything else is a labelled judgement call.

**Hard defects (verified)**
- **S1** `engine.py:85-88,131`: `run()` catches only `_Stop`. A `PolicyViolation` from the start-page URL check, Playwright or `LookupError` failures in `click_role`/`reload`, and exceptions raised inside `except _NeedRecovery` all escape, and no `result.json` is written.
- **S2** `session.py:163`: `transitions=list(t.history)` copies the whole token history, so a second handoff's record repeats the first handoff's transitions.
- **S3** `discovery/agent.py`: `discover()` has no catch-all, so API errors or `LookupError` skip the `discovery_stopped` log.
- **S4** `playwright_surface.py:272-280`: a SELECT through a coordinate fallback clicks and silently ignores the value.
- **S5** `contracts.py:225`: `value_from` without a dot raises a raw `IndexError` rather than a validation error. `FieldSpec(type="enum", enum=None)` validates but can never match.
- **S6** `tests/test_zz_invariants.py:40`: depends on alphabetical file order and on at least 30 prior runs, so it breaks under `-k`, xdist, or random order.
- Dead code and type hints that don't match behaviour:
  - `_execute` never returns "RETRY", so the `else 0` at `engine.py:147` is unreachable.
  - The no-op `try/except: raise` at `engine.py:296-300`.
  - `_stop` should be typed `NoReturn`.
  - `_recover` can fall through and return None.
- `assert` is used as a runtime guard in `session.py:150,196`.
- Per-run state lives on `self` in `ReplayEngine` and `DiscoveryAgent`, so neither is re-entrant, and a stale act guard stays on the surface after a run.
- Mock-only issues: bad form input to `/subaccount/confirm` returns a 500, and `expired_once` is written outside the lock.

**Judgement calls (smells)**
- **Primitive Obsession**:
  - The "NEXT"/"RETRY" verdict strings are shared across engine, session, and agent.
  - The `("point", frame, x, y)` handle tuple.
  - `risk.value == "IRREVERSIBLE"` (`agent.py:285`).
  - Headings tuples indexed as `b[0]`/`b[2]`.
  - `"inputs.<name>"` is parsed in three places.
  - Untyped detector dicts.
- **Duplicated Code**:
  - `d.kind in ("BUSINESS","FAILURE")` appears three times in the engine.
  - The fingerprint poll is duplicated between agent and engine.
  - The role logic in the two JS snippets has drifted.
  - A hardcoded `".modal"` duplicates the detector pack's `dialog_selector`.
  - `ArtifactStore(ROOT/"artifacts")` is built five times in the CLI.
- **Repeated Switches**: on `LocatorKind` (surface, contracts, recorder) and on the tool-name to `ActionType` map in the agent.
- **Divergent Change**: `discovery/agent.py` holds the prompt, tool schemas, observation rendering, the loop, and the gateway.
- **Data Clumps**: `_fail(reason, step, expected, observed)` is called about 20 times.
- **Speculative Generality**: `Surface.text_visible`, `frame_route`, and `HumanEvent.extra` are unused.
- **Mysterious Name**: `nr`, `eff`, `tcfg`, `tu`, `S`.

## Spec

**(a) Missing or partial**: see the checklist rows marked Missing or Partial above. The five deliverable gaps (evidence, REPORT.md, git, a discovered artifact, the demo chain) are the ones an evaluator will hit first. The brief says outright that "we can't assess a description" of discovery.

**(b) Scope beyond the ask.** §8 allows "at most one or two" stretch goals, and §7 says multi-tenant *plumbing* is not rewarded.
- Tenant overlays with drift gating map to the "canonicalization / cross-tenant reuse" stretch goal. The DRAFT/APPROVED lifecycle maps to "confidence & approval". That is two stretch goals, which is within the limit, but REPORT.md should say they were deliberate picks.
- `propose-revision`, HMAC-signed approval tokens, and the mermaid/node tooling are extras. They're small, but they're more to defend. Keep them, and flag them as optional in REPORT.md §7.
- Discovery requires a vendor fingerprint, which ties "goal + target" to a detector pack. Justify it, or make it optional for discovery.

**(c) Looks implemented, but wrong**
- Risk classification fails open (policy row above).
- The CLI handoff has no visible browser.
- Recoverable conditions are absent from `RunResult`.
- `result.json` is written unredacted.
- Discovery after a human takeover can save an incomplete artifact.

## Summary

- **Standards**: 12 findings (6 hard defects plus minor dead-code and typing issues, and about 10 smell groups). Worst: **S1**, where exceptions can escape replay without a result, breaking "exactly one bucket".
- **Spec**: about 17 gaps (5 missing deliverables, 9 partial requirements, 5 wrong implementations, 3 scope notes). Worst: **no `/evidence/` from a real discovery run**, the one requirement the brief marks as not negotiable.

---

## Resolution (follow-up pass)

Every finding above was re-checked against the PDF and addressed, or consciously left open with a reason. "Proof" points at the test or evidence that shows the fix.

### Must-fix list

| # | Finding | Resolution | Proof |
|---|---|---|---|
| 1 | No `/evidence/` from a real discovery | 5 live Claude discovery runs (clean ×2, interstitial, unknown dialog, session expiry with a live handoff), 13 replays of what they recorded, plus the stress matrix | [evidence/README.md](../evidence/README.md) |
| 2 | No `/REPORT.md` | Written with the seven required headings | [REPORT.md](../REPORT.md) |
| 3 | No git repo | `git init` done. Commit and push are left to the author | repo root |
| 4 | Replay can exit without a result (S1) | Catch-all in `run()` gives `FAILURE UNEXPECTED_ERROR` with evidence. The start-page URL check is handled. Step attempts and recoveries share one guard, so policy and control-token violations raised during recovery still map to their bucket | X34, X35; `test_every_run_ends_in_exactly_one_bucket` |
| 5 | Risk classification fails open | IRREVERSIBLE on explicit rule, on commit keyword, **or on POST form submit**. Artifacts cannot lower the tier | `test_risk_classification_and_no_downgrade`; X02, X12, X37 |
| 6 | CLI handoff with no visible browser | An attached operator forces a headed browser; `--unattended` means no operator and headless | `cli.py:_surface` |
| 7 | Discovery can save an incomplete artifact after a human takeover | If the human changed the page, discovery restarts from the entry point and the model is told what the human did. It never restarts after a commit | `test_human_takeover_restarts_discovery_so_artifact_stays_complete`, `test_no_restart_after_a_commit`; live run `savings-session-expired` |
| 8 | README lacks the demo chain | discover → approve v2 → replay (success, not found, bad input, ambiguous) → handoff demo. Commands verified against a running mock | [README.md](../README.md) |

### Spec gaps

| Finding | Resolution |
|---|---|
| No capability-level success condition | `Capability.success` (required) is verified after the last step, giving `SUCCESS_CONDITION_UNMET` if unmet. The recorder derives it from the final page |
| Recoverable conditions not in the result contract | `recoveries` is now a field of every result bucket, and the CLI prints it |
| `result.json` unredacted | `FieldSpec.sensitive`: sensitive outputs are returned in memory and masked on disk (`test_sensitive_outputs_masked_on_disk_but_returned`) |
| Discovery stop has no richer signal | Every discovery stop captures a masked screenshot plus redacted page text (`test_discovery_stop_keeps_evidence`) |
| Observation depends on the DOM | Added `click_point` (screenshot coordinates), which the recorder turns into a ladder (`test_discovery_by_screenshot_coordinates`). REPORT §4 makes the surface argument |
| No per-locator robustness reasoning | Ladder rationale in REPORT §2; each target carries a human description |
| Scope beyond the ask | Overlays and lifecycle are named as the two chosen stretch goals in REPORT §7. `propose-revision` and signed tokens are listed as small extras |

### Standards

| Finding | Resolution |
|---|---|
| S2 cumulative handoff transitions | Each record holds only its own slice (`test_separate_handoffs_keep_separate_transition_logs`, X17) |
| S3 discovery without catch-all | Catch-all logs `discovery_stopped` with evidence |
| S4 SELECT via coordinates silently clicks | Raises `ActionFailed`. The recorder no longer adds a coordinate fallback for `<select>` |
| S5 `value_from` IndexError; enum without values | `value_from` must match `^inputs\.name$` and is only allowed on FILL/SELECT. Enum fields need values (unit tests) |
| S6 order-dependent invariants | Invariant tests now produce the runs they check |
| Dead code, `NoReturn`, fall-through, `assert` guards | Removed or fixed. Explicit `RuntimeError`s replace the asserts |
| Per-run state on `self`; stale act guard | Replay and discovery keep per-run state in a context object. The guard and dialog policy are reset after every run |
| Primitive Obsession: verdict strings, headings tuples, `risk.value ==` | `Verdict` type, `_FrameState` NamedTuple, enum comparison |
| Duplicated `d.kind in (...)`; hardcoded `.modal`; repeated `ArtifactStore(...)` | `Detection.terminal`; the dialog selector comes from the detector pack; a `_store()` helper |
| Repeated Switch on tool name | One `_ACTION_TOOLS` table drives both schemas and gateway |
| Speculative Generality (`text_visible`, `frame_route`, `HumanEvent.extra`) | Removed |
| Mock: 500 on bad confirm input; unlocked `expired_once` | Returns 400; the update happens under the lock |
| Open (judgement calls kept) | `_fail(...)` data clump, the `LocatorKind` switch in the surface (one per surface is the seam's job), and the `agent.py` size |

### Found while fixing (not in the original review)

- **Balances leaked into logs.** Found by the live runs: Claude's `done` summary quoted `$2,450.17`. Currency amounts are now redacted everywhere, and balances are in the leak-scan canaries.
- **Names on screen.** Label-based redaction (the cell next to "Name:" or "Address:") covers text and screenshots.
- **FAILURE after a handoff had no handoff record.** Found by stress X18. `Failure.handoffs` was added.
- **Token counts were redacted as secrets** because their keys contain "token". Fields were renamed rather than weakening the rule.
