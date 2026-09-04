from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))

from prd_studio_deploy.constants import GATE_ORDER, PHASE_BY_GATE
from prd_studio_deploy.errors import ChildTimeout, RunnerError
from prd_studio_deploy.evidence import assert_release_evidence_safe
from prd_studio_deploy.gate_runtime import evaluate_certification_case
from prd_studio_deploy.process import run_bounded
from prd_studio_deploy.records import assert_secret_free
from prd_studio_deploy.supervisor import DeploymentSupervisor

WORKER_SPEC = importlib.util.spec_from_file_location(
    "prd_studio_remote_worker", ROOT / "runner/prd_studio_deploy/remote_worker.py")
assert WORKER_SPEC and WORKER_SPEC.loader
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)
BUILDER_SPEC = importlib.util.spec_from_file_location(
    "prd_studio_candidate_builder", ROOT / "tools/build_candidate_artifact.py")
assert BUILDER_SPEC and BUILDER_SPEC.loader
candidate_builder = importlib.util.module_from_spec(BUILDER_SPEC)
BUILDER_SPEC.loader.exec_module(candidate_builder)
ABSENCE_SPEC = importlib.util.spec_from_file_location(
    "prd_studio_absence_builder", ROOT / "tools/build_absence_artifact.py")
assert ABSENCE_SPEC and ABSENCE_SPEC.loader
absence_builder = importlib.util.module_from_spec(ABSENCE_SPEC)
ABSENCE_SPEC.loader.exec_module(absence_builder)


class CertificationBoundaryTests(unittest.TestCase):
    def test_known_good_pass(self) -> None:
        self.assertEqual(evaluate_certification_case(
            {"status": "PASS", "reason_code": "KNOWN_GOOD", "evidence": {}}),
            ("PASS", "KNOWN_GOOD"))

    def test_known_bad_fail(self) -> None:
        self.assertEqual(evaluate_certification_case(
            {"status": "FAIL", "reason_code": "KNOWN_BAD", "evidence": {}}),
            ("FAIL", "KNOWN_BAD"))

    def test_missing_is_error(self) -> None:
        self.assertEqual(evaluate_certification_case(None),
                         ("ERROR", "MISSING_GATE_EVIDENCE"))

    def test_timeout_is_error(self) -> None:
        self.assertEqual(evaluate_certification_case(None, ChildTimeout()),
                         ("ERROR", "GATE_TIMEOUT"))


class ProcessBoundaryTests(unittest.TestCase):
    def test_hung_process_group_is_bounded(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ChildTimeout):
            run_bounded(["/usr/bin/python3", "-c",
                         "import os,time;\n"
                         "p=os.fork();\n"
                         "time.sleep(60) if p==0 else time.sleep(60)"],
                        timeout_seconds=0.2)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_output_limit_kills_noisy_child(self) -> None:
        with self.assertRaisesRegex(RunnerError, "CHILD_OUTPUT_LIMIT_EXCEEDED"):
            run_bounded(["/usr/bin/python3", "-c",
                         "import os; os.write(1,b'x'*131072)"],
                        timeout_seconds=2, max_output_bytes=4096)

    def test_lock_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = pathlib.Path(raw) / "lock"
            path.touch(mode=0o600)
            first = os.open(path, os.O_RDWR)
            second = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(second)
                os.close(first)


class SecretBoundaryTests(unittest.TestCase):
    def test_secret_in_array_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunnerError, "PROTECTED_FIELD_IN_OUTPUT"):
            assert_secret_free({"items": [{"password": "not-emitted"}]})

    def test_placeholder_in_array_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunnerError, "PROTECTED_OR_PLACEHOLDER"):
            assert_secret_free({"items": ["TODO"]})

    def test_release_evidence_rejects_raw_body_and_url(self) -> None:
        with self.assertRaises(RunnerError):
            assert_release_evidence_safe({"schema_version": "1.0", "raw_body": "x"})
        with self.assertRaises(RunnerError):
            assert_release_evidence_safe({"schema_version": "1.0", "location": "https://private"})


