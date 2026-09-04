#!/usr/bin/env python3
"""Build the canonical first-install absence artifact and parent-bound overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from prd_studio_deploy.records import canonical_json_bytes  # noqa: E402

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _write_exclusive(path: pathlib.Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o444)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def build(parent_commit: str, parent_tree: str, artifact: pathlib.Path,
          overlay: pathlib.Path) -> tuple[str, str]:
    if not HEX40.fullmatch(parent_commit) or not HEX40.fullmatch(parent_tree):
        raise ValueError("PARENT_GIT_IDENTITY_INVALID")
    if artifact.exists() or overlay.exists():
        raise FileExistsError("absence output already exists")
    policy = json.loads((ROOT / "overlays/canonical-absence-policy-v1.json").read_text(encoding="utf-8"))
    absence = {
        "schema_version": "1.0", "artifact_type": "canonical-absence",
        "component_id": "prd-studio", "parent": {"commit": parent_commit, "tree": parent_tree},
        "required_absence": policy["required_absence"],
        "rollback_policy": policy["rollback_policy"],
    }
    artifact_bytes = canonical_json_bytes(absence)
    _write_exclusive(artifact, artifact_bytes)
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    concrete = {
        "schema_version": "1.0", "overlay_id": "prd-studio-canonical-absence",
        "version": "1.0.0", "component_id": "prd-studio",
        "classification": "reset_to_empty", "semantic_identity": "canonical-absence",
        "parent": {"commit": parent_commit, "tree": parent_tree},
        "absence_artifact_sha256": artifact_sha,
        "required_absence": policy["required_absence"],
        "rollback_policy": policy["rollback_policy"],
    }
    overlay_bytes = canonical_json_bytes(concrete)
    _write_exclusive(overlay, overlay_bytes)
    return artifact_sha, hashlib.sha256(overlay_bytes).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-commit", required=True)
    parser.add_argument("--parent-tree", required=True)
    parser.add_argument("--artifact", required=True, type=pathlib.Path)
    parser.add_argument("--overlay", required=True, type=pathlib.Path)
    args = parser.parse_args()
    artifact_sha, overlay_sha = build(args.parent_commit, args.parent_tree, args.artifact, args.overlay)
    print(json.dumps({"status": "PASS", "absence_artifact_sha256": artifact_sha,
                      "overlay_sha256": overlay_sha}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
