"""_release.py — shared release-identity logic for freeze.py and validate.py.

Kept in one place so the validator can independently RECOMPUTE the digest
freeze.py produced. A manifest can no longer just carry a plausible-looking
sha256 string — it has to match the bundle's real, current bytes, or
validate.py fails it. Also carries the (best-effort) git existence checks
used to catch fabricated commit/tag references.
"""
import hashlib, json, subprocess
from pathlib import Path

# Every artifact file/dir whose literal bytes get signed at freeze. Order is
# fixed and paths are sorted, so the hash is reproducible across machines.
#
# prd.manifest.yaml is deliberately NOT in this list: release_id/frozen/
# evidence_tag/digest live inside it and only get pasted in AFTER this hash
# is computed (freeze.py prints them, the human pastes them, then commits).
# Hashing the manifest's bytes would make the digest depend on itself —
# unrecoverable the moment the digest field is filled in. The manifest
# fields that DO matter (prototype_commits, figma_versions, api_contracts,
# approvals, cold_session.transcript) are pulled in explicitly below instead,
# read at the same "before the paste" moment, so they're stable too.
HASHED_FILES = ["SPEC.md", "TRACKING.md", "MOCKS.md", "DECISIONS.md", "HANDOFF.md"]
HASHED_DIRS = ["contracts", "EVIDENCE"]


def bundle_content_hash(bundle: Path) -> str:
    """sha256 over the literal bytes of every artifact file that currently exists."""
    paths = [bundle / name for name in HASHED_FILES if (bundle / name).exists()]
    for d in HASHED_DIRS:
        dp = bundle / d
        if dp.exists():
            paths.extend(f for f in dp.rglob("*") if f.is_file())
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x.relative_to(bundle))):
        h.update(str(p.relative_to(bundle)).encode())
        h.update(b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def compute_digest(bundle: Path, manifest: dict, release_id: str) -> str:
    """Returns the bare hex digest (no 'sha256:' prefix — callers add that)."""
    rel = manifest.get("release") or {}
    cold = manifest.get("cold_session") or {}
    material = {
        "release_id": release_id,
        "prototype_commits": rel.get("prototype_commits"),
        "figma_versions": rel.get("figma_versions"),
        "api_contracts": rel.get("api_contracts"),
        "approvals": rel.get("approvals"),
        "cold_session_transcript": cold.get("transcript"),
        "bundle_content_sha256": bundle_content_hash(bundle),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _git(bundle: Path, *args):
    try:
        return subprocess.run(["git", "-C", str(bundle), *args], capture_output=True, text=True, timeout=10)
    except Exception:
        return None


def in_git_repo(bundle: Path) -> bool:
    r = _git(bundle, "rev-parse", "--is-inside-work-tree")
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")


def commit_exists(bundle: Path, sha: str) -> bool:
    r = _git(bundle, "cat-file", "-e", f"{sha}^{{commit}}")
    return bool(r and r.returncode == 0)


def tag_exists(bundle: Path, tag: str) -> bool:
    r = _git(bundle, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}")
    return bool(r and r.returncode == 0)


def next_release_num(bundle: Path, slug: str) -> int:
    """Highest existing prd/<slug>/rN tag + 1, defaulting to 1 if none / not a repo."""
    r = _git(bundle, "tag", "-l", f"prd/{slug}/r*")
    if not r or r.returncode != 0 or not r.stdout.strip():
        return 1
    nums = []
    for line in r.stdout.strip().splitlines():
        tail = line.rsplit("/r", 1)[-1]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1
