# The Living PRD — Standard v1.2

> **This is the strict reference for engineers and AI agents.** Humans: read `PROCESS.md` instead — same process, plain words.

**AI-driven product development: requirement → design → development → testing.**

| Owner | Status | Applies to | Last updated |
|---|---|---|---|
| Mohsin (Product) | v1.2 — hardened after external AI review; pilots next | Risk-gated — see §5 mandate | 2026-09-02 |

> **Golden rule: the PRD is not a document about the product. It is the product, one build early.**
> Tech receives a running prototype plus a spec bundle. If a behaviour isn't in the bundle, it isn't defined — add it before anyone builds it.

**v1.2 changelog:** hardened per external AI review of the repo (2026-09-02): manifest is now valid YAML with a formal JSON Schema (`schema/`); scaffolder, validator and freeze scripts (`bin/`); CI + CODEOWNERS-enforced approvals + protected `prd/**` release tags; release identity (ID + digest); `contracts/` area with pinned API truth; demonstrability rules for `NOT-DEMONSTRABLE`; synthetic-data default with gated staging dumps; cold-session test clarified as required-not-sufficient; "tech builds against it" wording; T1-lite package; decision provenance in §10.
**v1.2 hardening pass 2 (2026-09-03, second external AI review):** fidelity labels renamed `PROVEN/SIMULATED/INDICATIVE` → `REAL/MOCKED/STATIC` (schema-breaking, no bundle had used the old names yet); validator now fails closed instead of warning (null manifest, missing/broken jsonschema, PII/secret hits are all hard errors now, not warnings); validator scans `.sql`/`.dump` too; validator checks all seven gates at an advanced status, not just G4, and independently recomputes the freeze digest and checks commit/tag references against git instead of trusting the manifest's word; `bin/freeze.py` now runs the validator first, checks G0–G3 and all three approval keys, hashes the bundle's actual file bytes into the digest (not just manifest strings), and auto-increments the release number; schema now requires `pinned_versions`, pins `standard_version`, and requires a real `links.spec` on a `demonstrable` scenario; `prd/*` corrected to `prd/**` everywhere (tags nest two levels); README's T1-lite/seven-artifacts contradiction fixed; G4 checklist's `pii_scrubbed` line scoped to staging-dump bundles only; STATIC's definition split "copy is real and reviewable" from "values aren't computed"; T2/T3 scaffold language in §3 and R2 below softened from mandatory to preferred, matching §8's MVP wording and Mohsin's own Feedback Form pilot (built by reusing an existing app, not the slim scaffold).
**v1.1 changelog:** merged the agreed operating model (Rohan + ChatGPT input): risk-triggered mandate; Alpha Review gate; three-signature immutable freeze; reuse/rebuild/reference-only + scenario IDs; Reconcile gate; Tool v1 scope + owner.

