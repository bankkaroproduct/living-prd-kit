# The Living PRD — Standard v1.1

> **This is the strict reference for engineers and AI agents.** Humans: read `PROCESS.md` instead — same process, plain words.

**AI-driven product development: requirement → design → development → testing.**

| Owner | Status | Applies to | Last updated |
|---|---|---|---|
| Mohsin (Product) | v1.1 — frozen for pilots; with Rohan for review | Risk-gated — see §5 mandate | 2026-09-02 |

> **Golden rule: the PRD is not a document about the product. It is the product, one build early.**
> Tech receives a running prototype plus a spec bundle. If a behaviour isn't in the bundle, it isn't defined — add it before anyone builds it.

**v1.1 changelog:** merged the agreed operating model (Rohan + ChatGPT input, 2026-09-02): risk-triggered mandate; Alpha Review gate; three-signature immutable freeze; reuse/rebuild/reference-only classification with scenario IDs; Reconcile gate; Tool v1 scope + owner. Kept from v1: fidelity labels, cold-session test, mock contracts, visible events, PII rails.

**Contents:** [1 Why](#1-why) · [2 The contract](#2-the-contract--what-a-living-prd-is) · [3 Tiers](#3-tiers--how-much-prototype-to-build) · [4 Roles](#4-roles) · [5 Process & gates](#5-process--gates) · [6 Productionise & reconcile](#6-productionise--reconcile-g5g6) · [7 Living means living](#7-living-means-living) · [8 Tooling](#8-tooling) · [9 Rollout](#9-rollout) · [10 Decisions](#10-decisions--resolved-and-open)

---

## 1. Why

The static PRD has a fixed failure mode: every behaviour it doesn't define gets invented later — by a designer's guess, a developer's assumption, or a QA bug — and each invention costs a meeting. Meanwhile PMs and designers here already build with AI: near-production replicas on codebase forks with staging data, standalone simulations, single-file E2E mocks. The gap is that none of it is a *standard* — tech can't rely on what it gets, so the prototype ends up a demo, not a spec.

This standard closes that gap with a contract. The prototype becomes the PRD when it carries, demonstrably: every frontend validation, every technical and user edge case, every tracking event, a written contract for everything mocked, and the PM's decisions and notes — assembled so both a human and an AI agent can consume it without archaeology.

Two chronic failure modes die here: **undefined behaviour discovered mid-build**, and **"done" claims without evidence**.

---

## 2. The contract — what a Living PRD is

A Living PRD is a **bundle**: one running prototype + seven supporting artifacts, the first of which — the manifest — indexes the rest. Templates for all of these are in `living-prd-kit/`.

| # | Artifact | File | Must contain |
|---|---|---|---|
| 0 | Manifest | `prd.manifest.yaml` | Machine-readable index: feature id, tier, mandate check, prototype URL/repo, fidelity map, coverage plan, release pinning, changelog. **Downstream AI agents read this first.** |
| 1 | Prototype | repo / URL / file | Running, clickable, every coverage-plan scenario reachable (tier per §3) |
| 2 | Behaviour spec | `SPEC.md` | Screens with field tables (type, required, format, **validation + exact error copy**), state machine, error matrix, edge-case register with **scenario IDs** |
| 3 | Tracking plan | `TRACKING.md` | Every event: name, trigger, properties, surface — and every event **fires visibly in the prototype** (§8 collector) |
| 4 | Mock inventory | `MOCKS.md` | Every mocked dependency: why mocked, the mock's contract (request/response/failure modes), what the real integration must satisfy, where the mock lives |
| 5 | Decisions & notes | `DECISIONS.md` | Open decisions (default + owner + what it hits), PM inputs, requirements that fit nowhere else |
| 6 | Evidence | `EVIDENCE/` | Validation runs, test matrices, screenshots, cold-session transcript, "what to look for" pass/fail blocks |
| 7 | Handoff | `HANDOFF.md` | PM → Tech contract: fidelity map, reuse/rebuild/reference-only per layer, how to run, acceptance checks, the frozen release |

> **Fidelity labels — nothing unlabeled.** Every surface, API call, and data path in the prototype carries one of three labels in the manifest:
> **PROVEN** — runs against real or staging systems; behaviour is authoritative.
> **SIMULATED** — runs against a mock that honours a written contract in `MOCKS.md`; behaviour is authoritative, numbers may not be.
> **INDICATIVE** — visual only; do not trust values, copy, or data shapes.
> Tech plans from PROVEN, verifies SIMULATED contracts against the real service, and treats INDICATIVE as a sketch.

Why the manifest is YAML and strict: agents downstream don't infer intent — an agent without explicit grammar stalls or freelances (we learned this on the release-operator gating). The manifest is the grammar that lets tech's AI tooling ingest a PRD unattended.

---

## 3. Tiers — how much prototype to build

Tier changes **fidelity, not the contract** — the seven artifacts are mandatory at every tier. Varies by project and by who leads, exactly as it already does in practice.

| Tier | What it is | When | Leads | Data | Existing precedent |
|---|---|---|---|---|---|
| **T1 Mock** | Single-file or few-file HTML E2E mock, full flow clickable | UI/flow-heavy, no new business logic at risk | Designer (PM supports) | Hardcoded, labelled INDICATIVE | Axis Rewards E2E mock |
| **T2 Simulation** | Standalone app on the **slim scaffold** (design system + mock layer + event collector preinstalled); mocked APIs; synthetic data | New surface or new logic with no dependency on the existing codebase | PM or Designer | Synthetic / seeded, SIMULATED | IDFC Rewards+ variants, edu-loans |
| **T3 Replica** | The **sanctioned fork** of the production codebase (centrally maintained, regularly refreshed) + PII-scrubbed staging dump; real/staging APIs where cheap, mocks where expensive | Changes to existing product behaviour, data shapes, or API contracts | PM | Staging dump (dated, scrubbed), PROVEN/SIMULATED | `_fe-*` forks, cross-sell service |

**Decision rule: pick the lowest tier on which the riskiest assumption can fail.**

| The risk being tested | Tier |
|---|---|
| Layout, copy, flow comprehension, visual direction | T1 |
| A new flow's logic, states, validations end-to-end | T2 |
| Behaviour of the existing system: data shapes, API contracts, integration edge cases, migration | T3 |

Rules that hold at every tier: prototypes use the **production design system** (tokens + components), never generic AI styling; expensive integrations (SMS, payment, KYC, insurer APIs) are always mocked, never rebuilt; a T1 that starts sprouting logic gets promoted to T2 — don't fake logic in a mock. Vocabulary note: this standard counts **three** tiers; external drafts that say "T4" mean our T3.

---

## 4. Roles

| Role | Owns |
|---|---|
| **PM** | The bundle end-to-end: framing, tier call, spec, tracking, decisions. Accountable for the freeze (G4) passing. Signs **intent & behaviour** |
| **Designer** | Leads T1 builds and the UX layer of any tier; design-system compliance |
| **Tech lead** | G2 Alpha Review; signs **feasibility & production delta** at G4; reuse/rebuild/reference-only calls at G5; owns the real build |
| **QA** | Challenges states, validations, errors, retries, testability from G2 onward; signs **coverage & testability** at G4; owns G6 Reconcile |
| **AI (Planner / Builder / Reviewer)** | Fills the build-framework roles for constructing the prototype itself; continuous gap detection during G3 |

Building the prototype **is a build-framework job** — the [build-framework](https://github.com/bankkaroproduct/build-framework) loop (classify → plan + challenge → build → verify → ship behind a gate) and all its safety rails apply. Two rails get sharper here:

> **Staging dumps are Real, always.** A dump carrying customer data is classified Real under build-framework rules: scrub PII before it lands on any PM's server (names, phones, PAN, Aadhaar, addresses → synthetic), date the dump in the manifest, and never commit it. An Aadhaar number in a prototype repo is a production incident. For mandate-triggered projects, **no real data or live integrations before G2 Alpha Review**.
> **Mocks never ship.** Every mock is annotated `// MOCK — contract in MOCKS.md` at the code site, so nothing simulated can silently ride into the real build.

---

## 5. Process & gates

```
Frame → Solution → Alpha → ALPHA REVIEW → Complete bundle → FREEZE & APPROVE → Productionise → Reconcile → Ship
 G0        G1               G2                 G3                  G4                G5            G6
```

### When this process is mandatory

The full process is **required** when the work touches any of: regulated data or a compliance surface; **payments, KYC, or PII**; complex integrations or state machines; irreversible user or money actions; cross-team dependencies. Outside those triggers a PM may **opt out at G0** (recorded in the manifest) and build informally with the kit — a living PRD is still the default. An opted-out project that later trips a trigger re-enters at G2.

| Gate | Pass condition | Who signs |
|---|---|---|
| **G0** Frame | Problem, outcome, scope, context entered; tier call; **mandate check**; non-goals; the plan survived a devil's-advocate pass (right thing? simpler way? riskiest assumption first?) | PM |
| **G1** Solution | Flow map + `SPEC.md` skeleton: screens and states listed, edge-case register started, tracking plan drafted. Design review for UI-heavy work | PM + Designer |
| **G2** **Alpha Review** | The **Alpha** — one coherent working journey (mocks + synthetic data fine) with real/mocked/incomplete/undecided identified — reviewed by tech lead (feasibility, integrations, data boundaries, reuse potential) and QA (states, validations, errors, retries, testability). **They may block only on feasibility, safety, or testability.** Output: an **agreed coverage plan** with scenario IDs — not handoff approval | Tech lead + QA |
| **G3** Bundle complete | Every coverage-plan scenario demonstrable: every state reachable, every validation fires, every event visible in the collector; AI gap-detection pass clean (missing states, inconsistent rules, untracked actions, prototype/spec/API-contract drift) | PM (self-gate) |
| **G4** **Freeze & approve** | Definition of Done below + cold-session test passed → an **immutable Handoff Release** pinned to exact prototype commits, Figma versions, API contract versions, and test evidence. Three signatures: **PM** (intent & behaviour) · **Tech lead** (feasibility & production delta) · **QA** (coverage & testability). Changes after freeze create a new release | PM + Tech lead + QA |
| **G5** Productionise | See §6 | Tech lead |
| **G6** Reconcile | See §6 | QA + PM |

### The cold-session test — the freeze mechanism

A **fresh AI session with zero build context** is handed only the bundle and must:

1. answer behaviour questions correctly with citations — *"user enters an invalid PAN on screen 2: what happens, what's the error copy, what event fires?"*;
2. produce the acceptance checklist tech will build against;
3. list every question the bundle cannot answer.

Anything in list 3 is a bundle defect. **Fix the bundle, not the answer.** Continuous AI gap-detection (G3) checks *consistency*; only a zero-context session proves *sufficiency*. Transcript goes in `EVIDENCE/`.

### G4 Definition of Done

- [ ] Every coverage-plan scenario (G2) demonstrable in the prototype, by scenario ID
- [ ] Every user-visible state reachable — empty, loading, and error states included
- [ ] Every field validation fires in the prototype **and** appears in `SPEC.md` with exact error copy
- [ ] Every edge case in the register is demonstrable (trigger steps linked), or marked `NOT-DEMONSTRABLE` with a written rationale
- [ ] Every tracking event fires visibly in the collector and matches `TRACKING.md` 1:1 — names, triggers, properties
- [ ] Every external dependency is PROVEN, or mocked with its contract in `MOCKS.md`
- [ ] Data provenance stated in the manifest (staging dump + date / synthetic / hardcoded) and PII-scrubbed
- [ ] Every open decision has a default, an owner, and what it blocks
- [ ] Cold-session test passed; transcript in `EVIDENCE/`
- [ ] Release pinned: prototype commits, Figma versions, API contracts, evidence tag
- [ ] No secrets anywhere in the bundle; build-framework safety rails clean

---

## 6. Productionise & reconcile (G5–G6)

**G5 — Productionise.** The prototype is the **executable reference**; the spec is the **acceptance suite**. Per layer, the tech lead classifies:

| Layer | reuse (take the code) | rebuild (to spec) | reference-only |
|---|---|---|---|
| Frontend from a T3 fork | Often — already on our stack + design system | When the fork drifted from main | — |
| Frontend from T1/T2 | Rare | Default — behaviour-and-pixel matched | Visual direction only |
| Business logic | Case-by-case; Reviewer-audited first | Default | — |
| Mocks | **Never** | Always — build the real integration to `MOCKS.md` contracts | — |
| Tracking plan | Verbatim | — | — |

Production work stays tied to the **same scenario IDs** from the coverage plan — tickets, tests, and acceptance all reference them. **Material changes reopen the relevant approval** (a new release supersedes the frozen one); they never get settled in calls or chats.

QA writes test cases straight from the error matrix + edge-case register, and uses the prototype as the oracle: same input, same expected behaviour. Every test ask ships with a "what to look for" block — ✓ pass signs / ✗ fail signs per screen — no bare "test it".

**G6 — Reconcile (before release).** Compare the built behaviour against the frozen release, scenario ID by scenario ID: deviations are either fixed or explicitly approved and recorded against the release; the event audit is rerun on the real build (every `TRACKING.md` event fires with correct properties); test results attach to the release. Ship only from a reconciled release.

---

## 7. Living means living

The bundle stays the single source of truth **through the build**, not just at handoff.

| Rule | Detail |
|---|---|
| Changes land in the bundle first | Any behaviour change during development updates `SPEC.md` + the prototype + the manifest changelog **before** tech implements it |
| Material changes reopen approval | A frozen release is immutable — a material change produces a new release re-signed by whoever's domain it touches |
| Disagreement is a same-day fix | Prototype and spec disagree → whichever is wrong gets fixed that day; the manifest changelog records which won and why |
| Decisions close in writing | An open decision resolving flips its row in `DECISIONS.md` from default → decided (owner + date), and the affected spec sections update in the same commit |
| Retirement | The bundle archives with the release. The living PRD for v1 becomes the starting spec for v1.1 |

---

## 8. Tooling

| Tool | Job |
|---|---|
| `living-prd-kit/` | Templates for the manifest and every supporting artifact + gate checklists. **Runs the whole process manually today**: manifest = the structured inventories, checklists = approvals, git tag = immutable release |
| build-framework | Governs the construction of the prototype itself (roles, loop, rails) |
| Event collector pattern | ~40 lines of JS: on-screen event panel + `window.__events`. Drops into every tier including single-file mocks — this is how "events fire visibly" stays cheap |
| Mock pattern | Standard shape for mocking expensive services (SMS, payment, KYC): visible mock banner, logged calls, contract file cross-linked |

### Tool v1 — the internal app

CashKaro-owned. **Built by the PM team as pilot #3, under this standard** — the tool's own living PRD, with an engineering owner named at its G2 Alpha Review and engineering taking it at G4/G5. Build the MVP against the pain the first two pilots actually surface, not the full wishlist.

| Cut | Features |
|---|---|
| **MVP** | Guided intake + risk classification (mandate check) · preferred scaffold per tier · manifest/scenario viewer with executable prototype preview · tech-lead + QA review inbox · immutable three-signature release with GitHub commit pinning · test-run evidence |
| **Later** (post-pilot evidence) | Figma snapshot integration · OpenAPI/API contract import · version comparison · continuous AI gap/inconsistency detection as a service · portable export/API for engineering and AI agents |
| **Non-goals** | Not an IDE, not a Jira replacement, not a deployment platform, not a database-cloning service, not an automatic production-code approver |

---

## 9. Rollout

| Step | What |
|---|---|
| 1 | Name the two pilots from the current roadmap (owner: Mohsin + Rohan): **Pilot A** — UI-heavy, designer-led, T1→T2. **Pilot B** — integration/state-machine work that **trips a mandatory trigger** (payments/KYC/PII), so the process is tested where it's mandated |
| 2 | Run both on the kit — manual gates, no tool |
| 3 | **Pilot #3 = Tool v1**, built by the PM team under the standard itself |
| 4 | Retro per pilot: what Alpha Review and the cold-session test caught, what tech still had to ask, bundle time-cost vs. saved meetings → revise to v1.2, then team-wide |
| 5 | Fold the kit into build-framework as `templates/prd/` once stable |

---

## 10. Decisions — resolved and open

**Resolved (2026-09-02):**

| # | Decision | Outcome |
|---|---|---|
| R1 | Process model | Merged operating model: risk-triggered mandate, Alpha Review, three-signature immutable freeze, reuse/rebuild/reference-only + scenario IDs, Reconcile |
| R2 | Preferred scaffold | Tiered: slim standalone harness (design system + mock layer + collector preinstalled) for T2; one centrally-maintained, regularly-refreshed **sanctioned fork** with a PII-scrubbed dump pipeline for T3 |
| R3 | Tool v1 owner | PM team builds it as pilot #3 under this standard; engineering owner named at its Alpha Review |
| R4 | Tier count | Three tiers; external "T4" = our T3 |

**Open (defaults; flip in review, not mid-build):**

| # | Question | Default |
|---|---|---|
| 1 | Where do bundles live? | One repo/folder per feature, named `prd-<feature>`, linked from the tracker |
| 2 | Who runs the cold-session test? | Tech-side AI session, not the PM's — cleaner separation |
| 3 | Backend-only work (no UI) | Same contract; "prototype" = runnable service + seeded calls; T3 by default |
| 4 | Design-system pack maintainer for the slim scaffold | Design team; generic AI styling fails G1 |
| 5 | Pilot A and Pilot B names | From roadmap — Mohsin + Rohan to name |
