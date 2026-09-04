"""Bounded child-process execution with descendant termination."""

from __future__ import annotations

import dataclasses
import os
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence

from .errors import ChildTimeout, RunnerError


@dataclasses.dataclass(frozen=True)
class ChildResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int


def terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float = 0.5) -> None:
    """Terminate a child and every descendant in its new process group."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=max(grace_seconds, 0.1))
    except subprocess.TimeoutExpired:
        pass


def run_bounded(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdin: bytes | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int = 256 * 1024,
) -> ChildResult:
    """Run argv without a shell; timeouts kill the complete child process group."""
    if not argv or timeout_seconds <= 0:
        raise RunnerError("CHILD_ARGUMENT_INVALID")
    executable = os.path.abspath(argv[0])
    if executable != argv[0] or not executable.startswith(("/usr/bin/", "/bin/", "/usr/sbin/")):
        raise RunnerError("CHILD_EXECUTABLE_NOT_ABSOLUTE_ALLOWED")
    child_env = ({"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
                 if env is None else dict(env))
    started = time.monotonic_ns()
    input_file = tempfile.TemporaryFile(mode="w+b") if stdin is not None else None
    if input_file is not None:
        input_file.write(stdin or b"")
        input_file.seek(0)
    process = subprocess.Popen(
        list(argv), stdin=input_file if input_file is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
        env=child_env, start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(process)
                raise ChildTimeout()
            for key, _ in selector.select(min(remaining, 0.1)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                sink = output[key.data]
                sink.extend(block)
                if len(sink) > max_output_bytes:
                    terminate_process_group(process)
                    raise RunnerError("CHILD_OUTPUT_LIMIT_EXCEEDED")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_process_group(process)
            raise ChildTimeout()
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        terminate_process_group(process)
        raise ChildTimeout() from None
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if input_file is not None:
            input_file.close()
    duration_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    return ChildResult(process.returncode, bytes(output["stdout"]),
                       bytes(output["stderr"]), duration_ms)
