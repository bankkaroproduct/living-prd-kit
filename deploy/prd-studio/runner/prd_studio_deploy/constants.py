"""Versioned paths and semantic gate identities."""

from __future__ import annotations

import pathlib

RUNNER_ID = "prd-studio-protected-staging-runner"
RUNNER_VERSION = "1.0.0"
CANONICAL_CONTROLLER = pathlib.Path(
    "/Users/mohsin/Downloads/Deployment/deployment_process/deployment_slot.py"
)
COMPONENT_ID = "prd-studio"
BASE_PATH = "/prd-studio"
SERVICE_NAME = "prd-studio.service"
APP_ROOT = pathlib.PurePosixPath("/opt/prd-studio")
RELEASES_ROOT = APP_ROOT / "releases"
CURRENT_LINK = APP_ROOT / "current"
RUNTIME_DIRECTORY = pathlib.PurePosixPath("/run/prd-studio")
HTTP_SOCKET = RUNTIME_DIRECTORY / "http.sock"
WRITE_FENCE_DIRECTORY = pathlib.PurePosixPath("/var/lib/prd-studio/deployment-control")
WRITE_FENCE = WRITE_FENCE_DIRECTORY / "write-fence"
RELEASE_ENV = pathlib.PurePosixPath("/etc/prd-studio/release.env")
PRIVATE_APP_ENV = pathlib.PurePosixPath("/etc/prd-studio/prd-studio.env")
SYSTEMD_UNIT = pathlib.PurePosixPath("/etc/systemd/system/prd-studio.service")
GLOBAL_LOCK = pathlib.PurePosixPath("/var/lock/bankkaro-protected-staging.lock")
PRIVATE_STATE_ROOT = pathlib.PurePosixPath("/var/lib/prd-studio")
STAGING_ROOT = PRIVATE_STATE_ROOT / "deployment-staging"
BACKUP_ROOT = PRIVATE_STATE_ROOT / "deployment-backups"
DATABASE_NAME = "prd_studio"

GATE_ORDER = (
    "global-lock",
    "live-baseline",
    "configuration-contract",
    "private-backup",
    "exact-install",
    "state-transition",
    "live-identity",
    "serving-topology",
    "health-readiness",
    "auth-contract",
    "bounded-logs",
    "out-of-scope-unchanged",
    "prd-studio-crud-smoke",
    "rollback-verification",
)

PHASE_BY_GATE = {
    "global-lock": "admission",
    "live-baseline": "admission",
    "configuration-contract": "admission",
    "private-backup": "backup",
    "exact-install": "cutover",
    "state-transition": "cutover",
    "live-identity": "cutover",
    "serving-topology": "cutover",
    "health-readiness": "cutover",
    "auth-contract": "cutover",
    "bounded-logs": "verification",
    "out-of-scope-unchanged": "verification",
    "prd-studio-crud-smoke": "verification",
    "rollback-verification": "rollback",
}

OUTCOME_LABELS = {
    "rejected_at_admission": "REJECTED_AT_ADMISSION — RETURNED TO DEVELOPMENT",
    "halted_before_mutation": "HALTED BEFORE MUTATION — NO RELEASE",
    "deployed_verified": "DEPLOYED + VERIFIED",
    "rolled_back": "ROLLED BACK — NO RELEASE",
    "incident_recovery_continues": "INCIDENT — RECOVERY CONTINUES",
}
