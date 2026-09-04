"""Strict, secret-free release-evidence validation.

The canonical slot controller authenticates evidence bytes by digest.  This
module supplies the project-specific semantic boundary which the controller
deliberately does not impose.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import RunnerError
from .records import HEX64, assert_secret_free, canonical_json_bytes

HEX40 = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
FORBIDDEN_FIELD = re.compile(
    r"(?:password|passwd|secret|token|authorization|cookie|body|payload|"
    r"request|response|project_data|customer|email|phone|username|raw_provider)", re.I)
URL_VALUE = re.compile(r"(?:https?|ssh|mysql)://", re.I)

ATTESTATION_IDS = {
    "linux_ci_sha256": "linux-ci",
    "independent_review_sha256": "independent-review",
    "rollback_rehearsal_sha256": "rollback-rehearsal",
    "runner_conformance_sha256": "runner-conformance",
}


def _walk(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise RunnerError("EVIDENCE_NESTING_LIMIT")
    if isinstance(value, dict):
        if len(value) > 128:
            raise RunnerError("EVIDENCE_FIELD_LIMIT")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 80 or FORBIDDEN_FIELD.search(key):
                raise RunnerError("EVIDENCE_PROTECTED_FIELD")
            _walk(child, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 1024:
            raise RunnerError("EVIDENCE_ITEM_LIMIT")
        for child in value:
            _walk(child, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 512 or URL_VALUE.search(value) or "\n" in value or "\r" in value:
            raise RunnerError("EVIDENCE_UNSAFE_STRING")
    elif value is None or type(value) in {bool, int}:
        return
    else:
        raise RunnerError("EVIDENCE_SCALAR_INVALID")


def assert_release_evidence_safe(value: Any) -> None:
    if not isinstance(value, dict):
        raise RunnerError("EVIDENCE_NOT_OBJECT")
    if len(canonical_json_bytes(value)) > 1024 * 1024:
        raise RunnerError("EVIDENCE_SIZE_LIMIT")
    assert_secret_free(value)
    _walk(value)


def candidate_identity(component: dict[str, Any]) -> dict[str, str]:
    candidate = component["candidate"]
    return {"commit": candidate["commit"], "tree": candidate["tree"],
            "artifact_sha256": candidate["artifact_sha256"]}


def validate_attestation(name: str, value: dict[str, Any],
                         candidate: dict[str, str]) -> None:
    assert_release_evidence_safe(value)
    if name == "provider_contract_sha256":
        expected_candidate = {
            "component_id": "prd-studio", "environment": "protected-staging",
            **candidate,
        }
        if (value.get("schema_version") != "1.0" or value.get("status") != "PASS"
                or value.get("candidate") != expected_candidate):
            raise RunnerError("PROVIDER_CONTRACT_BINDING_INVALID")
        return
    expected_id = ATTESTATION_IDS.get(name)
    if expected_id is None:
        raise RunnerError("ATTESTATION_ID_INVALID")
    required = {"schema_version", "evidence_id", "status", "candidate"}
    if (not required.issubset(value) or value["schema_version"] != "1.0"
            or value["evidence_id"] != expected_id or value["status"] != "PASS"
            or value["candidate"] != candidate):
        raise RunnerError("ATTESTATION_SEMANTICS_INVALID")
    if name == "rollback_rehearsal_sha256":
        if (type(value.get("proven_max_seconds")) is not int
                or not 1 <= value["proven_max_seconds"] <= 120
                or value.get("canonical_absence_restored") is not True):
            raise RunnerError("ROLLBACK_REHEARSAL_SEMANTICS_INVALID")


def validate_gate_certification(gate_id: str, value: dict[str, Any],
                                gate: dict[str, Any], runner_sha256: str,
                                candidate: dict[str, str]) -> None:
    assert_release_evidence_safe(value)
    required = {
        "schema_version", "certification_id", "gate_id", "gate_version", "status",
        "runner_sha256", "candidate", "tested_at_epoch", "reviewer_role",
        "reviewer_id_sha256", "cases",
    }
    cases = value.get("cases")
    if (set(value) != required or value["schema_version"] != "1.0"
            or value["gate_id"] != gate_id or value["gate_version"] != gate["version"]
            or value["status"] != "PASS" or value["runner_sha256"] != runner_sha256
            or value["candidate"] != candidate
            or not isinstance(value["certification_id"], str)
            or SAFE_ID.fullmatch(value["certification_id"]) is None
            or type(value["tested_at_epoch"]) is not int
            or value["reviewer_role"] != "independent-release-reviewer"
            or not isinstance(value["reviewer_id_sha256"], str)
            or HEX64.fullmatch(value["reviewer_id_sha256"]) is None
            or not isinstance(cases, dict)
            or cases.get("known_good") != "PASS"
            or cases.get("known_bad") != "FAIL"
            or cases.get("missing") != "ERROR"
            or cases.get("timeout") != "ERROR"):
        raise RunnerError("GATE_CERTIFICATION_SEMANTICS_INVALID")
    if not HEX64.fullmatch(runner_sha256) or not HEX40.fullmatch(candidate["commit"]):
        raise RunnerError("GATE_CERTIFICATION_BINDING_INVALID")