class ResetGuardTests(unittest.TestCase):
    schema = "a" * 64
    synthetic = "deploy-smoke-123"

    def snapshot(self, *, projects: int = 0, synthetic: int = 0,
                 valid: int = 0, payload_match: bool = True) -> dict[str, object]:
        return {
            "database_exists": True, "table_count": 2, "known_table_count": 2,
            "schema_row_count": 1, "schema_version": 1,
            "schema_checksum": self.schema, "project_count": projects,
            "synthetic_count": synthetic, "synthetic_valid_count": valid,
            "synthetic_payload_match": payload_match,
        }

    def test_absent_empty_and_exact_synthetic_pass(self) -> None:
        self.assertTrue(worker.evaluate_reset_guard(
            {"database_exists": False}, self.schema, self.synthetic)[0])
        self.assertEqual(worker.evaluate_reset_guard(
            self.snapshot(), self.schema, self.synthetic), (True, "RESET_GUARD_EMPTY"))
        self.assertEqual(worker.evaluate_reset_guard(
            self.snapshot(projects=1, synthetic=1, valid=1), self.schema, self.synthetic),
            (True, "RESET_GUARD_SYNTHETIC_ONLY"))

    def test_altered_or_foreign_data_fails(self) -> None:
        self.assertFalse(worker.evaluate_reset_guard(
            self.snapshot(projects=1, synthetic=1, valid=1, payload_match=False),
            self.schema, self.synthetic)[0])
        changed = self.snapshot()
        changed["schema_checksum"] = "b" * 64
        self.assertFalse(worker.evaluate_reset_guard(changed, self.schema, self.synthetic)[0])
        self.assertFalse(worker.evaluate_reset_guard(
            {"database_exists": True}, self.schema, self.synthetic)[0])


class StaticContractTests(unittest.TestCase):
    def test_gate_set_order_and_budgets(self) -> None:
        gates = {}
        for path in (ROOT / "gates").glob("*.json"):
            value = json.loads(path.read_text())
            gates[value["id"]] = value
        self.assertEqual(set(gates), set(GATE_ORDER))
        self.assertEqual([gates[item]["timeout_seconds"] for item in GATE_ORDER],
                         [15, 25, 20, 60, 55, 45, 20, 25, 30, 25, 25, 35, 30, 120])
        self.assertEqual({item: gates[item]["phase"] for item in gates}, PHASE_BY_GATE)
        for gate in gates.values():
            self.assertEqual(gate["max_attempts"], 1)
            self.assertEqual(gate["secret_policy"], "names-and-digests-only")

    def test_service_has_durable_non_network_fence(self) -> None:
        service = (ROOT / "templates/prd-studio.service").read_text()
        self.assertIn("WRITE_FENCE_FILE=/var/lib/prd-studio/deployment-control/write-fence", service)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", service)
        self.assertNotIn("AF_INET", service)
        self.assertIn("Group=prd-studio-socket", service)
        self.assertIn("SupplementaryGroups=prd-studio", service)

    def test_nginx_auth_and_trust_boundary(self) -> None:
        nginx = (ROOT / "templates/prd-studio.nginx.conf").read_text()
        self.assertIn("auth_basic_user_file", nginx)
        self.assertIn("limit_req zone=prd_studio_auth", nginx)
        self.assertIn('proxy_set_header Authorization "";', nginx)
        self.assertIn("proxy_set_header X-PRD-Authenticated 1;", nginx)
        self.assertIn("proxy_pass http://unix:/run/prd-studio/http.sock:;", nginx)

    def test_schema_runner_and_exact_runtime_grants(self) -> None:
        source = (ROOT / "runner/prd_studio_deploy/remote_worker.py").read_text()
        self.assertIn('run_schema("schema_applied")', source)
        self.assertIn('run_schema("schema_already_current")', source)
        self.assertIn("GRANT SELECT ON prd_studio.schema_versions", source)
        self.assertIn("GRANT SELECT,INSERT,UPDATE ON prd_studio.projects", source)
        self.assertNotIn("GRANT SELECT,INSERT,UPDATE ON prd_studio.*", source)

    def test_live_execution_is_explicitly_not_certified(self) -> None:
        entrypoint = (ROOT / "runner/prd_studio_deploy/__main__.py").read_text()
        supervisor = (ROOT / "runner/prd_studio_deploy/supervisor.py").read_text()
        self.assertIn('raise RunnerError("RUNNER_EXECUTION_NOT_CERTIFIED")', entrypoint)
        self.assertIn('raise RunnerError("RUNNER_EXECUTION_NOT_CERTIFIED")', supervisor)

    def test_entrypoint_execute_fails_before_external_inputs(self) -> None:
        import prd_studio_deploy.__main__ as entrypoint
        output = StringIO()
        with tempfile.TemporaryDirectory() as raw:
            absent = pathlib.Path(raw) / "must-not-be-created"
            with mock.patch.object(
                    entrypoint, "load_packet",
                    side_effect=AssertionError("execute must not load a packet")), \
                    redirect_stdout(output):
                code = entrypoint.main([
                    "execute", "--packet", str(absent / "packet"),
                    "--profile", str(absent / "profile.json"),
                    "--approval", str(absent / "approval.json"),
                    "--attempt-dir", str(absent / "attempt"),
                ])
            self.assertFalse(absent.exists())
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {
            "status": "ERROR", "reason_code": "RUNNER_EXECUTION_NOT_CERTIFIED"})

    def test_worker_and_reconcile_fail_before_input_or_dispatch(self) -> None:
        class ForbiddenStdin:
            @property
            def buffer(self):
                raise AssertionError("disabled worker must not read stdin")

        output = StringIO()
        with mock.patch.object(worker.sys, "stdin", ForbiddenStdin()), redirect_stdout(output):
            self.assertEqual(worker.main(), 2)
        self.assertEqual(json.loads(output.getvalue()), {
            "status": "ERROR", "reason_code": "RUNNER_EXECUTION_NOT_CERTIFIED"})

        instance = worker.Worker()
        with self.assertRaisesRegex(worker.WorkerError, "RUNNER_EXECUTION_NOT_CERTIFIED"):
            instance.request("initialize", {})

        supervisor = object.__new__(DeploymentSupervisor)
        with self.assertRaisesRegex(RunnerError, "RUNNER_EXECUTION_NOT_CERTIFIED"):
            supervisor.reconcile()

    def test_public_requests_never_follow_redirects(self) -> None:
        handler = worker._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {},
                                                   "https://untrusted.invalid/"))


