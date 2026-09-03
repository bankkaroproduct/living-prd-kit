#!/usr/bin/env python3
"""freeze.py — compute the release identity for a Living PRD bundle (standard v1.2).

Usage: bin/freeze.py <bundle-dir> [--release-num N]

Runs bin/validate.py on the bundle first and refuses to freeze if it fails —
freeze is not a separate opinion from the validator, it's downstream of it.
Then checks gates, approvals and cold-session state itself, and computes the
digest over the bundle's actual current file bytes (via _release.py), not
just the manifest's self-reported strings — so a later edit to SPEC.md (or
any signed artifact) invalidates the digest instead of leaving it unchanged.

It does NOT rewrite the manifest — you paste the values, re-run validate.py,
commit, tag.
"""
import subprocess, sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _release

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed"); sys.exit(1)

REQUIRED_GATES_BEFORE_FREEZE = ["g0_frame", "g1_solution", "g2_alpha_review", "g3_bundle_complete"]
REQUIRED_APPROVAL_KEYS = ["pm_intent_behaviour", "tech_feasibility_delta", "qa_coverage_testability"]

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    bundle = Path(sys.argv[1])
    relnum_override = None
    if "--release-num" in sys.argv:
        relnum_override = int(sys.argv[sys.argv.index("--release-num") + 1])

    validate_py = Path(__file__).resolve().parent / "validate.py"
    vr = subprocess.run([sys.executable, str(validate_py), str(bundle)], capture_output=True, text=True)
    if vr.returncode != 0:
        print("NOT freeze-ready: bin/validate.py failed on this bundle. Fix that first:\n")
        print(vr.stdout or vr.stderr)
        sys.exit(1)

    m = yaml.safe_load((bundle / "prd.manifest.yaml").read_text())
    problems = []
    slug = (m.get("prd") or {}).get("id", "")
    rel = m.get("release") or {}
    cold = m.get("cold_session") or {}
    gates = m.get("gates") or {}
    approvals = rel.get("approvals") or {}

    for gk in REQUIRED_GATES_BEFORE_FREEZE:
        if (gates.get(gk) or {}).get("passed") in ("pending", "", None):
            problems.append(f"gates.{gk}.passed is still pending — G4 comes after G0–G3, not instead of them")
    if not rel.get("prototype_commits"):
        problems.append("release.prototype_commits is empty — pin the exact SHAs (or file hash for T1) first")
    if cold.get("open_defects") != 0:
        problems.append(f"cold_session.open_defects is {cold.get('open_defects')} — must be 0 (run the stranger/cold-session test)")
    for k in REQUIRED_APPROVAL_KEYS:
        a = approvals.get(k)
        if not a or not a.get("by"):
            problems.append(f"release.approvals.{k}.by is empty — all three signatures required before freeze")

    relnum = relnum_override if relnum_override is not None else _release.next_release_num(bundle, slug)
    tag = f"prd/{slug}/r{relnum}"
    if _release.in_git_repo(bundle) and _release.tag_exists(bundle, tag):
        problems.append(f"tag {tag} already exists — pass --release-num N for the next free number "
                         f"(next free: {_release.next_release_num(bundle, slug)})")

    if problems:
        print("NOT freeze-ready:")
        for p in problems: print(f"  - {p}")
        sys.exit(1)

    release_id = f"prd-{slug}-r{relnum}"
    frozen = date.today().isoformat()
    digest = "sha256:" + _release.compute_digest(bundle, m, release_id)

    print("Freeze-ready. Paste into prd.manifest.yaml -> release:")
    print(f'  release_id: "{release_id}"')
    print(f'  frozen: "{frozen}"')
    print(f'  evidence_tag: "{tag}"')
    print(f'  digest: "{digest}"')
    print("\nThen set prd.status: frozen, re-run bin/validate.py (it will recompute and check this")
    print("same digest against the bundle's actual content), commit, and tag:")
    print(f'  git tag -a {tag} -m "{release_id} {digest}"')
    print(f"  git push origin {tag}")
    print("\n(Protect the 'prd/**' tag pattern in repo settings so the tag can't be moved or deleted —")
    print(" use '**' not '*': these tags nest as prd/<slug>/r<N>, two levels under prd/.)")

if __name__ == "__main__":
    main()
