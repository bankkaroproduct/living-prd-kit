"""Stable, secret-free runner errors."""

from __future__ import annotations


class RunnerError(RuntimeError):
    """An expected fail-closed runner error with a stable reason code."""

    def __init__(self, reason_code: str, *, detail: str | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


class ChildTimeout(RunnerError):
    def __init__(self) -> None:
        super().__init__("CHILD_TIMEOUT")


class GateFailure(RunnerError):
    def __init__(self, gate_id: str, status: str, reason_code: str, *,
                 controller_recorded: bool = False) -> None:
        super().__init__(reason_code)
        self.gate_id = gate_id
        self.status = status
        self.controller_recorded = controller_recorded
