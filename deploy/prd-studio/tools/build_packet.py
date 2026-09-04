#!/usr/bin/env python3
"""Assemble one immutable, target-free PRD Studio release packet."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import stat
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from prd_studio_deploy.constants import GATE_ORDER, RUNNER_ID, RUNNER_VERSION  # noqa: E402
from prd_studio_deploy.evidence import (validate_attestation,
                                        validate_gate_certification)  # noqa: E402
from prd_studio_deploy.records import (canonical_json_bytes, load_json,
                                       load_json_with_digest, sha256_bytes,
                                       sha256_file, write_exclusive)  # noqa: E402

REQUIRED_ATTESTATIONS = {
    "linux_ci_sha256", "independent_review_sha256", "rollback_rehearsal_sha256",
    "runner_conformance_sha256",
    "provider_contract_sha256",
}


def validate_day2_backup(path: pathlib.Path, candidate: dict[str, str]) -> None:
    value = load_json(path)
    required = {
        "schema_version", "evidence_id", "status", "candidate", "encrypted",
        "access_controlled", "schedule_seconds", "retention_days", "rpo_seconds",
        "isolated_restore", "reviewer_role", "reviewed_at_epoch", "expires_at_epoch",
    }
    if set(value) != required or value["schema_version"] != "1.0" or value["status"] != "PASS":
        raise ValueError("DAY2_BACKUP_EVIDENCE_INVALID")
    if value["candidate"] != {
            "component_id": "prd-studio", "environment": "protected-staging",
            "commit": candidate["commit"], "tree": candidate["tree"],
            "artifact_sha256": candidate["artifact_sha256"]}:
        raise ValueError("DAY2_BACKUP_CANDIDATE_MISMATCH")
    restore = value.get("isolated_restore")
    now = int(time.time())
    if (not isinstance(value["evidence_id"], str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,79}", value["evidence_id"]) is None
            or value["encrypted"] is not True or value["access_controlled"] is not True
            or type(value["schedule_seconds"]) is not int
            or not 300 <= value["schedule_seconds"] <= 86400
            or type(value["retention_days"]) is not int or value["retention_days"] < 7
            or type(value["rpo_seconds"]) is not int
            or not 1 <= value["rpo_seconds"] <= value["schedule_seconds"]
            or not isinstance(restore, dict)
            or set(restore) != {"status", "tested_at_epoch", "target_class"}
            or restore["status"] != "PASS" or restore["target_class"] != "isolated"
            or type(restore["tested_at_epoch"]) is not int
            or value["reviewer_role"] != "independent-operations-reviewer"
            or type(value["reviewed_at_epoch"]) is not int
            or type(value["expires_at_epoch"]) is not int
            or restore["tested_at_epoch"] > value["reviewed_at_epoch"]
            or value["reviewed_at_epoch"] > now + 300
            or not now < value["expires_at_epoch"] <= value["reviewed_at_epoch"] + 30 * 86400):
        raise ValueError("DAY2_BACKUP_EVIDENCE_NOT_READY")


def named(values: list[str]) -> dict[str, pathlib.Path]:
    result = {}
    for raw in values:
        if raw.count("=") != 1:
            raise ValueError("NAMED_PATH_INVALID")
        key, path = raw.split("=", 1)
        if key in result or not path:
            raise ValueError("NAMED_PATH_DUPLICATE")
        result[key] = pathlib.Path(path).resolve(strict=True)
    return result


def copy_immutable(source: pathlib.Path, destination: pathlib.Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("PACKET_SOURCE_NOT_REGULAR")
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0), 0o444)
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("PACKET_COPY_INCOMPLETE")
                view = view[written:]
        os.fchmod(destination_fd, 0o444)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after):
            raise ValueError("PACKET_SOURCE_CHANGED_DURING_COPY")
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
    parent_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return sha256_file(destination)


def require_canonical_json(path: pathlib.Path) -> dict[str, object]:
    value, digest = load_json_with_digest(path)
    if digest != sha256_bytes(canonical_json_bytes(value)):
        raise ValueError("EVIDENCE_JSON_NOT_CANONICAL")
    return value


def build(args: argparse.Namespace) -> pathlib.Path:
    output = args.output.resolve()
    output.mkdir(mode=0o755)
    attestations = named(args.attestation)
    certifications = named(args.gate_cert)
    if set(attestations) != REQUIRED_ATTESTATIONS:
        raise ValueError("ATTESTATION_SET_INVALID")
    if set(certifications) != set(GATE_ORDER):
        raise ValueError("GATE_CERTIFICATION_SET_INVALID")
    candidate_identity = load_json(args.candidate_identity.resolve(strict=True))
    validate_day2_backup(attestations["provider_contract_sha256"], candidate_identity)
    candidate = {
        "commit": candidate_identity["commit"], "tree": candidate_identity["tree"],
        "artifact_sha256": candidate_identity["artifact_sha256"],
    }
    definitions = {}
    for path in sorted((ROOT / "gates").glob("*.json")):
        gate = json.loads(path.read_text(encoding="utf-8"))
        definitions[gate["id"]] = gate
    if set(definitions) != set(GATE_ORDER):
        raise ValueError("GATE_DEFINITION_SET_INVALID")
    runner_sha256 = sha256_file(args.runner.resolve(strict=True))
    for name, source in attestations.items():
        validate_attestation(name, require_canonical_json(source), candidate)
    for gate_id, source in certifications.items():
        validate_gate_certification(
            gate_id, require_canonical_json(source), definitions[gate_id],
            runner_sha256, candidate)
    overlay_value = load_json(args.overlay.resolve(strict=True))
    rollback_value = load_json(args.rollback_artifact.resolve(strict=True))
    if (overlay_value.get("parent") != rollback_value.get("parent")
            or overlay_value.get("absence_artifact_sha256") != sha256_file(args.rollback_artifact)):
        raise ValueError("ABSENCE_ARTIFACT_BINDING_INVALID")
    parent = overlay_value["parent"]
    files: dict[str, dict[str, str]] = {}
    files["runner"] = {"path": f"runner/{args.runner.name}",
                       "sha256": copy_immutable(args.runner.resolve(strict=True), output / f"runner/{args.runner.name}")}
    files["candidate_artifact"] = {"path": "artifacts/prd-studio-candidate.tar",
                                   "sha256": copy_immutable(args.candidate_artifact.resolve(strict=True), output / "artifacts/prd-studio-candidate.tar")}
    files["rollback_artifact"] = {"path": "artifacts/prd-studio-canonical-absence.json",
                                  "sha256": copy_immutable(args.rollback_artifact.resolve(strict=True), output / "artifacts/prd-studio-canonical-absence.json")}
    files["overlay"] = {"path": "overlays/prd-studio-canonical-absence.json",
                        "sha256": copy_immutable(args.overlay.resolve(strict=True), output / "overlays/prd-studio-canonical-absence.json")}

    copied_attestations = {}
    for key, source in sorted(attestations.items()):
        relative = f"attestations/{key}.json"
        copied_attestations[key] = {"path": relative,
                                    "sha256": copy_immutable(source, output / relative)}
    copied_certs = {}
    for key, source in sorted(certifications.items()):
        relative = f"certifications/{key}.json"
        copied_certs[key] = {"path": relative,
                             "sha256": copy_immutable(source, output / relative)}

    gates = []
    smoke = None
    for gate_id in GATE_ORDER:
        gate = dict(definitions[gate_id])
        gate["certification_sha256"] = copied_certs[gate_id]["sha256"]
        if gate_id == "prd-studio-crud-smoke":
            smoke = gate
        else:
            gates.append(gate)
    assert smoke is not None
    candidate = {**candidate, "artifact_sha256": files["candidate_artifact"]["sha256"]}
    if candidate_identity.get("artifact_sha256") != candidate["artifact_sha256"]:
        raise ValueError("CANDIDATE_IDENTITY_MISMATCH")
    rollback = {"commit": parent["commit"], "tree": parent["tree"],
                "artifact_sha256": files["rollback_artifact"]["sha256"]}
    manifest = {
        "schema_version": "1.0", "release_id": args.release_id,
        "environment": "protected-staging", "lane": "stateful_backend",
        "authorization": {"approval_reference": args.approval_reference,
                          "approved_components": ["prd-studio"], "staging": True,
                          "production": False},
        "runner": {"id": RUNNER_ID, "version": RUNNER_VERSION,
                   "sha256": files["runner"]["sha256"]},
        "components": [{"id": "prd-studio", "repository": args.repository,
                        "candidate": candidate, "expected_live": rollback, "rollback": rollback}],
        "rollback_rehearsal": {
            "evidence_sha256": copied_attestations["rollback_rehearsal_sha256"]["sha256"],
            "proven_max_seconds": args.rollback_seconds,
        },
        "overlays": [{"id": "prd-studio-canonical-absence", "version": "1.0.0",
                      "sha256": files["overlay"]["sha256"]}],
        "state_transition": {
            "classification": "reset_to_empty",
            "recovery_policy": "Restore canonical absence; drop only an empty or exact certified synthetic database, otherwise enter incident recovery.",
        },
        "timing": {"admission_seconds": 120, "active_window_seconds": 900,
                   "rollback_decision_seconds": 720, "rollback_target_seconds": 120},
        "attestations": {key: entry["sha256"] for key, entry in copied_attestations.items()},
        "active_gates": gates, "acceptance_smoke": smoke,
    }
    manifest_path = output / "release-manifest.json"
    manifest_sha256 = write_exclusive(manifest_path, manifest, mode=0o444)
    files["manifest"] = {"path": "release-manifest.json", "sha256": manifest_sha256}
    index = {
        "schema_version": "1.0", "release_id": args.release_id,
        "manifest": files["manifest"], "runner": files["runner"],
        "candidate_artifact": files["candidate_artifact"],
        "rollback_artifact": files["rollback_artifact"], "overlay": files["overlay"],
        "attestations": copied_attestations, "gate_certifications": copied_certs,
    }
    index_path = output / "packet-index.json"
    write_exclusive(index_path, index, mode=0o444)
    return index_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--candidate-artifact", required=True, type=pathlib.Path)
    parser.add_argument("--candidate-identity", required=True, type=pathlib.Path)
    parser.add_argument("--rollback-artifact", required=True, type=pathlib.Path)
    parser.add_argument("--overlay", required=True, type=pathlib.Path)
    parser.add_argument("--runner", required=True, type=pathlib.Path)
    parser.add_argument("--attestation", action="append", default=[])
    parser.add_argument("--gate-cert", action="append", default=[])
    parser.add_argument("--rollback-seconds", type=int, choices=range(1, 121), default=120)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    index = build(args)
    print(json.dumps({"status": "PASS", "packet_index": str(index)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
