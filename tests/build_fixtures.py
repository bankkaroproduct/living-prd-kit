#!/usr/bin/env python3
"""Builds the two regression fixtures used by tests/run_tests.sh.

complete-bundle/  — everything genuinely filled in, real cross-refs, inside
                    its own tiny git repo with a real commit + tag. Must
                    validate clean (0 errors).
broken-bundle/    — deliberately hits every false-green case the second
                    external review found. Must fail with several errors.

Run once from the kit root: python3 tests/build_fixtures.py
"""
import subprocess, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "bin"))
import _release

FIX = KIT / "tests" / "fixtures"
COMPLETE = FIX / "complete-bundle"
BROKEN = FIX / "broken-bundle"


def w(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def build_complete():
    for p in COMPLETE.rglob("*"):
        if p.is_file(): p.unlink()

    w(COMPLETE / "SPEC.md", "# SPEC\n\n## S1 — user submits form\nField `email` required, format check. "
      "On submit: success screen shown.\n")
    w(COMPLETE / "TRACKING.md", "# TRACKING\n\n- `form_submitted` — fires on successful submit. Properties: `email_domain`.\n")
    w(COMPLETE / "MOCKS.md", "# MOCKS\n\n## M1 — submit endpoint\nRequest: `{email}`. Response: `{ok:true}`. "
      "Failure mode not simulated: rate limiting.\n")
    w(COMPLETE / "DECISIONS.md", "# DECISIONS\n\nNone open — all resolved before freeze.\n")
    w(COMPLETE / "HANDOFF.md", "# HANDOFF\n\nfrontend: rebuild\nbusiness_logic: rebuild\nmocks: rebuild\n"
      "tracking_plan: reuse\n\nRun: `npm start` from a clean checkout.\n")
    w(COMPLETE / "contracts" / "submit.schema.json", '{"type":"object","properties":{"email":{"type":"string"}}}\n')
    w(COMPLETE / "EVIDENCE" / "run1.md", "# Evidence run 1\nAll scenarios passed manual test 2026-09-01.\n")
    w(COMPLETE / "EVIDENCE" / "cold-session-2026-09-01.md", "# Cold session 2026-09-01\nQ: what happens on invalid "
      "email? A: inline error, cites SPEC.md#s1. 0 unanswered questions.\n")

    # git init + one real commit so prototype_commits / evidence_tag can be verified for real
    subprocess.run(["git", "init", "-q"], cwd=COMPLETE, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=COMPLETE, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture Builder"], cwd=COMPLETE, check=True)

    manifest_stub = """prd:
  id: "demo-feature"
  title: "Demo Feature"
  owner: "Alex PM"
  tech_lead: "Sam Tech"
  qa: "Jo QA"
  designer: "none"
  tier: T2
  status: shipped
  standard_version: "1.2"

pinned_versions:
  build_framework: "n/a — fixture"
  slim_scaffold: "n/a — reused existing app"
  sanctioned_fork: "n/a"

mandate:
  mandatory: false
  triggers: []
  opt_out: "2026-09-01, Alex PM"

prototype:
  kind: standalone-app
  entry: "https://example.test/demo"
  run: "npm start"
  base: "n/a"

data:
  provenance: synthetic
  dump_date: "n/a"
  scrub_pipeline: "n/a"
  pii_scrubbed: null

fidelity:
  - path: "whole flow"
    label: MOCKED
    note: "demo fixture"

coverage_plan:
  agreed_at: "2026-09-01"
  scenarios:
    - id: S1
      name: "user submits form"
      status: demonstrable
      not_demonstrable_reason: "n/a"
      links: { spec: "SPEC.md#s1", events: ["form_submitted"], mocks: ["M1"], evidence: "EVIDENCE/run1.md", production_ref: "pending", reconcile: "pending" }

artifacts:
  spec: { file: SPEC.md, status: complete }
  tracking: { file: TRACKING.md, status: complete }
  mocks: { file: MOCKS.md, status: complete }
  decisions: { file: DECISIONS.md, status: complete }
  handoff: { file: HANDOFF.md, status: complete }
  evidence: { dir: EVIDENCE/, status: complete }
  contracts: { dir: contracts/, status: complete }

gates:
  g0_frame: { passed: "2026-08-25", by: "Alex PM" }
  g1_solution: { passed: "2026-08-26", by: "Alex PM" }
  g2_alpha_review: { passed: "2026-08-28", tech_lead: "Sam Tech", qa: "Jo QA", coverage_plan: "agreed" }
  g3_bundle_complete: { passed: "2026-08-30", by: "Alex PM" }
  g4_freeze: { passed: "2026-09-01" }
  g5_productionise: { passed: "2026-09-02", by: "Sam Tech" }
  g6_reconcile: { passed: "2026-09-03", by: "Jo QA" }

release:
  release_id: "REPLACE_ID"
  frozen: "2026-09-01"
  prototype_commits: ["REPLACE_SHA"]
  figma_versions: []
  api_contracts: ["contracts/submit.schema.json@1"]
  evidence_tag: "REPLACE_TAG"
  digest: "REPLACE_DIGEST"
  approvals:
    pm_intent_behaviour: { by: "Alex PM", date: "2026-09-01" }
    tech_feasibility_delta: { by: "Sam Tech", date: "2026-09-01" }
    qa_coverage_testability: { by: "Jo QA", date: "2026-09-01" }
  supersedes: "n/a"

cold_session:
  passed: "2026-09-01"
  transcript: "EVIDENCE/cold-session-2026-09-01.md"
  open_defects: 0

changelog:
  - date: "2026-09-01"
    change: "fixture built"
    reason: "regression test"
"""
    w(COMPLETE / "prd.manifest.yaml", manifest_stub)
    subprocess.run(["git", "add", "-A"], cwd=COMPLETE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture commit"], cwd=COMPLETE, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=COMPLETE, capture_output=True, text=True, check=True).stdout.strip()

    import yaml
    m = yaml.safe_load((COMPLETE / "prd.manifest.yaml").read_text())
    release_id = "prd-demo-feature-r1"
    tag = "prd/demo-feature/r1"
    m["release"]["release_id"] = release_id
    m["release"]["prototype_commits"] = [sha]
    m["release"]["evidence_tag"] = tag
    digest = "sha256:" + _release.compute_digest(COMPLETE, m, release_id)  # compute_digest returns bare hex
    m["release"]["digest"] = digest
    # rewrite by string-replace on the stub so formatting/quoting stays hand-authored-looking
    text = manifest_stub.replace("REPLACE_ID", release_id).replace("REPLACE_SHA", sha) \
                         .replace("REPLACE_TAG", tag).replace("REPLACE_DIGEST", digest)
    w(COMPLETE / "prd.manifest.yaml", text)
    subprocess.run(["git", "add", "-A"], cwd=COMPLETE, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fill release block"], cwd=COMPLETE, check=True)
    subprocess.run(["git", "tag", "-a", tag, "-m", release_id], cwd=COMPLETE, check=True)
    print(f"complete-bundle built. commit={sha[:8]} tag={tag} digest={digest[:18]}...")


def build_broken():
    for p in BROKEN.rglob("*"):
        if p.is_file(): p.unlink()
    # A "shipped" bundle that is almost entirely hollow — this is the exact
    # shape the second external review demonstrated as a false green.
    broken = """prd:
  id: "broken-demo"
  title: "Broken Demo"
  owner: "Alex PM"
  tech_lead: "Sam Tech"
  qa: "Jo QA"
  designer: "none"
  tier: T2
  status: shipped
  standard_version: "1.2"

pinned_versions:
  build_framework: "n/a"

mandate:
  mandatory: false
  triggers: []
  opt_out: "n/a"

prototype:
  kind: standalone-app
  entry: "n/a"
  run: "n/a"
  base: "n/a"

data:
  provenance: synthetic
  dump_date: "n/a"
  scrub_pipeline: "n/a"
  pii_scrubbed: null

fidelity:
  - path: "whole flow"
    label: MOCKED
    note: "n/a"

coverage_plan:
  agreed_at: "pending"
  scenarios:
    - id: S1
      name: "something"
      status: demonstrable
      not_demonstrable_reason: "n/a"
      links: {}

artifacts:
  spec: { file: SPEC.md, status: complete }
  tracking: { file: TRACKING.md, status: complete }
  mocks: { file: MOCKS.md, status: complete }
  decisions: { file: DECISIONS.md, status: complete }
  handoff: { file: HANDOFF.md, status: complete }
  evidence: { dir: EVIDENCE/, status: complete }
  contracts: { dir: contracts/, status: complete }

gates:
  g0_frame: { passed: "2026-08-25", by: "Alex PM" }
  g1_solution: { passed: "pending", by: "" }
  g2_alpha_review: { passed: "pending", tech_lead: "", qa: "", coverage_plan: "pending" }
  g3_bundle_complete: { passed: "pending", by: "" }
  g4_freeze: { passed: "2026-09-01" }
  g5_productionise: { passed: "pending", by: "" }
  g6_reconcile: { passed: "pending", by: "" }

release:
  release_id: "prd-broken-demo-r1"
  frozen: "2026-09-01"
  prototype_commits: ["0000000000000000000000000000000000dead"]
  figma_versions: []
  api_contracts: []
  evidence_tag: "prd/broken-demo/r1"
  digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  approvals:
    pm_intent_behaviour: { by: "Alex PM", date: "2026-09-01" }
    tech_feasibility_delta: { by: "", date: "pending" }
    qa_coverage_testability: { by: "", date: "pending" }
  supersedes: "n/a"

cold_session:
  passed: "pending"
  transcript: "EVIDENCE/cold-session-missing.md"
  open_defects: 0

changelog:
  - date: "2026-09-01"
    change: "fixture built"
    reason: "regression test"
"""
    w(BROKEN / "prd.manifest.yaml", broken)
    # scaffolded-but-untouched artifact files (this is the "shipped with nothing real
    # in it" case) plus one file carrying a PII-shaped string to prove the scan blocks.
    w(BROKEN / "SPEC.md", "# SPEC\n\n<fill in screens, states, edge cases>\n")
    w(BROKEN / "TRACKING.md", "# TRACKING\n\n<fill in events>\n")
    w(BROKEN / "MOCKS.md", "# MOCKS\n\n<fill in mocks>\n")
    w(BROKEN / "DECISIONS.md", "# DECISIONS\n\n<fill in>\n")
    w(BROKEN / "HANDOFF.md", "# HANDOFF\n\n<fill in>\n")
    w(BROKEN / "EVIDENCE" / "README.md", "Evidence goes here.\n")
    w(BROKEN / "contracts" / "README.md", "Contracts go here.\n")
    # built from parts on purpose, so this literal never sits contiguously in
    # THIS source file — kit self-check would otherwise flag build_fixtures.py
    # itself (it's not under tests/fixtures/, so the SKIP_DIRS exclusion for
    # "fixtures" doesn't cover it). The generated notes.sql below still gets
    # the real contiguous string, which is the point of this fixture.
    fake_pan = "".join(["ABCDE", "1234", "F"])
    w(BROKEN / "notes.sql", f"-- test dump\nINSERT INTO users (name, pan) VALUES ('Test User', '{fake_pan}');\n")
    print("broken-bundle built.")


if __name__ == "__main__":
    build_complete()
    build_broken()
