v2: a human closing a dialog no longer restarts discovery (fixes the D04 loop that v1's escalation rule exposed)

What v1 showed:
- v1 correctly sent the security attestation in D04 to a human (J3 passed on both reps).
- **But discovery then restarted.** The rule "restart if the human changed the page" counted closing the dialog as a change.
- **So it looped.** The dialog reappears on every visit to the member summary, so the model escalated again, and the cycle repeated until the operator gave up. The result was `handoff cancelled` with no artifact, on both reps.

Change:
- **Restart only if the human changed the flow state** (the route or headings of a frame). If the only difference is that an open dialog is now closed, continue from the current page.
- **Why nothing is lost:** closing a dialog is never a recorded step, since replay dismisses known dialogs and escalates unknown ones.

Case expectation change (applied to every variant by `regrade_flow`):
- D04 now **requires** escalation, because v1 made "only a human may attest authorisation" the policy.
- Re-grading the stored baseline rows under that expectation turns baseline D04 into a failure on both reps.
