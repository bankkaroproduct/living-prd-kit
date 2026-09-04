"""Narrow adapter around the canonical external deployment-slot controller."""

from __future__ import annotations

import json
import os
import pathlib
import pwd
import re
from typing import Any

from .constants import CANONICAL_CONTROLLER
from .errors import RunnerError
from .process import run_bounded

REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


class SlotController:
    def __init__(self, packet: Any, approval: pathlib.Path, receipt: pathlib.Path,
                 runner_file: pathlib.Path):
        canonical = CANONICAL_CONTROLLER.resolve(strict=True)
        if canonical != CANONICAL_CONTROLLER:
            raise RunnerError("CANONICAL_CONTROLLER_PATH_CHANGED")
        self.packet = packet
        self.approval = approval.resolve(strict=True)
        self.receipt = receipt
        self.runner_file = runner_file.resolve(strict=True)

    def _evidence_args(self) -> list[str]:
        args: list[str] = []
        for evidence_id, path in sorted(self.packet.attestations.items()):
            args += ["--attestation", f"{evidence_id}={path}"]
        for gate_id, path in sorted(self.packet.gate_certifications.items()):
            args += ["--gate-cert", f"{gate_id}={path}"]
        args += ["--overlay", f"prd-studio-canonical-absence={self.packet.overlay}"]
        return args

    def _call(self, arguments: list[str], timeout: float = 15) -> tuple[int, dict[str, Any]]:
        account_home = pwd.getpwuid(os.geteuid()).pw_dir
        result = run_bounded(
            ["/usr/bin/python3", str(CANONICAL_CONTROLLER), *arguments],
            timeout_seconds=timeout,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": account_home},
        )
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            raise RunnerError("CONTROLLER_OUTPUT_INVALID")
        try:
            value = json.loads(lines[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunnerError("CONTROLLER_OUTPUT_INVALID") from error
        if not isinstance(value, dict) or not isinstance(value.get("status"), str):
            raise RunnerError("CONTROLLER_OUTPUT_INVALID")
        reason = value.get("reason_code")
        if reason is not None and (not isinstance(reason, str) or not REASON.fullmatch(reason)):
            raise RunnerError("CONTROLLER_UNSAFE_REASON_CODE")
        return result.returncode, value

    def validate(self) -> None:
        code, result = self._call([
            "validate", str(self.packet.manifest), str(self.runner_file), str(self.approval),
            *self._evidence_args(),
        ], 30)
        if code != 0 or result.get("status") != "PASS":
            raise RunnerError(result.get("reason_code", "CONTROLLER_VALIDATE_FAILED"))

    def start(self) -> None:
        code, result = self._call([
            "start", str(self.packet.manifest), str(self.runner_file), str(self.approval),
            str(self.receipt), *self._evidence_args(),
        ], 30)
        if code != 0 or result.get("status") != "ACTIVE":
            raise RunnerError(result.get("reason_code", "CONTROLLER_START_FAILED"))

    def check(self, phase: str, timeout: float = 10) -> tuple[int, dict[str, Any]]:
        return self._call(["check", str(self.receipt), phase], timeout)

    def mark_mutation(self, timeout: float = 10) -> None:
        code, result = self._call([
            "mark-mutation", str(self.receipt), "prd-studio", "INSTALL_CANDIDATE",
        ], timeout)
        if code != 0 or result.get("status") != "MUTATION_STARTED":
            raise RunnerError(result.get("reason_code", "MUTATION_MARKER_FAILED"))

    def record_failure(self, gate_id: str, reason_code: str, timeout: float = 10) -> None:
        code, result = self._call([
            "record-failure", str(self.receipt), gate_id, reason_code,
        ], timeout)
        if code != 0 or result.get("status") != "FAILURE_RECORDED":
            raise RunnerError(result.get("reason_code", "FAILURE_MARKER_FAILED"))

    def begin_rollback(self, timeout: float = 10) -> None:
        code, result = self._call(["begin-rollback", str(self.receipt)], timeout)
        if code != 0 or result.get("status") != "ROLLBACK_ACTIVE":
            raise RunnerError(result.get("reason_code", "ROLLBACK_MARKER_FAILED"))

    def record_rollback_failure(self, reason_code: str, timeout: float = 10) -> None:
        code, result = self._call([
            "record-rollback-failure", str(self.receipt), "rollback-verification", reason_code,
        ], timeout)
        if code != 30 or result.get("status") != "INCIDENT_RECOVERY_ONLY":
            raise RunnerError(result.get("reason_code", "ROLLBACK_FAILURE_MARKER_FAILED"))

    def finish(self, outcome: str, live_identities: pathlib.Path,
               gate_summary: pathlib.Path, *, state_changed: bool,
               rollback_ran: bool, outage_seconds: int, timeout: float = 20) -> None:
        state_flag = "--state-changed" if state_changed else "--no-state-changed"
        rollback_flag = "--rollback-ran" if rollback_ran else "--no-rollback-ran"
        code, result = self._call([
            "finish", str(self.receipt), outcome, str(live_identities), str(gate_summary),
            str(outage_seconds), state_flag, rollback_flag,
        ], timeout)
        if code != 0 or result.get("status") != "TERMINAL":
            raise RunnerError(result.get("reason_code", "CONTROLLER_FINISH_FAILED"))
