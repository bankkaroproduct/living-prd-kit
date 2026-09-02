#!/usr/bin/env python3
"""freeze.py — compute the release identity for a Living PRD bundle (standard v1.2).

Usage: bin/freeze.py <bundle-dir> [--release-num N]

Reads the manifest, checks the bundle is freeze-ready, then prints:
  - the release block values to paste into prd.manifest.yaml (release_id, frozen, digest)
  - the git tag command that makes the release immutable (protect the prd/* tag pattern)

It does NOT rewrite the manifest — you paste the values, re-run validate.py, commit, tag.
The digest is sha256 over the pinned refs + approvals, so any later change to what was
signed produces a different digest: that is the release identity.
"""
import hashlib, json, sys
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed"); sys.exit(1)

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    bundle = Path(sys.argv[1])
    relnum = 1
    if "--release-num" in sys.argv:
        relnum = int(sys.argv[sys.argv.index("--release-num") + 1])
    m = yaml.safe_load((bundle / "prd.manifest.yaml").read_text())

    problems = []
    slug = (m.get("prd") or {}).get("id", "")
    rel = m.get("release") or {}
    cold = m.get("cold_session") or {}
    if not rel.get("prototype_commits"):
        problems.append("release.prototype_commits is empty — pin the exact SHAs (or file hash for T1) first")
    if cold.get("open_defects") != 0:
        problems.append(f"cold_session.open_defects is {cold.get('open_defects')} — must be 0 (run the stranger test)")
    for k, a in (rel.get("approvals") or {}).items():
        if not a.get("by"):
            problems.append(f"release.approvals.{k}.by is empty — all three signatures required before freeze")
    if problems:
        print("NOT freeze-ready:")
        for p in problems: print(f"  - {p}")
        sys.exit(1)

    release_id = f"prd-{slug}-r{relnum}"
    frozen = date.today().isoformat()
    material = {
        "release_id": release_id,
        "prototype_commits": rel.get("prototype_commits"),
        "figma_versions": rel.get("figma_versions"),
        "api_contracts": rel.get("api_contracts"),
        "approvals": rel.get("approvals"),
        "cold_session_transcript": cold.get("transcript"),
    }
    digest = "sha256:" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    tag = f"prd/{slug}/r{relnum}"

    print("Freeze-ready. Paste into prd.manifest.yaml -> release:")
    print(f'  release_id: "{release_id}"')
    print(f'  frozen: "{frozen}"')
    print(f'  evidence_tag: "{tag}"')
    print(f'  digest: "{digest}"')
    print("\nThen set prd.status: frozen, re-run bin/validate.py, commit, and tag:")
    print(f'  git tag -a {tag} -m "{release_id} {digest}"')
    print(f"  git push origin {tag}")
    print("\n(Protect the 'prd/*' tag pattern in repo settings so the tag can't be moved or deleted.)")

if __name__ == "__main__":
    main()
