#!/usr/bin/env python3
"""Build deterministic, non-secret SBOM and provenance records for one candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def load(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("INPUT_NOT_OBJECT")
    return value


def write_exclusive(path: pathlib.Path, value: object) -> str:
    payload = canonical(value)
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
    return hashlib.sha256(payload).hexdigest()


def build(identity_path: pathlib.Path, lock_path: pathlib.Path, sbom_path: pathlib.Path,
          provenance_path: pathlib.Path, builder_id: str) -> tuple[str, str]:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,79}", builder_id) is None:
        raise ValueError("BUILDER_ID_INVALID")
    identity = load(identity_path)
    required_identity = {
        "schema_version", "commit", "tree", "node_version", "npm_version",
        "build_os", "build_arch", "package_lock_sha256", "artifact_sha256",
    }
    if set(identity) != required_identity:
        raise ValueError("CANDIDATE_IDENTITY_INVALID")
    lock_bytes = lock_path.read_bytes()
    if hashlib.sha256(lock_bytes).hexdigest() != identity["package_lock_sha256"]:
        raise ValueError("PACKAGE_LOCK_BINDING_MISMATCH")
    lock = json.loads(lock_bytes)
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("PACKAGE_LOCK_PACKAGES_INVALID")
    components = []
    for location, package in sorted(packages.items()):
        if not location or not isinstance(package, dict):
            continue
        name = package.get("name") or location.rsplit("node_modules/", 1)[-1]
        version = package.get("version")
        integrity = package.get("integrity")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("PACKAGE_IDENTITY_INVALID")
        component = {"name": name, "version": version, "development": bool(package.get("dev"))}
        if isinstance(integrity, str):
            component["integrity"] = integrity
        components.append(component)
    sbom = {
        "schema_version": "1.0", "format": "bankkaro-node-lock-sbom",
        "candidate_artifact_sha256": identity["artifact_sha256"],
        "package_lock_sha256": identity["package_lock_sha256"],
        "components": components,
    }
    sbom_sha = write_exclusive(sbom_path, sbom)
    provenance = {
        "schema_version": "1.0", "builder_id": builder_id,
        "source": {"commit": identity["commit"], "tree": identity["tree"]},
        "subject": {"artifact_sha256": identity["artifact_sha256"]},
        "materials": {"package_lock_sha256": identity["package_lock_sha256"],
                      "sbom_sha256": sbom_sha},
        "build_environment": {"node_version": identity["node_version"],
                              "npm_version": identity["npm_version"],
                              "os": identity["build_os"], "arch": identity["build_arch"]},
        "network_install_phase": "ci-only",
    }
    provenance_sha = write_exclusive(provenance_path, provenance)
    return sbom_sha, provenance_sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-identity", required=True, type=pathlib.Path)
    parser.add_argument("--package-lock", required=True, type=pathlib.Path)
    parser.add_argument("--sbom-output", required=True, type=pathlib.Path)
    parser.add_argument("--provenance-output", required=True, type=pathlib.Path)
    parser.add_argument("--builder-id", required=True)
    args = parser.parse_args()
    sbom, provenance = build(args.candidate_identity, args.package_lock, args.sbom_output,
                             args.provenance_output, args.builder_id)
    print(json.dumps({"status": "PASS", "sbom_sha256": sbom,
                      "provenance_sha256": provenance}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
