"""Secret-free command line for the immutable PRD Studio deployment runner."""

from __future__ import annotations

import argparse
import json
import pathlib

from .constants import RUNNER_ID, RUNNER_VERSION
from .errors import RunnerError
from .package import load_packet
from .records import assert_secret_free, sanitized_error, sha256_file


def _emit(value: dict[str, object]) -> None:
    assert_secret_free(value)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=RUNNER_ID)
    parser.add_argument("--version", action="version", version=RUNNER_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("offline-validate")
    validate.add_argument("--packet", required=True, type=pathlib.Path)
    execute = commands.add_parser("execute")
    execute.add_argument("--packet", required=True, type=pathlib.Path)
    execute.add_argument("--profile", required=True, type=pathlib.Path)
    execute.add_argument("--approval", required=True, type=pathlib.Path)
    execute.add_argument("--attempt-dir", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        # Live mutation remains unavailable until the separate durable recovery
        # journal/controller-reconciliation design has independent fault
        # certification.  Keep this before profile, SSH, controller, or mkdir use.
        if args.command == "execute":
            raise RunnerError("RUNNER_EXECUTION_NOT_CERTIFIED")

        packet = load_packet(args.packet)
        _emit({"status": "PASS", "release_id": packet.manifest_value["release_id"],
               "manifest_sha256": sha256_file(packet.manifest),
               "runner_sha256": sha256_file(packet.runner),
               "scope": "OFFLINE_INTEGRITY_ONLY"})
        return 0

    except RunnerError as error:
        _emit(sanitized_error(error.reason_code))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        _emit(sanitized_error("RUNNER_INPUT_INVALID"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
