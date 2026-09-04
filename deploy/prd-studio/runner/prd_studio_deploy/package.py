"""Immutable release-packet loading and cross-file binding checks."""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any

from .constants import COMPONENT_ID, GATE_ORDER, RUNNER_ID, RUNNER_VERSION
from .evidence import (candidate_identity, validate_attestation,
                       validate_gate_certification)
from .errors import RunnerError
from .records import HEX64, load_json, sha256_file

SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _packet_path(root: pathlib.Path, raw: Any) -> pathlib.Path:
    if not isinstance(raw, str) or not SAFE_RELATIVE.fullmatch(raw):
        raise RunnerError("PACKET_PATH_INVALID")
    relative = pathlib.PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise RunnerError("PACKET_PATH_INVALID")
    path = (root / pathlib.Path(*relative.parts)).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RunnerError("PACKET_PATH_ESCAPES_ROOT") from error
    return path


@dataclass(frozen=True)
class ReleasePacket:
    root: pathlib.Path
    index: pathlib.Path
    manifest: pathlib.Path
    runner: pathlib.Path
    candidate_artifact: pathlib.Path
    rollback_artifact: pathlib.Path
    overlay: pathlib.Path
    attestations: dict[str, pathlib.Path]
    gate_certifications: dict[str, pathlib.Path]
    manifest_value: dict[str, Any]
    overlay_value: dict[str, Any]


def load_packet(root: pathlib.Path) -> ReleasePacket:
    root = root.resolve(strict=True)
    index_path = root / "packet-index.json"
    index = load_json(index_path)
    required = {
        "schema_version", "release_id", "manifest", "runner", "candidate_artifact",
        "rollback_artifact", "overlay", "attestations", "gate_certifications",
    }
    if set(index) != required or index["schema_version"] != "1.0":
        raise RunnerError("PACKET_INDEX_FIELDS_INVALID")
    references: dict[str, pathlib.Path] = {}
    for key in ("manifest", "runner", "candidate_artifact", "rollback_artifact", "overlay"):
        entry = index[key]
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise RunnerError("PACKET_INDEX_REFERENCE_INVALID")
        path = _packet_path(root, entry["path"])
        if not isinstance(entry["sha256"], str) or not HEX64.fullmatch(entry["sha256"]):
            raise RunnerError("PACKET_INDEX_REFERENCE_HASH_INVALID")
        if sha256_file(path) != entry["sha256"]:
            raise RunnerError("PACKET_INDEX_REFERENCE_HASH_MISMATCH")
        references[key] = path

    def named_paths(key: str) -> dict[str, pathlib.Path]:
        values = index[key]
        if not isinstance(values, dict) or not values:
            raise RunnerError("PACKET_NAMED_REFERENCE_INVALID")
        result = {}
        for name, entry in values.items():
            if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise RunnerError("PACKET_NAMED_REFERENCE_INVALID")
            path = _packet_path(root, entry["path"])
            if not isinstance(entry["sha256"], str) or sha256_file(path) != entry["sha256"]:
                raise RunnerError("PACKET_NAMED_REFERENCE_HASH_MISMATCH")
            result[name] = path
        return result

    attestations = named_paths("attestations")
    gate_certifications = named_paths("gate_certifications")
    manifest = load_json(references["manifest"])
    overlay = load_json(references["overlay"])
    if manifest.get("release_id") != index["release_id"]:
        raise RunnerError("PACKET_RELEASE_ID_MISMATCH")
    if manifest.get("environment") != "protected-staging" or manifest.get("lane") != "stateful_backend":
        raise RunnerError("PACKET_SCOPE_INVALID")
    if manifest.get("state_transition") != {
        "classification": "reset_to_empty",
        "recovery_policy": "Restore canonical absence; drop only an empty or exact certified synthetic database, otherwise enter incident recovery.",
    }:
        raise RunnerError("PACKET_STATE_POLICY_INVALID")
    components = manifest.get("components")
    if not isinstance(components, list) or len(components) != 1 or components[0].get("id") != COMPONENT_ID:
        raise RunnerError("PACKET_COMPONENT_INVALID")
    runner = manifest.get("runner")
    if (not isinstance(runner, dict) or runner.get("id") != RUNNER_ID
            or runner.get("version") != RUNNER_VERSION
            or runner.get("sha256") != sha256_file(references["runner"])):
        raise RunnerError("PACKET_RUNNER_IDENTITY_INVALID")
    gates = manifest.get("active_gates")
    smoke = manifest.get("acceptance_smoke")
    ids = [gate.get("id") for gate in gates] + [smoke.get("id")] if isinstance(gates, list) and isinstance(smoke, dict) else []
    if set(ids) != set(GATE_ORDER) or len(ids) != len(GATE_ORDER):
        raise RunnerError("PACKET_GATE_SET_INVALID")
    expected_attestations = set(manifest.get("attestations", {}))
    if set(attestations) != expected_attestations:
        raise RunnerError("PACKET_ATTESTATION_SET_INVALID")
    if any(sha256_file(attestations[name]) != manifest["attestations"][name]
           for name in attestations):
        raise RunnerError("PACKET_ATTESTATION_MANIFEST_HASH_MISMATCH")
    expected_certs = {gate["id"] for gate in gates + [smoke]}
    if set(gate_certifications) != expected_certs:
        raise RunnerError("PACKET_GATE_CERTIFICATION_SET_INVALID")
    gate_by_id = {gate["id"]: gate for gate in gates + [smoke]}
    if any(sha256_file(gate_certifications[name])
           != gate_by_id[name]["certification_sha256"]
           for name in gate_certifications):
        raise RunnerError("PACKET_GATE_CERTIFICATION_MANIFEST_HASH_MISMATCH")
    component = components[0]
    if component["candidate"]["artifact_sha256"] != sha256_file(references["candidate_artifact"]):
        raise RunnerError("PACKET_CANDIDATE_ARTIFACT_MISMATCH")
    if component["rollback"]["artifact_sha256"] != sha256_file(references["rollback_artifact"]):
        raise RunnerError("PACKET_ROLLBACK_ARTIFACT_MISMATCH")
    if (overlay.get("parent") != {"commit": component["rollback"]["commit"], "tree": component["rollback"]["tree"]}
            or overlay.get("absence_artifact_sha256") != component["rollback"]["artifact_sha256"]):
        raise RunnerError("PACKET_ABSENCE_BINDING_INVALID")
    candidate = candidate_identity(component)
    for name, path in attestations.items():
        validate_attestation(name, load_json(path), candidate)
    for gate_id, path in gate_certifications.items():
        validate_gate_certification(
            gate_id, load_json(path), gate_by_id[gate_id], runner["sha256"], candidate)
    return ReleasePacket(
        root, index_path, references["manifest"], references["runner"],
        references["candidate_artifact"], references["rollback_artifact"],
        references["overlay"], attestations, gate_certifications, manifest, overlay,
    )
