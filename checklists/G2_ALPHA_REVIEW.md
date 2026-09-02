# G2 — Alpha Review checklist

Run when the **Alpha** exists: one coherent working journey, end to end. Mocked APIs and synthetic data are fine; for mandate-triggered projects (payments/KYC/PII etc.), **no real data or live integrations before this review**. Output is an **agreed coverage plan**, not handoff approval.

## Input bar (PM, before booking the review)

- [ ] One journey clickable start → finish
- [ ] Manifest fidelity map drafted: what is real, mocked, incomplete, undecided
- [ ] `SPEC.md` skeleton + edge-case register started; `TRACKING.md` drafted; mocks so far in `MOCKS.md`

## Tech lead reviews

- [ ] Feasibility: can this be built on our stack as prototyped?
- [ ] Integrations: are the mocked contracts plausible against the real services?
- [ ] Data boundaries: provenance, PII scrub, nothing real used before now (mandated projects)
- [ ] Reuse potential: which layers look reuse vs rebuild vs reference-only (provisional call)

## QA challenges

- [ ] States: missing states? unreachable states? empty/loading/error everywhere?
- [ ] Validations and error copy: complete? consistent?
- [ ] Errors and retries: every failure path has an exit
- [ ] Testability: can each scenario be triggered deterministically?

## Blocking rule

Reviewers may block **only on feasibility, safety, or testability** — not taste, not scope preferences. Anything else is a note in `DECISIONS.md`.

## Output — the coverage plan

Scenario list with IDs into `prd.manifest.yaml → coverage_plan`: every scenario the finished bundle must demonstrate (happy paths, edge cases, failure paths, tracking checkpoints). These IDs persist through production tickets, tests, and G6 reconcile.

| Sign | By | Date |
|---|---|---|
| Tech lead — feasibility reviewed | | |
| QA — testability reviewed | | |
| Coverage plan agreed (manifest updated) | | |
