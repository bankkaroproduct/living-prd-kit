"""Gate result normalization and evidence recording."""

from __future__ import annotations

import dataclasses
import pathlib
import re
import time
from typing import Any

from .errors import ChildTimeout, RunnerError
from .records import assert_secret_free, write_exclusive

REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


@dataclasses.dataclass
class GateResult:
    id: str
    version: str
    status: str
    reason_code: str
    started_epoch: int
    finished_epoch: int
    duration_ms: int
    attempts: int
    evidence_sha256: str
    evidence_reference: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def skipped(gate: dict[str, Any], epoch: int) -> GateResult:
    return GateResult(gate["id"], gate["version"], "SKIPPED", "NOT_RUN", epoch, epoch, 0, 0,
                      "0" * 64, "none")


class GateRecorder:
    def __init__(self, attempt_dir: pathlib.Path, release_id: str,
                 gates: list[dict[str, Any]], started_epoch: int):
        self.attempt_dir = attempt_dir
        self.evidence_dir = attempt_dir / "evidence"
        self.evidence_dir.mkdir(mode=0o700)
        self.release_id = release_id
        self.gates = {gate["id"]: gate for gate in gates}
        self.results = {gate["id"]: skipped(gate, started_epoch) for gate in gates}

    def record(self, gate_id: str, response: dict[str, Any] | None, *,
               started_epoch: int, started_ns: int,
               error: BaseException | None = None) -> GateResult:
        gate = self.gates[gate_id]
        finished_epoch = int(time.time())
        duration_ms = max(1, (time.monotonic_ns() - started_ns) // 1_000_000)
        overrun = duration_ms > gate["timeout_seconds"] * 1000
        if response is not None:
            status = response.get("status", "ERROR")
            reason = response.get("reason_code", "REMOTE_RESPONSE_INVALID")
            evidence = response.get("evidence", {})
        elif isinstance(error, ChildTimeout):
            status, reason, evidence = "ERROR", "GATE_TIMEOUT", {}
        elif isinstance(error, RunnerError):
            status, reason, evidence = "ERROR", error.reason_code, {}
        else:
            status, reason, evidence = "ERROR", "MISSING_GATE_EVIDENCE", {}
        if status not in {"PASS", "FAIL", "ERROR"}:
            status, reason, evidence = "ERROR", "REMOTE_RESPONSE_INVALID", {}
        if not isinstance(reason, str) or not REASON.fullmatch(reason):
            status, reason, evidence = "ERROR", "UNSAFE_GATE_REASON", {}
        if overrun:
            status, reason, evidence = "ERROR", "GATE_TIMEOUT", {}
        document = {
            "schema_version": "1.0", "release_id": self.release_id,
            "gate_id": gate_id, "gate_version": gate["version"],
            "status": status, "reason_code": reason, "evidence": evidence,
        }
        assert_secret_free(document)
        prior = self.results[gate_id]
        suffix = ".failure.json" if prior.status != "SKIPPED" else ".json"
        path = self.evidence_dir / f"{gate_id}{suffix}"
        digest = write_exclusive(path, document)
        result = GateResult(
            gate_id, gate["version"], status, reason, started_epoch, finished_epoch,
            duration_ms, 1, digest, f"evidence/{path.name}",
        )
        self.results[gate_id] = result
        return result

    def summary(self, manifest_sha256: str) -> dict[str, Any]:
        results = []
        for gate in self.gates.values():
            result = self.results[gate["id"]]
            value = result.as_dict()
            results.append(value)
        return {"schema_version": "1.0", "release_id": self.release_id,
                "manifest_sha256": manifest_sha256, "results": results}


def evaluate_certification_case(response: dict[str, Any] | None,
                                error: BaseException | None = None) -> tuple[str, str]:
    """Small pure boundary used by the gate certification harness."""
    if response is None:
        return ("ERROR", "GATE_TIMEOUT") if isinstance(error, ChildTimeout) else (
            "ERROR", "MISSING_GATE_EVIDENCE")
    status = response.get("status")
    reason = response.get("reason_code")
    if status not in {"PASS", "FAIL", "ERROR"} or not isinstance(reason, str) or not REASON.fullmatch(reason):
        return "ERROR", "REMOTE_RESPONSE_INVALID"
    return status, reason
