"""SSH transport for the continuously locked standalone remote worker."""

from __future__ import annotations

import base64
import importlib.resources
import json
import os
import selectors
import shlex
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

from .errors import ChildTimeout, RunnerError
from .process import run_bounded, terminate_process_group
from .profile import ConnectionProfile
from .records import assert_secret_free, canonical_json_bytes, sha256_file


def _remote_source() -> bytes:
    return importlib.resources.files("prd_studio_deploy").joinpath("remote_worker.py").read_bytes()


def _worker_command() -> str:
    encoded = base64.b85encode(zlib.compress(_remote_source(), level=9)).decode("ascii")
    loader = (
        "import base64,zlib;"
        f"exec(compile(zlib.decompress(base64.b85decode({encoded!r})),"
        "'prd-studio-remote-worker','exec'))"
    )
    return "/usr/bin/python3 -I -u -c " + shlex.quote(loader)


class RemoteSession:
    """One SSH process whose remote worker owns the flock for its whole life."""

    def __init__(self, profile: ConnectionProfile):
        self.profile = profile
        argv = profile.ssh_argv() + [_worker_command()]
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None:
            terminate_process_group(self.process)
            raise RunnerError("REMOTE_SESSION_PIPE_FAILED")
        self._buffer = bytearray()
        self._stderr_bytes = 0
        self.closed = False
        self.mutation_possible = False

    def _write(self, payload: bytes, deadline: float) -> None:
        assert self.process.stdin is not None
        fd = self.process.stdin.fileno()
        view = memoryview(payload)
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_WRITE)
        try:
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.abort(grace_seconds=0.5)
                    raise ChildTimeout()
                if not selector.select(min(remaining, 0.1)):
                    if self.process.poll() is not None:
                        raise RunnerError("REMOTE_SESSION_EXITED")
                    continue
                try:
                    count = os.write(fd, view[:65536])
                except BrokenPipeError as error:
                    raise RunnerError("REMOTE_SESSION_BROKEN_PIPE") from error
                if count <= 0:
                    raise RunnerError("REMOTE_SESSION_WRITE_FAILED")
                view = view[count:]
        finally:
            selector.close()

    def _read_line(self, deadline: float) -> bytes:
        assert self.process.stdout is not None and self.process.stderr is not None
        fd = self.process.stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ, "stdout")
        selector.register(self.process.stderr.fileno(), selectors.EVENT_READ, "stderr")
        try:
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.abort(grace_seconds=0.5)
                    raise ChildTimeout()
                events = selector.select(min(remaining, 0.1))
                if not events:
                    if self.process.poll() is not None:
                        raise RunnerError("REMOTE_SESSION_EXITED")
                    continue
                key = events[0][0]
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fd)
                    if key.data == "stdout":
                        raise RunnerError("REMOTE_SESSION_EOF")
                    continue
                if key.data == "stderr":
                    self._stderr_bytes += len(block)
                    if self._stderr_bytes > 64 * 1024:
                        self.abort(grace_seconds=0.5)
                        raise RunnerError("REMOTE_STDERR_OUTPUT_LIMIT")
                    continue
                self._buffer.extend(block)
                if len(self._buffer) > 1024 * 1024:
                    self.abort(grace_seconds=0.5)
                    raise RunnerError("REMOTE_PROTOCOL_OUTPUT_LIMIT")
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            return bytes(line)
        finally:
            selector.close()

    def request(self, op: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        if self.closed or timeout_seconds <= 0:
            raise RunnerError("REMOTE_SESSION_NOT_AVAILABLE")
        if op in {"arm-mutation", "initialize-reconcile"}:
            self.mutation_possible = True
        request = canonical_json_bytes({"op": op, "payload": payload})
        if len(request) > 1024 * 1024:
            raise RunnerError("REMOTE_PROTOCOL_REQUEST_LIMIT")
        deadline = time.monotonic() + timeout_seconds
        self._write(request, deadline)
        raw = self._read_line(deadline)
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RunnerError("REMOTE_PROTOCOL_RESPONSE_INVALID") from error
        if (not isinstance(response, dict) or set(response) != {"status", "reason_code", "evidence"}
                or response["status"] not in {"PASS", "FAIL", "ERROR"}
                or not isinstance(response["reason_code"], str)
                or not isinstance(response["evidence"], dict)):
            raise RunnerError("REMOTE_PROTOCOL_RESPONSE_INVALID")
        assert_secret_free(response)
        if (op in {"rollback-verification", "finalize-success"}
                and response.get("status") == "PASS"):
            self.mutation_possible = False
        return response

    def stage_candidate(self, source: Path, release_id: str, artifact_sha256: str,
                        timeout_seconds: float) -> None:
        if sha256_file(source) != artifact_sha256:
            raise RunnerError("LOCAL_CANDIDATE_HASH_MISMATCH")
        destination = f"/var/lib/prd-studio/deployment-staging/{release_id}/{artifact_sha256}.tar"
        result = run_bounded(self.profile.scp_argv(source, destination),
                             timeout_seconds=timeout_seconds)
        if result.returncode != 0:
            raise RunnerError("CANDIDATE_TRANSFER_FAILED")

    def abort(self, *, grace_seconds: float | None = None) -> None:
        if not self.closed:
            grace = (125 if self.mutation_possible else 0.5) if grace_seconds is None else grace_seconds
            terminate_process_group(self.process, grace_seconds=grace)
            self.closed = True

    def close(self, timeout_seconds: float | None = None) -> None:
        if self.closed:
            return
        budget = (125.0 if self.mutation_possible else 3.0) if timeout_seconds is None else timeout_seconds
        try:
            response = self.request("close", {}, budget)
            if response.get("status") != "PASS":
                raise RunnerError(response.get("reason_code", "REMOTE_CLOSE_FAILED"))
            self.mutation_possible = False
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                terminate_process_group(self.process)
        except RunnerError:
            if self.process.poll() is None:
                terminate_process_group(
                    self.process, grace_seconds=125 if self.mutation_possible else 0.5)
        finally:
            if self.process.poll() is None:
                terminate_process_group(self.process)
            self.closed = True

    def __enter__(self) -> "RemoteSession":
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self.close()
