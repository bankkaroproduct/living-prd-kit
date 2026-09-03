# G4 — Freeze & approve checklist

Creates the **immutable Handoff Release**. Three parts: the cold-session test, the DoD sweep, then pin + sign. Record everything in `prd.manifest.yaml → release`. PM cannot self-accept; a material change after freeze creates a **new** release.

## Part 1 — cold-session test

1. Open a **fresh AI session with zero build context** (tech-side, not the PM's).
2. Hand it only the bundle root. Prompt:

> You have a Living PRD bundle for `<feature>`. Read `prd.manifest.yaml` first, then the artifacts it indexes. Answer only from the bundle, citing file + section for every claim. Then: (a) answer my behaviour questions; (b) produce the acceptance checklist the build team should build against; (c) list every question you could NOT answer from the bundle alone.

3. Ask the question bank in `templates/EVIDENCE.md` + ≥5 feature-specific questions (pick the hairiest edge cases).
4. **List (c) must be empty.** Anything on it is a bundle defect: fix the bundle, not the answer, and rerun.
5. Save the transcript to `EVIDENCE/cold-session-<date>.md`; set `cold_session.passed` in the manifest.

## Part 2 — Definition of Done sweep

- [ ] Every coverage-plan scenario (from G2) demonstrable, by scenario ID
- [ ] Every user-visible state reachable in the prototype — empty, loading, error included
- [ ] Every field validation fires in the prototype **and** appears in `SPEC.md` with exact error copy
- [ ] Every edge case in the register: demonstrable with linked trigger steps, or `NOT-DEMONSTRABLE` + rationale
- [ ] Every tracking event fires visibly in the collector and matches `TRACKING.md` 1:1 (run the event audit)
- [ ] Every external dependency REAL, or mocked with a full contract in `MOCKS.md` (incl. failure modes NOT simulated)
- [ ] Fidelity map covers every surface/call/data path — nothing unlabeled
- [ ] Data provenance stated + PII-scrubbed (`pii_scrubbed: true` with method; spot-check the dump)
- [ ] Every open decision has default + owner + what it blocks
- [ ] No secrets anywhere in the bundle; mock sites annotated; build-framework rails clean
- [ ] `HANDOFF.md` drafted: run steps work from zero on a clean machine

## Part 3 — pin, sign, seal

- [ ] Release pinned in the manifest: exact prototype commits (file hash for T1), Figma versions, `contracts/` versions
- [ ] **PM signs** — intent & behaviour · **Tech lead signs** — feasibility & production delta · **QA signs** — coverage & testability. Recorded in the manifest AND as GitHub PR reviews (CODEOWNERS on a protected branch — the GitHub identities are the binding copy)
- [ ] Run `bin/freeze.py <bundle>` → paste `release_id` / `frozen` / `digest` / `evidence_tag` into the manifest, set `prd.status: frozen`
- [ ] `bin/validate.py <bundle>` exits 0
- [ ] Push the protected tag it prints (`git tag -a prd/<slug>/r<N> …`) — that tag is the immutable release

> The cold-session's `open_defects: 0` and a green validator are **gates, not proof**. Sufficiency is the three humans' call — that is what the signatures mean.

| Check | By | Date |
|---|---|---|
| Cold-session passed (open_defects: 0) | | |
| DoD sweep clean | | |
| Release pinned | | |
| **G4 FROZEN — all three signatures** | | |

Refusal is normal and cheap here. Freezing a leaky bundle just moves the gap into the sprint, where it costs 10×.
