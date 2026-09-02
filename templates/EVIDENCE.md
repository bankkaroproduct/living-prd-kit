# EVIDENCE/ — what goes in this folder

> A green checkmark, a passing test, or an AI saying "done" is not proof. Evidence is: the thing ran, here is what it did, dated, with the environment stated.

Every file states, at the top: **date · environment (local/preview URL) · data snapshot (dump date / synthetic) · what was being validated.**

| File | What it is | Minimum bar |
|---|---|---|
| `validation-run-<date>.md` | Systematic validation of logic against independent recomputation | A real matrix (profiles × scenarios), math recomputed outside the prototype, failures clustered by root cause — not a flat bug list. Ask per root cause: "is this the *whole* mechanism?" before closing it |
| `edge-case-walkthrough-<date>.md` | Register X-IDs → trigger steps → screenshot/recording refs | Every `Demonstrable: yes` row in SPEC §7 has an entry |
| `event-audit-<date>.md` | Collector export vs. TRACKING.md diff | 1:1 both directions; property values sane |
| `cold-session-<date>.md` | Full transcript of the G4 cold-session test | Questions asked, citations given, `open_defects: 0` at the end |
| `screens/` | Screenshots / recordings referenced above | Named `<screen>-<state>.png` |

## Cold-session question bank (start here, add feature-specific)

1. Pick 5 field validations at random: "user enters <bad input> on <screen> — what happens, exact error copy, what event fires?"
2. Pick 3 error-matrix rows: "how does the user get out of this state?"
3. "Which displayed numbers can I trust, and why?"
4. "What is mocked, and what must the real integration do differently?"
5. "What's explicitly out of scope for v1?"
6. "Produce the acceptance checklist for the real build."

Answers must cite bundle files/sections. Any question the session can't answer from the bundle = a bundle defect → fix the bundle, retest.
