# HANDOFF — <feature> (PM → Tech)

> Filled by the PM at G4 freeze, signed three ways (PM, Tech lead, QA). The prose restates the YAML for humans; **if the two disagree, the YAML wins.** Tech's AI agents read `prd.manifest.yaml` + this block before anything else.

```yaml
handoff:
  prd_id: <feature-slug>
  bundle_root: <repo/folder>
  release: <release id from prd.manifest.yaml>   # the immutable, pinned Handoff Release this handoff ships
  approvals:
    pm_intent_behaviour: { by: <pm>, date: <date> }
    tech_feasibility_delta: { by: <tech-lead>, date: <date> }
    qa_coverage_testability: { by: <qa>, date: <date> }
  build_mode_per_layer:            # reuse = take the code | rebuild = to spec | reference-only = look, don't lift
    frontend: reuse | rebuild | reference-only
    business_logic: reuse | rebuild | reference-only
    mocks: rebuild                 # always — mocks never ship; build real integrations to MOCKS.md contracts
    tracking_plan: reuse           # verbatim from TRACKING.md
  scenario_ids: coverage_plan      # production tickets/tests reference the manifest's scenario IDs, unchanged
  authoritative_order:             # when sources disagree mid-build
    - contracts/ (API shapes + integration behaviour ONLY — pinned versions outrank PM-written assumptions)
    - SPEC.md (product behaviour — what the user sees and can do)
    - prototype (PROVEN paths)
    - prototype (SIMULATED paths)
    - anything INDICATIVE          # never authoritative
  release_digest: "<sha256:... from prd.manifest.yaml — tech verifies it matches the prd/* tag before building>"
  requires_reopen:                 # material changes reopen the relevant approval — a NEW release, never a call/chat
    - changing any behaviour defined in SPEC.md
    - dropping/renaming a tracking event
    - resolving an open decision without its owner
```

## How to run the prototype
<Exact steps from zero: clone/open → env (names only, no secrets) → seed → run → the demo path. A cold session must succeed from these lines alone.>

## What to trust (fidelity summary)
<Three short lists from the manifest fidelity map: PROVEN paths · SIMULATED paths (with their mock IDs) · INDICATIVE paths. One line each.>

## What's proven vs. indicative — the numbers
<Where the prototype's displayed values are real (staging data, dated) vs. illustrative. Stops tech from reverse-engineering fake numbers.>

## Acceptance checks (lifted into tech's plan)
- [ ] <observable behaviour 1 — same grammar as SPEC's validations/error matrix>
- [ ] Empty, loading, and error states for every new surface
- [ ] Every event in TRACKING.md fires with correct properties in tech's build

## What to look for when testing
Per surface: ✓ pass signs (what should happen) / ✗ fail signs (what to flag, on which screen). No bare "test it".

| Surface | ✓ Pass | ✗ Fail |
|---|---|---|
| | | |

## Known gaps & corners cut
<What the prototype deliberately doesn't do; risks tech inherits; NOT-DEMONSTRABLE edge cases (from the register) that tech must still build and QA must still test.>
