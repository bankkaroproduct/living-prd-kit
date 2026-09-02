# G6 — Reconcile checklist (before release)

Compares the **built product** against the **frozen Handoff Release**, scenario ID by scenario ID. Owned by QA with the PM; ship only from a reconciled release.

## Sweep

- [ ] Every coverage-plan scenario executed against the real build — same input, same expected behaviour as the prototype (the oracle)
- [ ] Deviations: each one either **fixed**, or **explicitly approved** and recorded against the release (what changed, why, approved by whom) — nothing settled in calls or chats
- [ ] Event audit rerun on the real build: every `TRACKING.md` event fires with correct names, triggers, properties — into the real analytics pipeline this time
- [ ] Every mock replaced: real integrations verified against their `MOCKS.md` contracts, including the failure modes the mock did NOT simulate
- [ ] `NOT-DEMONSTRABLE` edge cases from the register: built and tested (they were never optional — only undemonstrable in the prototype)
- [ ] Test results attached to the release (`release.evidence_tag`)
- [ ] Any material change during the build produced a superseding release with re-signed approval — check the manifest changelog for orphaned changes

## Output

| Check | By | Date |
|---|---|---|
| All scenarios reconciled (fixed or approved-deviation) | | |
| Analytics verified on real build | | |
| **G6 RECONCILED — clear to ship** | QA + PM | |
