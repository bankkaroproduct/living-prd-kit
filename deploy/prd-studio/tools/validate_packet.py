#!/usr/bin/env python3
"""Offline packet-integrity check; live admission remains the canonical controller's job."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from prd_studio_deploy.package import load_packet  # noqa: E402
from prd_studio_deploy.records import sha256_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("packet", type=pathlib.Path)
    args = parser.parse_args()
    packet = load_packet(args.packet)
    print(json.dumps({"status": "PASS", "release_id": packet.manifest_value["release_id"],
                      "manifest_sha256": sha256_file(packet.manifest),
                      "runner_sha256": sha256_file(packet.runner),
                      "note": "OFFLINE_INTEGRITY_ONLY_CANONICAL_ADMISSION_STILL_REQUIRED"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