**Contents:** [1 Why](#1-why) · [2 The contract](#2-the-contract--what-a-living-prd-is) · [3 Tiers](#3-tiers--how-much-prototype-to-build) · [4 Roles](#4-roles) · [5 Process & gates](#5-process--gates) · [6 Productionise & reconcile](#6-productionise--reconcile-g5g6) · [7 Living means living](#7-living-means-living) · [8 Tooling](#8-tooling) · [9 Rollout](#9-rollout) · [10 Decisions](#10-decisions--resolved-and-open)

---

## 1. Why

The static PRD has a fixed failure mode: every behaviour it doesn't define gets invented later — by a designer's guess, a developer's assumption, or a QA bug — and each invention costs a meeting. Meanwhile PMs and designers here already build with AI: near-production replicas on codebase forks with staging data, standalone simulations, single-file E2E mocks. The gap is that none of it is a *standard* — tech can't rely on what it gets, so the prototype ends up a demo, not a spec.

This standard closes that gap with a contract. The prototype becomes the PRD when it carries, demonstrably: every frontend validation, every technical and user edge case, every tracking event, a written contract for everything mocked, and the PM's decisions and notes — assembled so both a human and an AI agent can consume it without archaeology.

Two chronic failure modes die here: **undefined behaviour discovered mid-build**, and **"done" claims without evidence**.

---

## 2. The contract — what a Living PRD is

A Living PRD is a **bundle**: one running prototype + eight supporting artifacts, the first of which — the manifest — indexes the rest. `bin/new-prd.sh <slug>` generates the whole structure; `bin/validate.py <bundle>` enforces it (schema in `schema/prd.manifest.schema.json`).

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
| 8 | API contracts | `contracts/` | Pinned OpenAPI/JSON Schema + fixtures, exact versions. **For API shapes and integration behaviour, these outrank `SPEC.md` and any PM-written assumption**; for product behaviour, `SPEC.md` outranks everything. A conflict between the two is a same-day-fix defect |

> **Fidelity labels — nothing unlabeled.** Every surface, API call, and data path in the prototype carries one of three labels in the manifest:
> **REAL** — runs against real or staging systems; behaviour is authoritative.
> **MOCKED** — runs against a mock that honours a written contract in `MOCKS.md`; behaviour is authoritative, numbers may not be.
> **STATIC** — nothing behind it is computed or wired to a real system; do not trust any value, number, or data shape it shows.
> The literal copy and layout of a STATIC surface is real work and the thing under review (that's what T1 is for) — the label only says the *values* aren't live. Tech plans from REAL, verifies MOCKED contracts against the real service, and treats STATIC copy as approved wording but STATIC data as a sketch.

Why the manifest is YAML and strict: agents downstream don't infer intent — an agent without explicit grammar stalls or freelances (we learned this on the release-operator gating). The manifest is the grammar that lets tech's AI tooling ingest a PRD unattended.

---

## 3. Tiers — how much prototype to build

Tier changes **fidelity, not the contract** — the bundle is mandatory at every tier, with one relief: **T1-lite**. A T1 mock with no logic, no integrations and nothing faked-with-behaviour may ship a reduced bundle — manifest, `SPEC.md` (screens, states, copy), `TRACKING.md`, and `EVIDENCE/` — dropping `MOCKS.md` and `contracts/` (nothing to contract). The validator accepts this only for `tier: T1`.

| Tier | What it is | When | Leads | Data | Existing precedent |
|---|---|---|---|---|---|
| **T1 Mock** | Single-file or few-file HTML E2E mock, full flow clickable | UI/flow-heavy, no new business logic at risk | Designer (PM supports) | Hardcoded, labelled STATIC | Axis Rewards E2E mock |
| **T2 Simulation** | Standalone app; mocked APIs; synthetic data. **Preferred** shape is the **slim scaffold** (design system + mock layer + event collector preinstalled) — but reusing an existing standalone build you already made is fine too, if it's noted in `pinned_versions` (`slim_scaffold: "n/a — reusing <what>"`) | New surface or new logic with no dependency on the existing codebase | PM or Designer | Synthetic / seeded, MOCKED | IDFC Rewards+ variants, edu-loans, Feedback Form pilot |
| **T3 Replica** | A fork of the production codebase (**preferred: the centrally-maintained sanctioned fork**, kept fresh) + PII-scrubbed staging dump; real/staging APIs where cheap, mocks where expensive | Changes to existing product behaviour, data shapes, or API contracts | PM | Staging dump (dated, scrubbed), REAL/MOCKED | `_fe-*` forks, cross-sell service |

"Preferred" is doing real work in that table: it's a default you can depart from with a one-line reason in the manifest, not a hard gate the validator enforces. See R2/R6 in §10 — this wording was tightened from an earlier draft that read as mandatory when the actual decision was a preference.

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

> **Synthetic data is the default, everywhere.** A staging dump is the exception and it is **gated**: allowed only through the documented scrub pipeline, with `scrub_pipeline`, `dump_date` and `pii_scrubbed: true` attested in the manifest — the schema rejects a staging-dump bundle without all three, and `pii_scrubbed` has no default. Scrubbed means names, phones, PAN, Aadhaar, addresses → synthetic. Dumps are never committed (`.gitignore` blocks `*.sql`/`*.dump`), the validator scans every bundle for PII and secret patterns **and fails the build on a hit** (`.sql`/`.dump` files included, not just docs and code), and dumps are deleted when the bundle archives. An Aadhaar number in a prototype repo is a production incident. For mandate-triggered projects, **no real data or live integrations before G2 Alpha Review**.
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
| **G3** Bundle complete | Every coverage-plan scenario demonstrable **or** `NOT-DEMONSTRABLE` per §5's rules below: every reachable state hits, every validation fires, every event visible in the collector; AI gap-detection pass clean (missing states, inconsistent rules, untracked actions, prototype/spec/API-contract drift) | PM (self-gate) |
| **G4** **Freeze & approve** | Definition of Done below + cold-session test passed → an **immutable Handoff Release**: `release_id` + `sha256` digest over the pinned refs (`bin/freeze.py`), exact prototype commits, Figma versions, `contracts/` versions and test evidence pinned, sealed with a protected `prd/<slug>/r<N>` git tag. Three signatures — **PM** (intent & behaviour) · **Tech lead** (feasibility & production delta) · **QA** (coverage & testability) — recorded in the manifest **and enforced as GitHub reviews via CODEOWNERS on a protected branch** (GitHub identities are the binding copy). Changes after freeze create a new release | PM + Tech lead + QA |
| **G5** Productionise | See §6 | Tech lead |
| **G6** Reconcile | See §6 | QA + PM |

### The cold-session test — the freeze mechanism

A **fresh AI session with zero build context** is handed only the bundle and must:

1. answer behaviour questions correctly with citations — *"user enters an invalid PAN on screen 2: what happens, what's the error copy, what event fires?"*;
2. produce the acceptance checklist tech will build against;
3. list every question the bundle cannot answer.

Anything in list 3 is a bundle defect. **Fix the bundle, not the answer.** Continuous AI gap-detection (G3) checks *consistency*; the cold session is the adversarial *completeness* probe. Both are **required and neither is sufficient**: `open_defects: 0` from an AI is a gate you must pass, never proof that requirements are complete — final sufficiency is the human PM + Tech lead + QA decision expressed in the three signatures. Transcript goes in `EVIDENCE/`.

### Demonstrability rules

Default: every coverage-plan scenario is **demonstrable** — trigger steps reproduce it in the prototype. `NOT-DEMONSTRABLE` is allowed only for three categories: **(a)** behaviour that executes on a third party's side (their retries, their settlement, their UW queue); **(b)** long-horizon or time-based behaviour impractical to simulate honestly (30-day expiry, quarterly refresh); **(c)** race/concurrency conditions with no deterministic trigger. Each such scenario must still carry, in the bundle: the expected behaviour written in `SPEC.md`, the reason (in the manifest's `not_demonstrable_reason` — schema-enforced), a named production test in `HANDOFF.md`'s known-gaps, and it is built and tested in production like any other scenario — undemonstrable never means optional. Anything outside (a)–(c) that can't be demonstrated means the prototype is at the wrong tier: promote it.

### G4 Definition of Done

- [ ] Every coverage-plan scenario (G2) demonstrable in the prototype, by scenario ID
- [ ] Every user-visible state reachable — empty, loading, and error states included
- [ ] Every field validation fires in the prototype **and** appears in `SPEC.md` with exact error copy
- [ ] Every edge case in the register is demonstrable (trigger steps linked), or marked `NOT-DEMONSTRABLE` with a written rationale
- [ ] Every tracking event fires visibly in the collector and matches `TRACKING.md` 1:1 — names, triggers, properties
- [ ] Every external dependency is REAL, or mocked with its contract in `MOCKS.md`
- [ ] Data provenance stated in the manifest (staging dump + date / synthetic / hardcoded) and PII-scrubbed
- [ ] Every open decision has a default, an owner, and what it blocks
- [ ] Cold-session test passed (`open_defects: 0`); transcript in `EVIDENCE/` — required, not sufficient
- [ ] `NOT-DEMONSTRABLE` scenarios all fall in categories (a)–(c) above, each with reason + production test named
- [ ] `contracts/` pinned: exact OpenAPI/schema versions listed in `release.api_contracts`
- [ ] Release identity: `release_id` + digest from `bin/freeze.py`; protected `prd/**` tag pushed
- [ ] `bin/validate.py <bundle>` exits 0
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
| `bin/new-prd.sh <slug>` | Generates a complete bundle: manifest (slug filled), all templates, checklists, patterns, `contracts/`, `EVIDENCE/`, `.gitignore` |
| `bin/validate.py <bundle>` | Enforces the standard: YAML + JSON Schema, unfilled placeholders, artifact existence, scenario/event/mock/evidence cross-references, gate consistency, PII + secret scan. Runs in CI (`.github/workflows/validate.yml`) on every push |
| `bin/freeze.py <bundle>` | Computes the release identity — `release_id` + sha256 digest over pinned commits/Figma/contracts/approvals — and prints the protected-tag command |
| `.github/CODEOWNERS` | Binds the three signatures to real GitHub identities via required reviews on a protected branch; `prd/**` tags protected in repo settings |
| `.github/workflows/three-role-approval.yml` | CODEOWNERS alone only guarantees one approval from *someone* on the list — this reads the three handles off CODEOWNERS' `prd.manifest.yaml` line and fails the PR check unless each has individually approved |
| `tests/run_tests.sh` | Regression tests for `validate.py`/`freeze.py` themselves: a deliberately-broken fixture must fail with the right errors, a genuinely complete one must pass clean. Runs in CI on every push |
| build-framework | Governs the construction of the prototype itself (roles, loop, rails) — **pin the ref used** in `pinned_versions` |
| Event collector pattern | ~40 lines of JS: on-screen event panel + `window.__events`. Drops into every tier including single-file mocks — this is how "events fire visibly" stays cheap |
| Mock pattern | Standard shape for mocking expensive services (SMS, payment, KYC): visible mock banner, logged calls, contract file cross-linked |

### Tool v1 — the internal app

CashKaro-owned. **Built by the PM team as pilot #3, under this standard** — the tool's own living PRD, with an engineering owner named at its G2 Alpha Review and engineering taking it at G4/G5. Build the MVP against the pain the first two pilots actually surface, not the full wishlist.

| Cut | Features |
|---|---|
| **MVP** | Guided intake + risk classification (mandate check) · preferred scaffold per tier · manifest/scenario viewer with executable prototype preview · tech-lead + QA review inbox · immutable three-signature release with GitHub commit pinning · **manual Figma-version and API-contract pin fields** (the manifest already carries them) · test-run evidence |
| **Later** (post-pilot evidence) | **Automated** Figma snapshot integration · OpenAPI/API contract import · version comparison · continuous AI gap/inconsistency detection as a service · portable export/API for engineering and AI agents |
| **Non-goals** | Not an IDE, not a Jira replacement, not a deployment platform, not a database-cloning service, not an automatic production-code approver |

---

## 9. Rollout

| Step | What |
|---|---|
| 0 | **Retrospective reference bundle**: retrofit one already-shipped feature (GroupCare360 is the natural pick — its PRD grammar is the house standard) into a complete bundle. It becomes the worked example every PM copies from, and the first real test of the templates + validator |
| 1 | Name the two pilots from the current roadmap (owner: Mohsin + Rohan): **Pilot A** — UI-heavy, designer-led, T1→T2. **Pilot B** — integration/state-machine work that **trips a mandatory trigger** (payments/KYC/PII), so the process is tested where it's mandated |
| 2 | Run both on the kit — manual gates, no tool. **Measure**: preparation effort, handoff time, clarification loops, requirement-driven rework, missing states found after handoff |
| 3 | **Pilot #3 = Tool v1**, built by the PM team under the standard itself |
| 4 | Retro per pilot → revise to v1.3, then team-wide |
| 5 | Fold the kit into build-framework as `templates/prd/` once stable |

---

## 10. Decisions — resolved and open

**Resolved** — provenance recorded so reviewers know who decided, not who drafted:

| # | Decision | Outcome | Decided by |
|---|---|---|---|
| R1 | Process model | Merged operating model: risk-triggered mandate, Alpha Review, three-signature immutable freeze, reuse/rebuild/reference-only + scenario IDs, Reconcile | Mohsin, 2026-09-02 (option round; ChatGPT/Rohan input merged) |
| R2 | Preferred scaffold | Tiered *preference*, not a mandate: slim standalone harness (design system + mock layer + collector preinstalled) for T2; one centrally-maintained, regularly-refreshed sanctioned fork for T3. Reusing an existing build is an accepted alternative (noted in `pinned_versions`) | Mohsin, 2026-09-02 (option round) — reworded 2026-09-03, see Open #6 |
| R3 | Tool v1 owner | PM team builds it as pilot #3 under this standard; engineering owner named at its Alpha Review | Mohsin, 2026-09-02 (option round; reaffirmed after external review) |
| R4 | Tier count | Three tiers; external "T4" = our T3 | Mohsin, 2026-09-02 |
| R5 | Data policy | Synthetic default; staging dumps gated behind a documented scrub pipeline attested in the manifest; `pii_scrubbed` has no default | Mohsin, 2026-09-02 (after external review) |
| R6 | Tool v1 pinning scope | Manual Figma/API version pins in MVP; automated integrations post-pilots | Mohsin, 2026-09-02 (after external review) |
| R7 | Validation plan | Retrospective reference bundle first, then two live pilots with measurement, Tool v1 as pilot #3 | Mohsin, 2026-09-02 (after external review) |

**Open (defaults; flip in review, not mid-build):**

| # | Question | Default |
|---|---|---|
| 1 | Where do bundles live? | One repo/folder per feature, named `prd-<feature>`, linked from the tracker |
| 2 | Who runs the cold-session test? | Tech-side AI session, not the PM's — cleaner separation |
| 3 | Backend-only work (no UI) | Same contract; "prototype" = runnable service + seeded calls; T3 by default |
| 4 | Design-system pack maintainer for the slim scaffold | Design team; generic AI styling fails G1 |
| 5 | Pilot A and Pilot B names | From roadmap — Mohsin + Rohan to name |
| 6 | Does R2's "preferred scaffold" and R6's "manual pins in MVP" fully match what was actually chosen — "preferred scaffold plus external imports" and GitHub/Figma/API/test-runs as v1 connections? | R2's wording above was already loosened from a mandatory reading on 2026-09-03 (second external review). R6 is unchanged pending Mohsin confirming whether v1 needs live Figma/API ingestion or manual pins are still right |
