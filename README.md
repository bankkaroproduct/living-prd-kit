# living-prd-kit (standard v1.2)

**Humans: read [`PROCESS.md`](./PROCESS.md)** — the whole process in plain words, 3 minutes. **Engineers and AI agents: [`LIVING_PRD_STANDARD.md`](./LIVING_PRD_STANDARD.md)** is the strict spec behind it. This kit is what you copy into a feature; it runs the whole process manually until Tool v1 exists. Gates: G0 Frame → G1 Solution → **G2 Alpha Review** → G3 Complete → **G4 Freeze (3 signatures)** → G5 Productionise → **G6 Reconcile**.

## Use

```
# generate a complete bundle (manifest + all templates + checklists + patterns + contracts/ + EVIDENCE/)
bin/new-prd.sh <feature-slug> [target-dir]

# enforce the standard at any point (also runs in CI on every push)
bin/validate.py prd-<feature-slug>

# at G4: compute the release identity (ID + digest) and the protected-tag command
bin/freeze.py prd-<feature-slug>

# prove the validator/freeze scripts actually catch what they claim to (CI runs this too)
tests/run_tests.sh
```

The schema behind the manifest is `schema/prd.manifest.schema.json`; `.github/CODEOWNERS` binds the three freeze signatures to real GitHub reviews (set branch protection + protect the `prd/**` tag pattern in repo settings — `**` matters, releases tag as `prd/<slug>/r<N>`, two levels deep). **T1-lite:** a pure-visual T1 mock may drop `MOCKS.md` and `contracts/` — everything else stays.

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
| Layout, copy, flow comprehension | **T1 Mock** | Single-file HTML E2E mock, design-led, data STATIC |
| New flow's logic, states, validations | **T2 Simulation** | Standalone app, mocked APIs, synthetic data |
| Existing system behaviour, data shapes, API contracts | **T3 Replica** | Codebase fork + scrubbed staging dump; mocks only where expensive |

Tier changes fidelity, **not** the contract — the bundle is mandatory at every tier, with one relief: **T1-lite** (above) drops `MOCKS.md` and `contracts/` for a pure-visual T1 with nothing faked-with-behaviour. A T1 that starts sprouting logic gets promoted to T2.

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

- **Nothing unlabeled**: every surface/call/data path is REAL, MOCKED, or STATIC in the manifest.
- **Staging dumps are Real**: scrub PII before the dump leaves staging; date it in the manifest; never commit it.
- **Mocks never ship**: annotate `// MOCK — contract in MOCKS.md` at every mock site.
- **If it isn't in the bundle, it isn't defined.**
