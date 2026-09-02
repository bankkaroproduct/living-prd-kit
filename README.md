# living-prd-kit (standard v1.1)

**Humans: read [`PROCESS.md`](./PROCESS.md)** — the whole process in plain words, 3 minutes. **Engineers and AI agents: [`LIVING_PRD_STANDARD.md`](./LIVING_PRD_STANDARD.md)** is the strict spec behind it. This kit is what you copy into a feature; it runs the whole process manually until Tool v1 exists. Gates: G0 Frame → G1 Solution → **G2 Alpha Review** → G3 Complete → **G4 Freeze (3 signatures)** → G5 Productionise → **G6 Reconcile**.

## Use

```
# in your prototype repo / feature folder
cp -r living-prd-kit/templates/* .
cp living-prd-kit/prd.manifest.yaml .
```

Then, as the prototype builds (under build-framework rules), fill the bundle **as you go — not at the end**:

| When | Fill |
|---|---|
| G0 Frame | `prd.manifest.yaml` header + **mandate check** (risk triggers), `DECISIONS.md` first rows |
| G1 Solution | `SPEC.md` skeleton (screens, states, edge-case register), `TRACKING.md` draft |
| G2 Alpha Review | One coherent journey → tech + QA review (`checklists/G2_ALPHA_REVIEW.md`) → **coverage plan** with scenario IDs into the manifest |
| During build | Validations + error copy into `SPEC.md` the day they're built; every mock into `MOCKS.md` the day it's written; events wired through the collector (`patterns/event-collector.md`); AI gap-detection passes |
| G3 | Every coverage-plan scenario demonstrable; `EVIDENCE/` runs; self-check against the DoD |
| G4 Freeze | Cold-session test + DoD sweep (`checklists/G4_FREEZE.md`), `HANDOFF.md`, **pinned release, three signatures** (PM + Tech + QA) |
| G6 Reconcile | Built behaviour vs. frozen release (`checklists/G6_RECONCILE.md`) before ship |

## Tier call (make it at G0)

**Pick the lowest tier on which the riskiest assumption can fail.**

| Risk under test | Tier | Shape |
|---|---|---|
| Layout, copy, flow comprehension | **T1 Mock** | Single-file HTML E2E mock, design-led, data INDICATIVE |
| New flow's logic, states, validations | **T2 Simulation** | Standalone app, mocked APIs, synthetic data |
| Existing system behaviour, data shapes, API contracts | **T3 Replica** | Codebase fork + scrubbed staging dump; mocks only where expensive |

Tier changes fidelity, **not** the contract — all seven artifacts are mandatory at every tier. A T1 that starts sprouting logic gets promoted to T2.

## The bundle

```
prd-<feature>/
  prd.manifest.yaml     ← agents read this first
  SPEC.md               ← behaviour: screens, validations + error copy, states, error matrix, edge cases
  TRACKING.md           ← every event; all fire visibly in the prototype
  MOCKS.md              ← contract for everything simulated
  DECISIONS.md          ← open decisions (default + owner) + PM notes
  HANDOFF.md            ← PM → Tech contract
  EVIDENCE/             ← validation runs, matrices, cold-session transcript
  <the prototype>
```

## Rules that save you later

- **Nothing unlabeled**: every surface/call/data path is PROVEN, SIMULATED, or INDICATIVE in the manifest.
- **Staging dumps are Real**: scrub PII before the dump leaves staging; date it in the manifest; never commit it.
- **Mocks never ship**: annotate `// MOCK — contract in MOCKS.md` at every mock site.
- **If it isn't in the bundle, it isn't defined.**