class DeterministicRunnerTests(unittest.TestCase):
    def test_runner_build_is_deterministic_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            first = pathlib.Path(raw) / "first.pyz"
            second = pathlib.Path(raw) / "second.pyz"
            tool = ROOT / "tools/build_runner.py"
            for output in (first, second):
                subprocess.run([sys.executable, str(tool), "--output", str(output)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(),
                             hashlib.sha256(second.read_bytes()).digest())
            result = subprocess.run([str(first), "--version"], check=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(result.stdout.strip(), "1.0.0")

    def test_absence_artifact_is_deterministic_and_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            outputs = []
            for index in (1, 2):
                artifact, overlay = root / f"absence-{index}.json", root / f"overlay-{index}.json"
                digests = absence_builder.build("a" * 40, "b" * 40, artifact, overlay)
                outputs.append((artifact.read_bytes(), overlay.read_bytes(), digests))
            self.assertEqual(outputs[0], outputs[1])
            overlay_value = json.loads(outputs[0][1])
            self.assertEqual(overlay_value["parent"], {"commit": "a" * 40, "tree": "b" * 40})
            self.assertEqual(overlay_value["absence_artifact_sha256"], outputs[0][2][0])


class CandidateBuildBoundaryTests(unittest.TestCase):
    def staged_metadata(self, root: pathlib.Path) -> pathlib.Path:
        studio = root / "studio"
        studio.mkdir(parents=True)
        shutil.copyfile(ROOT.parents[1] / "studio/package.json", studio / "package.json")
        shutil.copyfile(ROOT.parents[1] / "studio/package-lock.json", studio / "package-lock.json")
        return studio

    def test_current_dependency_contract_passes(self) -> None:
        self.assertRegex(candidate_builder._validate_dependency_contract(
            ROOT.parents[1] / "studio"), r"^[0-9a-f]{64}$")

    def test_local_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            studio = self.staged_metadata(pathlib.Path(raw))
            package = json.loads((studio / "package.json").read_text())
            package["dependencies"]["express"] = "file:/etc"
            (studio / "package.json").write_text(json.dumps(package))
            with self.assertRaisesRegex(ValueError, "PACKAGE_CONTRACT_INVALID"):
                candidate_builder._validate_dependency_contract(studio)

    def test_non_registry_or_link_lock_material_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            studio = self.staged_metadata(pathlib.Path(raw))
            lock = json.loads((studio / "package-lock.json").read_text())
            entry = next(value for key, value in lock["packages"].items() if key)
            entry["resolved"] = "https://unapproved.invalid/archive.tgz"
            (studio / "package-lock.json").write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, "PACKAGE_LOCK_MATERIAL_UNAPPROVED"):
                candidate_builder._validate_dependency_contract(studio)
            studio = self.staged_metadata(pathlib.Path(raw) / "second")
            lock = json.loads((studio / "package-lock.json").read_text())
            entry = next(value for key, value in lock["packages"].items() if key)
            entry["link"] = True
            (studio / "package-lock.json").write_text(json.dumps(lock))
            with self.assertRaisesRegex(ValueError, "PACKAGE_LOCK_LOCATION_INVALID"):
                candidate_builder._validate_dependency_contract(studio)

    def test_builder_requires_linux_x86_64_and_pinned_tools(self) -> None:
        source = (ROOT / "tools/build_candidate_artifact.py").read_text()
        self.assertIn('sys.platform != "linux"', source)
        self.assertIn('os.uname().machine != "x86_64"', source)
        self.assertIn('expected_npm != "10.9.7"', source)


if __name__ == "__main__":
    unittest.main()
