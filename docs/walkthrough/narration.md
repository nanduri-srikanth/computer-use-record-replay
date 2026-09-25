# Walkthrough narration

Spoken track of `walkthrough.mp4`, segment by segment.

**01-title**: Here's a computer-use automation system for legacy bank back-office apps. Claude explores the app once. What it learns becomes a versioned artifact that replays deterministically, with no model in the loop.

**02-pipeline**: Five parts. Discovery: Claude drives a real browser toward a goal. An artifact, approved by a person. Deterministic replay, ending in one of four outcomes. Human handoff on the same live session. And evidence for every run, with sensitive data masked.

**03-setup**: Setup is three commands. Make setup builds the Python environment and installs Chromium. The API key script stores your Anthropic key in the macOS Keychain, never on disk. And make test runs 162 offline tests, no key needed.

**04-app**: The target is a mock legacy banking app. Framesets, nested tables, no element ids. Three tenants and over twenty injectable faults, like expired sessions, surprise dialogs, and slow pages. All the data is synthetic.

**05-discovery**: Discovery starts from a short spec: a goal, with typed inputs and outputs. Claude reads the page as text and calls one tool per turn, each with a stated reason. A gateway checks every action against the allowlist first. Six turns later, a draft is saved.

**06-escalate**: When Claude hits something it shouldn't handle, it asks for help. Here the session expires. Claude has no credentials, so it calls request human. An operator signs in on the same browser, and discovery carries on.

**07-artifact**: The recorder turns that trace into an artifact. Each step has a ranked ladder of locators: role and name, then label, then table anchor, and coordinates only as a last resort. It stays a draft until an operator approves it.

**08-replay**: Replay runs the approved artifact with no model at all. Blue outlines are actions; green are reads. Member M1001 returns success, with balance and currency. The caller gets the balance; on disk, it's masked.

**09-buckets**: Every run ends in exactly one of four buckets. An unknown member is a business outcome: an answer, not a crash. Two matching savings rows is a failure, because replay won't guess.

**10-handoff**: Anything that needs a person escalates. Here the session expires mid-run. Automation never types credentials, so it hands a control token to an operator on the same live session. While the human holds it, automation is locked out. The operator signs in and resumes, and replay verifies the page before continuing.

**11-approval**: Irreversible steps, like opening a sub-account, need a single-use approval from an operator. Without one, nothing is committed.

**12-policy**: Here the app redirects off the allowlist. Replay catches it at the next checkpoint and stops with a policy violation. Its own clicks are checked before they happen. And a redactor masks names, SSNs, and balances in every log and screenshot.

**13-evidence**: Every run leaves evidence: an event log, masked screenshots, and a result. The stress matrix injects 38 fault conditions, no key needed. All 38 are classified correctly, with zero false successes and zero leaks.

**14-evals**: Evals keep measuring it. Telemetry on every run. A replay eval in CI. And a live discovery eval that replays what Claude found, graded by a calibrated judge. Across three prompt versions, task success rose from 82 to 100 percent.

**15-recap**: To try it: make setup, make test, make mock, and make replay demo. With a key: discover, evidence live, and eval live. The README, the report, and an interactive playground cover the rest. Thanks for watching!

