"""Independent local supervisor for one bounded protected-staging slot."""

from __future__ import annotations

import pathlib
import time
from typing import Any, Callable

from .asset_loader import load_assets
from .constants import PHASE_BY_GATE
from .controller import SlotController
from .errors import GateFailure, RunnerError
from .gate_runtime import GateRecorder
from .package import ReleasePacket
from .profile import ConnectionProfile
from .records import load_json, sha256_bytes, sha256_file, write_exclusive
from .remote_protocol import RemoteSession


class BoundaryDecision(RunnerError):
    def __init__(self, reason_code: str, *, controller_recorded: bool = False):
        super().__init__(reason_code)
        self.controller_recorded = controller_recorded


class DeploymentSupervisor:
    def __init__(
        self,
        packet: ReleasePacket,
        profile: ConnectionProfile,
        approval: pathlib.Path,
        attempt_dir: pathlib.Path,
        runner_file: pathlib.Path,
        *,
        controller: Any | None = None,
        session_factory: Callable[[ConnectionProfile], Any] = RemoteSession,
    ):
        self.packet = packet
        self.profile = profile
        self.approval = approval
        self.attempt_dir = attempt_dir
        self.runner_file = runner_file
        self.receipt = attempt_dir / f"{packet.manifest_value['release_id']}.receipt.json"
        self.controller = controller or SlotController(packet, approval, self.receipt, runner_file)
        self.session_factory = session_factory
        self.session: Any | None = None
        self.mutated = False
        self.failure_recorded = False
        self.recorder: GateRecorder | None = None
        self.receipt_value: dict[str, Any] | None = None
        self.controller_terminal = False

    @property
    def component(self) -> dict[str, Any]:
        return self.packet.manifest_value["components"][0]

    @property
    def gates(self) -> list[dict[str, Any]]:
        manifest = self.packet.manifest_value
        return manifest["active_gates"] + [manifest["acceptance_smoke"]]

    def _slot_remaining(self, limit_seconds: int) -> float:
        if self.receipt_value is None:
            raise RunnerError("SLOT_RECEIPT_NOT_LOADED")
        wall_elapsed = (time.time_ns() - self.receipt_value["started_wall_ns"]) / 1_000_000_000
        mono_elapsed = (time.monotonic_ns()
                        - self.receipt_value["started_monotonic_ns"]) / 1_000_000_000
        return limit_seconds - max(0.0, wall_elapsed, mono_elapsed)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerError("BOUNDARY_BUDGET_EXHAUSTED")
        return remaining

    def _forward_gate_deadline(self, gate: dict[str, Any]) -> float:
        remaining = self._slot_remaining(720)
        if remaining < gate["timeout_seconds"]:
            raise BoundaryDecision("FORWARD_GATE_BUDGET_UNAVAILABLE")
        return min(time.monotonic() + gate["timeout_seconds"],
                   time.monotonic() + remaining)

    def _check(self, phase: str, deadline: float) -> None:
        timeout = min(10.0, self._remaining(deadline))
        code, decision = self.controller.check(phase, timeout=timeout)
        if code == 0 and decision.get("status") == "CONTINUE":
            return
        reason = decision.get("reason_code", "CONTROLLER_BOUNDARY_REJECTED")
        raise BoundaryDecision(reason, controller_recorded=self.mutated and code in {20, 30})

    def _record_gate(self, gate_id: str, started_epoch: int, started_ns: int,
                     response: dict[str, Any] | None = None,
                     error: BaseException | None = None) -> None:
        assert self.recorder is not None
        result = self.recorder.record(
            gate_id, response, started_epoch=started_epoch, started_ns=started_ns, error=error)
        if result.status != "PASS":
            raise GateFailure(gate_id, result.status, result.reason_code)

    def _remote_gate(self, gate_id: str) -> None:
        assert self.session is not None
        gate = next(item for item in self.gates if item["id"] == gate_id)
        started_epoch, started_ns = int(time.time()), time.monotonic_ns()
        try:
            deadline = self._forward_gate_deadline(gate)
            self._check(PHASE_BY_GATE[gate_id], deadline)
            response = self.session.request(gate_id, {}, self._remaining(deadline))
            self._record_gate(gate_id, started_epoch, started_ns, response=response)
        except BoundaryDecision as decision:
            assert self.recorder is not None
            result = self.recorder.record(
                gate_id, None, started_epoch=started_epoch, started_ns=started_ns,
                error=RunnerError(decision.reason_code))
            raise GateFailure(gate_id, result.status, result.reason_code,
                              controller_recorded=decision.controller_recorded)
        except GateFailure:
            raise
        except BaseException as error:
            self._record_gate(gate_id, started_epoch, started_ns, error=error)

    def _global_lock(self, payload: dict[str, Any]) -> None:
        gate = next(item for item in self.gates if item["id"] == "global-lock")
        started_epoch, started_ns = int(time.time()), time.monotonic_ns()
        try:
            deadline = self._forward_gate_deadline(gate)
            self._check("admission", deadline)
            self.session = self.session_factory(self.profile)
            response = self.session.request("initialize", payload, self._remaining(deadline))
            self._record_gate("global-lock", started_epoch, started_ns, response=response)
        except BoundaryDecision as decision:
            assert self.recorder is not None
            result = self.recorder.record(
                "global-lock", None, started_epoch=started_epoch, started_ns=started_ns,
                error=RunnerError(decision.reason_code))
            raise GateFailure("global-lock", result.status, result.reason_code,
                              controller_recorded=decision.controller_recorded)
        except GateFailure:
            raise
        except BaseException as error:
            self._record_gate("global-lock", started_epoch, started_ns, error=error)

    def _exact_install(self) -> None:
        assert self.session is not None
        gate = next(item for item in self.gates if item["id"] == "exact-install")
        started_epoch, started_ns = int(time.time()), time.monotonic_ns()
        try:
            deadline = self._forward_gate_deadline(gate)
            self._check("cutover", deadline)
            self.controller.mark_mutation(timeout=min(10.0, self._remaining(deadline)))
            self.mutated = True
            arm = self.session.request("arm-mutation", {}, min(20.0, self._remaining(deadline)))
            if arm.get("status") != "PASS":
                raise RunnerError(arm.get("reason_code", "REMOTE_RECOVERY_ARM_FAILED"))
            remaining = self._remaining(deadline)
            if remaining <= 1:
                raise RunnerError("EXACT_INSTALL_BUDGET_EXHAUSTED")
            transfer_budget = max(1.0, min(30.0, remaining - 1))
            self.session.stage_candidate(
                self.packet.candidate_artifact, self.packet.manifest_value["release_id"],
                self.component["candidate"]["artifact_sha256"], transfer_budget)
            response = self.session.request("exact-install", {}, self._remaining(deadline))
            self._record_gate("exact-install", started_epoch, started_ns, response=response)
        except BoundaryDecision as decision:
            assert self.recorder is not None
            result = self.recorder.record(
                "exact-install", None, started_epoch=started_epoch, started_ns=started_ns,
                error=RunnerError(decision.reason_code))
            raise GateFailure("exact-install", result.status, result.reason_code,
                              controller_recorded=decision.controller_recorded)
        except GateFailure:
            raise
        except BaseException as error:
            self._record_gate("exact-install", started_epoch, started_ns, error=error)

    def _write_terminal_evidence(self, role: str) -> tuple[pathlib.Path, pathlib.Path]:
        assert self.recorder is not None
        identity = ({"commit": "0" * 40, "tree": "0" * 40, "artifact_sha256": "0" * 64}
                    if role == "unknown" else self.component[role])
        live = {
            "schema_version": "1.0", "release_id": self.packet.manifest_value["release_id"],
            "components": [{"id": "prd-studio", **identity}],
        }
        live_path = self.attempt_dir / f"live-identities-{role}.json"
        summary_path = self.attempt_dir / f"gate-summary-{role}.json"
        write_exclusive(live_path, live)
        write_exclusive(summary_path, self.recorder.summary(sha256_file(self.packet.manifest)))
        return live_path, summary_path

    def _finish_without_mutation(self, failure: GateFailure) -> str:
        role = "expected_live"
        live, summary = self._write_terminal_evidence(role)
        outcome = ("rejected_at_admission"
                   if PHASE_BY_GATE[failure.gate_id] == "admission"
                   else "halted_before_mutation")
        self.controller.finish(outcome, live, summary, state_changed=False,
                               rollback_ran=False, outage_seconds=0)
        return outcome

    def _rollback(self, failure: GateFailure, *, controller_recorded: bool = False) -> str:
        assert self.session is not None and self.recorder is not None
        rollback_gate = next(item for item in self.gates if item["id"] == "rollback-verification")
        started_epoch, started_ns = int(time.time()), time.monotonic_ns()
        deadline = min(time.monotonic() + rollback_gate["timeout_seconds"],
                       time.monotonic() + max(0.0, self._slot_remaining(900)))
        response: dict[str, Any] | None = None
        error: BaseException | None = None
        bookkeeping_error: BaseException | None = None
        try:
            if not controller_recorded:
                self.controller.record_failure(
                    failure.gate_id, failure.reason_code,
                    timeout=min(10.0, self._remaining(deadline)))
            self.failure_recorded = True
            self.controller.begin_rollback(timeout=min(10.0, self._remaining(deadline)))
            self._check("rollback", deadline)
        except BaseException as caught:
            bookkeeping_error = caught
        try:
            rollback = self.session.request("rollback", {}, self._remaining(deadline))
            if rollback.get("status") != "PASS":
                raise RunnerError(rollback.get("reason_code", "ROLLBACK_OPERATION_FAILED"))
            response = self.session.request(
                "rollback-verification", {}, self._remaining(deadline))
            if response.get("status") != "PASS":
                raise RunnerError(response.get("reason_code", "ROLLBACK_VERIFICATION_FAILED"))
        except BaseException as caught:
            error = caught
        if error is None and bookkeeping_error is not None:
            error = RunnerError("CONTROLLER_ROLLBACK_BOOKKEEPING_FAILED")
        result = self.recorder.record(
            "rollback-verification", response, started_epoch=started_epoch,
            started_ns=started_ns, error=error)
        if result.status == "PASS":
            live, summary = self._write_terminal_evidence("rollback")
            self.controller.finish("rolled_back", live, summary, state_changed=True,
                                   rollback_ran=True, outage_seconds=0,
                                   timeout=min(20.0, self._remaining(deadline)))
            self.controller_terminal = True
            return "rolled_back"
        try:
            self.controller.record_rollback_failure(
                result.reason_code, timeout=min(10.0, self._remaining(deadline)))
            live, summary = self._write_terminal_evidence("unknown")
            self.controller.finish("incident_recovery_continues", live, summary,
                                   state_changed=True, rollback_ran=True, outage_seconds=0,
                                   timeout=min(20.0, self._remaining(deadline)))
            self.controller_terminal = True
        except BaseException:
            # Remote recovery/containment already ran; preserve the first failure and fail closed.
            pass
        return "incident_recovery_continues"

    def execute(self) -> str:
        raise RunnerError("RUNNER_EXECUTION_NOT_CERTIFIED")

        # Unreachable until the recovery protocol receives independent fault
        # certification.  Kept as reviewable implementation material only.
        self.attempt_dir.mkdir(mode=0o700)
        self.controller.validate()
        # The controller receipt is started immediately before the real remote lock attempt.
        self.controller.start()
        receipt = load_json(self.receipt)
        self.receipt_value = receipt
        self.recorder = GateRecorder(
            self.attempt_dir, self.packet.manifest_value["release_id"], self.gates,
            receipt["started_epoch"])
        assets, fixture = load_assets()
        payload = {
            "profile": self.profile.value,
            "manifest": self.packet.manifest_value,
            "overlay": self.packet.overlay_value,
            "assets": assets,
            "fixture": fixture,
        }
        current_gate = "global-lock"
        try:
            self._global_lock(payload)
            for gate_id in ("live-baseline", "configuration-contract", "private-backup"):
                current_gate = gate_id
                self._remote_gate(gate_id)
            current_gate = "exact-install"
            self._exact_install()
            for gate_id in (
                "state-transition", "live-identity", "serving-topology",
                "health-readiness", "auth-contract", "bounded-logs",
                "out-of-scope-unchanged", "prd-studio-crud-smoke",
            ):
                current_gate = gate_id
                self._remote_gate(gate_id)
            assert self.session is not None
            terminal_deadline = time.monotonic() + min(50.0, max(0.0, self._slot_remaining(720)))
            if self._slot_remaining(720) < 50:
                raise BoundaryDecision("FORWARD_TERMINAL_BUDGET_UNAVAILABLE")
            self._check("verification", terminal_deadline)
            terminal_started_epoch, terminal_started_ns = int(time.time()), time.monotonic_ns()
            prepared = self.session.request(
                "prepare-success", {}, min(15.0, self._remaining(terminal_deadline)))
            if prepared.get("status") != "PASS":
                self._record_gate(
                    current_gate, terminal_started_epoch, terminal_started_ns,
                    error=RunnerError(prepared.get("reason_code", "SUCCESS_PREPARE_FAILED")))
            live, summary = self._write_terminal_evidence("candidate")
            in_doubt = self.session.request(
                "enter-commit-in-doubt", {}, min(8.0, self._remaining(terminal_deadline)))
            if in_doubt.get("status") != "PASS":
                self._record_gate(
                    current_gate, terminal_started_epoch, terminal_started_ns,
                    error=RunnerError(in_doubt.get(
                        "reason_code", "GLOBAL_COMMIT_INTENT_FAILED")))
            self.controller.finish("deployed_verified", live, summary,
                                   state_changed=True, rollback_ran=False, outage_seconds=0,
                                   timeout=min(20.0, self._remaining(terminal_deadline)))
            self.controller_terminal = True
            finalized = self.session.request(
                "finalize-success", {}, min(12.0, self._remaining(terminal_deadline)))
            if finalized.get("status") != "PASS":
                raise RunnerError(finalized.get(
                    "reason_code", "POST_TERMINAL_FINALIZATION_FAILED"))
            return "deployed_verified"
        except GateFailure as failure:
            if self.mutated:
                return self._rollback(
                    failure, controller_recorded=failure.controller_recorded)
            return self._finish_without_mutation(failure)
        except BoundaryDecision as decision:
            assert self.recorder is not None
            started_epoch, started_ns = int(time.time()), time.monotonic_ns()
            result = self.recorder.record(
                current_gate, None, started_epoch=started_epoch, started_ns=started_ns,
                error=RunnerError(decision.reason_code))
            failure = GateFailure(current_gate, result.status, result.reason_code)
            if self.mutated:
                return self._rollback(failure, controller_recorded=decision.controller_recorded)
            return self._finish_without_mutation(failure)
        except BaseException as error:
            if self.controller_terminal:
                # The canonical terminal record cannot be rewritten.  The remote
                # in-doubt protocol remains durably fenced and enters containment
                # on close; surface reconciliation instead of inventing rollback.
                raise RunnerError("POST_TERMINAL_FINALIZATION_REQUIRES_RECONCILIATION") from error
            if self.recorder is None:
                raise
            reason = error.reason_code if isinstance(error, RunnerError) else "SUPERVISOR_UNEXPECTED"
            started_epoch, started_ns = int(time.time()), time.monotonic_ns()
            try:
                result = self.recorder.record(
                    current_gate, None, started_epoch=started_epoch, started_ns=started_ns,
                    error=RunnerError(reason))
                failure = GateFailure(current_gate, result.status, result.reason_code)
            except BaseException:
                failure = GateFailure(current_gate, "ERROR", "SUPERVISOR_UNEXPECTED")
            if self.mutated and self.session is not None:
                return self._rollback(failure)
            return self._finish_without_mutation(failure)
        finally:
            if self.session is not None:
                self.session.close()

    def _canonical_success_result(self) -> dict[str, Any] | None:
        result_path = self.attempt_dir / f"{self.packet.manifest_value['release_id']}.result.json"
        if not result_path.exists():
            return None
        result = load_json(result_path)
        candidate = self.component["candidate"]
        live = result.get("live_components")
        if (result.get("schema_version") != "1.0"
                or result.get("release_id") != self.packet.manifest_value["release_id"]
                or result.get("manifest_sha256") != sha256_file(self.packet.manifest)
                or result.get("runner_sha256") != sha256_file(self.runner_file)
                or result.get("outcome") != "deployed_verified"
                or not isinstance(live, list) or len(live) != 1
                or live[0] != {"id": "prd-studio", **candidate}):
            raise RunnerError("CANONICAL_SUCCESS_RESULT_INVALID")
        return result

    def reconcile(self) -> str:
        """Resolve a durable global-commit-in-doubt state from the canonical receipt."""
        self.receipt_value = load_json(self.receipt)
        assets, fixture = load_assets()
        payload = {"profile": self.profile.value, "manifest": self.packet.manifest_value,
                   "overlay": self.packet.overlay_value, "assets": assets, "fixture": fixture}
        self.session = self.session_factory(self.profile)
        try:
            initialized = self.session.request("initialize-reconcile", payload, 20)
            if initialized.get("status") != "PASS":
                raise RunnerError(initialized.get("reason_code", "RECONCILE_INITIALIZE_FAILED"))
            # Re-read after the participant lock is held.  command_finish uses an
            # exclusive, fsynced result file, so existence is an atomic decision.
            result = self._canonical_success_result()
            if result is not None:
                finalized = self.session.request("finalize-success", {}, 30)
                if finalized.get("status") != "PASS":
                    raise RunnerError(finalized.get("reason_code", "RECONCILE_FINALIZE_FAILED"))
                return "deployed_verified"
            return self._reconcile_nonterminal_rollback()
        finally:
            if self.session is not None:
                self.session.close()

    def _reconcile_nonterminal_rollback(self) -> str:
        assert self.session is not None and self.receipt_value is not None
        deadline = min(time.monotonic() + 120,
                       time.monotonic() + max(0.0, self._slot_remaining(900)))
        failed_gate = "prd-studio-crud-smoke"
        reason = "GLOBAL_COMMIT_NOT_TERMINAL"
        self.controller.record_failure(failed_gate, reason,
                                       timeout=min(10.0, self._remaining(deadline)))
        self.controller.begin_rollback(timeout=min(10.0, self._remaining(deadline)))
        rollback = self.session.request("rollback", {}, self._remaining(deadline))
        if rollback.get("status") != "PASS":
            raise RunnerError(rollback.get("reason_code", "RECONCILE_ROLLBACK_FAILED"))
        verified = self.session.request("rollback-verification", {}, self._remaining(deadline))
        if verified.get("status") != "PASS":
            raise RunnerError(verified.get("reason_code", "RECONCILE_ROLLBACK_VERIFY_FAILED"))

        summary_source = self.attempt_dir / "gate-summary-candidate.json"
        summary = load_json(summary_source)
        now = int(time.time())
        evidence_dir = self.attempt_dir / "evidence"
        failure_document = {
            "schema_version": "1.0", "release_id": summary["release_id"],
            "gate_id": failed_gate, "gate_version": "1.0.0", "status": "ERROR",
            "reason_code": reason, "evidence": {"global_commit_terminal": False},
        }
        failure_path = evidence_dir / "global-commit-reconcile.json"
        failure_sha = write_exclusive(failure_path, failure_document)
        rollback_document = {
            "schema_version": "1.0", "release_id": summary["release_id"],
            "gate_id": "rollback-verification", "gate_version": "1.0.0",
            "status": "PASS", "reason_code": verified["reason_code"],
            "evidence": verified["evidence"],
        }
        rollback_path = evidence_dir / "rollback-verification.reconcile.json"
        rollback_sha = write_exclusive(rollback_path, rollback_document)
        for item in summary["results"]:
            if item["id"] == failed_gate:
                item.update({"status": "ERROR", "reason_code": reason,
                             "started_epoch": now, "finished_epoch": now,
                             "duration_ms": 1, "attempts": 1,
                             "evidence_sha256": failure_sha,
                             "evidence_reference": "evidence/global-commit-reconcile.json"})
            elif item["id"] == "rollback-verification":
                item.update({"status": "PASS", "reason_code": verified["reason_code"],
                             "started_epoch": now, "finished_epoch": now,
                             "duration_ms": 1, "attempts": 1,
                             "evidence_sha256": rollback_sha,
                             "evidence_reference":
                                 "evidence/rollback-verification.reconcile.json"})
        summary_path = self.attempt_dir / "gate-summary-reconcile-rollback.json"
        write_exclusive(summary_path, summary)
        live_path = self.attempt_dir / "live-identities-reconcile-rollback.json"
        write_exclusive(live_path, {
            "schema_version": "1.0", "release_id": summary["release_id"],
            "components": [{"id": "prd-studio", **self.component["rollback"]}],
        })
        self.controller.finish("rolled_back", live_path, summary_path,
                               state_changed=True, rollback_ran=True, outage_seconds=0,
                               timeout=min(20.0, self._remaining(deadline)))
        self.controller_terminal = True
        return "rolled_back"
