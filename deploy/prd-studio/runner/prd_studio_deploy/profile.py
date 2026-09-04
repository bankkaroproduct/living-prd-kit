"""Strict loading of the external, private connection profile."""

from __future__ import annotations

import copy
import os
import pathlib
import re
import stat
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .errors import RunnerError
from .records import (HEX64, canonical_json_bytes, load_json, load_json_with_digest,
                      sha256_bytes, sha256_file)

IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
SSH_TARGET = re.compile(r"^[A-Za-z0-9_.:@-]{1,255}$")
MODE = re.compile(r"^0[0-7]{3}$")
SAFE_REMOTE_PATH_PREFIXES = (
    "/etc/",
    "/opt/",
    "/run/",
    "/usr/",
    "/var/lib/",
    "/var/lock/",
)
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
PREFLIGHT_CHECKS = {
    "ssh-host-key",
    "remote-python",
    "global-lock",
    "service-account",
    "private-paths",
    "configuration-files",
    "database-admin",
    "trusted-proxy",
    "tls-route",
    "out-of-scope-probes",
    "managed-target-absence",
    "disconnect-recovery",
    "safe-observability",
    "nginx-activation-topology",
    "day2-backup-recovery",
}
PROTECTED_TARGET_HOST_SHA256 = "508299c7eaf9d2b668ea5dc32de447cfe65f57a63ecbd064b3d1fcfd4d2ea666"


def _exact_fields(value: Any, required: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise RunnerError(code)
    return value


def _private_regular(path: pathlib.Path, code: str) -> None:
    details = os.lstat(path)
    if (not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode)
            or details.st_nlink != 1 or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077):
        raise RunnerError(code)


def _absolute_local_path(raw: Any, code: str) -> pathlib.Path:
    if not isinstance(raw, str):
        raise RunnerError(code)
    path = pathlib.Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise RunnerError(code)
    return path


def _remote_path(raw: Any, code: str) -> str:
    if (not isinstance(raw, str) or not SAFE_REMOTE_PATH.fullmatch(raw)
            or not raw.startswith(SAFE_REMOTE_PATH_PREFIXES)):
        raise RunnerError(code)
    path = pathlib.PurePosixPath(raw)
    if not path.is_absolute() or ".." in path.parts or str(path) != raw:
        raise RunnerError(code)
    return raw


@dataclass(frozen=True)
class ConnectionProfile:
    path: pathlib.Path
    value: dict[str, Any]
    contract_sha256: str
    day2_backup_recovery: dict[str, Any]

    @property
    def ssh(self) -> dict[str, Any]:
        return self.value["ssh"]

    def ssh_argv(self) -> list[str]:
        ssh = self.ssh
        if (sha256_file(pathlib.Path(ssh["identity_file"])) != ssh["identity_sha256"]
                or sha256_file(pathlib.Path(ssh["known_hosts_file"]))
                != ssh["known_hosts_sha256"]):
            raise RunnerError("SSH_TRUST_FILE_CHANGED")
        return [
            "/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", str(ssh["port"]),
            "-i", ssh["identity_file"],
            "-o", "BatchMode=yes",
            "-o", "ClearAllForwardings=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={ssh['known_hosts_file']}",
            ssh["target"],
        ]

    def scp_argv(self, source: pathlib.Path, destination: str) -> list[str]:
        ssh = self.ssh
        if (sha256_file(pathlib.Path(ssh["identity_file"])) != ssh["identity_sha256"]
                or sha256_file(pathlib.Path(ssh["known_hosts_file"]))
                != ssh["known_hosts_sha256"]):
            raise RunnerError("SSH_TRUST_FILE_CHANGED")
        if not destination.startswith("/var/lib/prd-studio/deployment-staging/"):
            raise RunnerError("SCP_DESTINATION_OUTSIDE_STAGING")
        return [
            "/usr/bin/scp", "-F", "/dev/null", "-q", "-P", str(ssh["port"]),
            "-i", ssh["identity_file"],
            "-o", "BatchMode=yes",
            "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={ssh['known_hosts_file']}",
            str(source), f"{ssh['target']}:{destination}",
        ]


def _validate_profile(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    _exact_fields(value, {
        "schema_version", "profile_id", "environment", "ssh", "remote",
        "configuration", "managed_targets", "route", "out_of_scope", "preflight_certificate",
        "day2_backup_recovery",
    }, "PROFILE_FIELDS_INVALID")
    if value["schema_version"] != "1.0" or value["environment"] != "protected-staging":
        raise RunnerError("PROFILE_SCOPE_INVALID")
    if not isinstance(value["profile_id"], str) or not IDENTIFIER.fullmatch(value["profile_id"]):
        raise RunnerError("PROFILE_ID_INVALID")

    ssh = _exact_fields(value["ssh"], {
            "target", "port", "identity_file", "identity_sha256",
            "known_hosts_file", "known_hosts_sha256",
    }, "PROFILE_SSH_FIELDS_INVALID")
    if (not isinstance(ssh["target"], str) or ssh["target"].startswith("-")
            or not SSH_TARGET.fullmatch(ssh["target"])):
        raise RunnerError("PROFILE_SSH_TARGET_INVALID")
    if type(ssh["port"]) is not int or not 1 <= ssh["port"] <= 65535:
        raise RunnerError("PROFILE_SSH_PORT_INVALID")
    for key in ("identity_file", "known_hosts_file"):
        local = _absolute_local_path(ssh[key], "PROFILE_SSH_PATH_INVALID")
        _private_regular(local, "PROFILE_SSH_FILE_NOT_PRIVATE")
        digest_key = key.replace("_file", "_sha256")
        if (not isinstance(ssh[digest_key], str) or not HEX64.fullmatch(ssh[digest_key])
                or sha256_file(local) != ssh[digest_key]):
            raise RunnerError("PROFILE_SSH_FILE_HASH_MISMATCH")

    remote = _exact_fields(value["remote"], {
        "nginx_include_path", "nginx_http_include_path", "nginx_worker_user",
        "nginx_worker_group", "nginx_tls_server_path", "nginx_http_parent_path",
        "nginx_tls_server_anchor", "nginx_http_parent_anchor",
        "nginx_auth_file", "mysql_socket_path", "authorization_header_file", "tls_ca_file",
        "expected_absence_status", "machine_id_sha256",
        "mysql_server_uuid_sha256", "mysql_socket_identity",
    }, "PROFILE_REMOTE_FIELDS_INVALID")
    for key in (
        "nginx_include_path", "nginx_http_include_path", "nginx_tls_server_path",
        "nginx_http_parent_path", "nginx_auth_file",
        "mysql_socket_path", "authorization_header_file", "tls_ca_file",
    ):
        _remote_path(remote[key], "PROFILE_REMOTE_PATH_INVALID")
    if (not isinstance(remote["nginx_worker_user"], str)
            or not re.fullmatch(r"^[a-z_][a-z0-9_-]{0,31}$", remote["nginx_worker_user"])):
        raise RunnerError("PROFILE_NGINX_WORKER_USER_INVALID")
    if (not isinstance(remote["nginx_worker_group"], str)
            or not re.fullmatch(r"^[a-z_][a-z0-9_-]{0,31}$", remote["nginx_worker_group"])
            or remote["nginx_worker_group"] == "prd-studio"):
        raise RunnerError("PROFILE_NGINX_WORKER_GROUP_INVALID")
    for key in ("nginx_tls_server_anchor", "nginx_http_parent_anchor"):
        anchor = remote[key]
        if (not isinstance(anchor, str) or not 1 <= len(anchor) <= 256
                or "\n" in anchor or "\r" in anchor or "\x00" in anchor
                or "include" in anchor.lower()):
            raise RunnerError("PROFILE_NGINX_ANCHOR_INVALID")
    if (remote["nginx_tls_server_path"] == remote["nginx_http_parent_path"]
            or remote["nginx_include_path"] in {
                remote["nginx_tls_server_path"], remote["nginx_http_parent_path"]}
            or remote["nginx_http_include_path"] in {
                remote["nginx_tls_server_path"], remote["nginx_http_parent_path"]}):
        raise RunnerError("PROFILE_NGINX_PATH_TOPOLOGY_INVALID")
    if type(remote["expected_absence_status"]) is not int or remote["expected_absence_status"] not in {404, 410}:
        raise RunnerError("PROFILE_ABSENCE_STATUS_INVALID")
    if (not isinstance(remote["machine_id_sha256"], str)
            or not HEX64.fullmatch(remote["machine_id_sha256"])):
        raise RunnerError("PROFILE_MACHINE_ID_INVALID")
    if (not isinstance(remote["mysql_server_uuid_sha256"], str)
            or not HEX64.fullmatch(remote["mysql_server_uuid_sha256"])):
        raise RunnerError("PROFILE_MYSQL_SERVER_ID_INVALID")
    mysql_identity = _exact_fields(remote["mysql_socket_identity"], {
        "uid", "gid", "mode", "parent_uid", "parent_gid", "parent_mode",
    }, "PROFILE_MYSQL_SOCKET_IDENTITY_INVALID")
    for key in ("uid", "gid", "parent_uid", "parent_gid"):
        if type(mysql_identity[key]) is not int or mysql_identity[key] < 0:
            raise RunnerError("PROFILE_MYSQL_SOCKET_IDENTITY_INVALID")
    for key in ("mode", "parent_mode"):
        if not isinstance(mysql_identity[key], str) or not MODE.fullmatch(mysql_identity[key]):
            raise RunnerError("PROFILE_MYSQL_SOCKET_IDENTITY_INVALID")
    if int(mysql_identity["parent_mode"], 8) & 0o002:
        raise RunnerError("PROFILE_MYSQL_SOCKET_PARENT_WORLD_WRITABLE")

    configuration = value["configuration"]
    if not isinstance(configuration, list) or not 6 <= len(configuration) <= 16:
        raise RunnerError("PROFILE_CONFIGURATION_INVALID")
    config_ids: set[str] = set()
    config_paths: set[str] = set()
    for entry in configuration:
        _exact_fields(entry, {"id", "path", "sha256", "uid", "gid", "mode"},
                      "PROFILE_CONFIGURATION_ENTRY_INVALID")
        if (not isinstance(entry["id"], str) or not IDENTIFIER.fullmatch(entry["id"])
                or entry["id"] in config_ids):
            raise RunnerError("PROFILE_CONFIGURATION_ID_INVALID")
        config_ids.add(entry["id"])
        path = _remote_path(entry["path"], "PROFILE_CONFIGURATION_PATH_INVALID")
        if path in config_paths:
            raise RunnerError("PROFILE_CONFIGURATION_PATH_DUPLICATE")
        config_paths.add(path)
        if not isinstance(entry["sha256"], str) or not HEX64.fullmatch(entry["sha256"]):
            raise RunnerError("PROFILE_CONFIGURATION_HASH_INVALID")
        if type(entry["uid"]) is not int or entry["uid"] < 0 or type(entry["gid"]) is not int or entry["gid"] < 0:
            raise RunnerError("PROFILE_CONFIGURATION_OWNER_INVALID")
        if not isinstance(entry["mode"], str) or not MODE.fullmatch(entry["mode"]):
            raise RunnerError("PROFILE_CONFIGURATION_MODE_INVALID")
        numeric_mode = int(entry["mode"], 8)
        if numeric_mode & 0o022:
            raise RunnerError("PROFILE_CONFIGURATION_WRITABLE_BY_OTHERS")
        sensitive_paths = {
            remote["authorization_header_file"], remote["nginx_auth_file"],
        }
        if path in sensitive_paths and numeric_mode & 0o007:
            raise RunnerError("PROFILE_SENSITIVE_CONFIGURATION_WORLD_ACCESSIBLE")
        root_private = {
            remote["authorization_header_file"],
        }
        if path in root_private and (entry["uid"] != 0 or numeric_mode not in {0o400, 0o600}):
            raise RunnerError("PROFILE_ROOT_PRIVATE_CONFIGURATION_INVALID")
    required_paths = {
        remote["authorization_header_file"], remote["tls_ca_file"],
        remote["nginx_auth_file"],
        remote["nginx_tls_server_path"], remote["nginx_http_parent_path"], "/usr/bin/node",
    }
    if not required_paths.issubset(config_paths):
        raise RunnerError("PROFILE_REQUIRED_CONFIGURATION_IDENTITY_MISSING")

    managed = value["managed_targets"]
    expected_managed = {
        ("systemd-unit", "/etc/systemd/system/prd-studio.service", "absent"),
        ("nginx-location-include", remote["nginx_include_path"], "absent"),
        ("nginx-http-include", remote["nginx_http_include_path"], "absent"),
    }
    if not isinstance(managed, list) or len(managed) != 3:
        raise RunnerError("PROFILE_MANAGED_TARGETS_INVALID")
    actual_managed = set()
    for entry in managed:
        _exact_fields(entry, {"id", "path", "expected_state"},
                      "PROFILE_MANAGED_TARGET_INVALID")
        _remote_path(entry["path"], "PROFILE_MANAGED_TARGET_PATH_INVALID")
        actual_managed.add((entry["id"], entry["path"], entry["expected_state"]))
    if actual_managed != expected_managed:
        raise RunnerError("PROFILE_MANAGED_TARGET_BINDING_INVALID")

    route = _exact_fields(value["route"], {"base_url"}, "PROFILE_ROUTE_FIELDS_INVALID")
    if not isinstance(route["base_url"], str):
        raise RunnerError("PROFILE_ROUTE_INVALID")
    parsed = urllib.parse.urlsplit(route["base_url"])
    try:
        port = parsed.port
    except ValueError as error:
        raise RunnerError("PROFILE_ROUTE_INVALID") from error
    host_digest = sha256_bytes((parsed.hostname or "").lower().encode("ascii", "strict"))
    if (parsed.scheme != "https" or host_digest != PROTECTED_TARGET_HOST_SHA256
            or port not in {None, 443} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/prd-studio"):
        raise RunnerError("PROFILE_ROUTE_INVALID")

    probes = value["out_of_scope"]
    if not isinstance(probes, list) or not 1 <= len(probes) <= 16:
        raise RunnerError("PROFILE_OUT_OF_SCOPE_INVALID")
    probe_ids: set[str] = set()
    for probe in probes:
        _exact_fields(probe, {"id", "path", "sha256"}, "PROFILE_OUT_OF_SCOPE_ENTRY_INVALID")
        if (not isinstance(probe["id"], str) or not IDENTIFIER.fullmatch(probe["id"])
                or probe["id"] in probe_ids):
            raise RunnerError("PROFILE_OUT_OF_SCOPE_ID_INVALID")
        probe_ids.add(probe["id"])
        _remote_path(probe["path"], "PROFILE_OUT_OF_SCOPE_PATH_INVALID")
        if not isinstance(probe["sha256"], str) or not HEX64.fullmatch(probe["sha256"]):
            raise RunnerError("PROFILE_OUT_OF_SCOPE_HASH_INVALID")

    certificate = _exact_fields(value["preflight_certificate"], {"path", "sha256"},
                                "PROFILE_CERTIFICATE_REFERENCE_INVALID")
    certificate_path = _absolute_local_path(certificate["path"], "PROFILE_CERTIFICATE_PATH_INVALID")
    _private_regular(certificate_path, "PROFILE_CERTIFICATE_NOT_PRIVATE")
    if not isinstance(certificate["sha256"], str) or not HEX64.fullmatch(certificate["sha256"]):
        raise RunnerError("PROFILE_CERTIFICATE_HASH_INVALID")
    contract = copy.deepcopy(value)
    del contract["preflight_certificate"]
    contract_sha256 = sha256_bytes(canonical_json_bytes(contract))
    cert, certificate_sha256 = load_json_with_digest(certificate_path)
    if certificate_sha256 != certificate["sha256"]:
        raise RunnerError("PROFILE_CERTIFICATE_HASH_MISMATCH")
    _exact_fields(cert, {
        "schema_version", "certificate_id", "status", "profile_contract_sha256",
        "checked_at_epoch", "expires_at_epoch", "checks",
    }, "PROFILE_CERTIFICATE_FIELDS_INVALID")
    if cert["schema_version"] != "1.0" or cert["status"] != "PASS":
        raise RunnerError("PROFILE_CERTIFICATE_NOT_PASSING")
    if not isinstance(cert["certificate_id"], str) or not IDENTIFIER.fullmatch(cert["certificate_id"]):
        raise RunnerError("PROFILE_CERTIFICATE_ID_INVALID")
    if cert["profile_contract_sha256"] != contract_sha256:
        raise RunnerError("PROFILE_CERTIFICATE_BINDING_MISMATCH")
    now = int(time.time())
    if (type(cert["checked_at_epoch"]) is not int or type(cert["expires_at_epoch"]) is not int
            or cert["checked_at_epoch"] > now + 300 or cert["expires_at_epoch"] <= now
            or cert["expires_at_epoch"] - cert["checked_at_epoch"] > 86400):
        raise RunnerError("PROFILE_CERTIFICATE_TIME_INVALID")
    checks = cert["checks"]
    if not isinstance(checks, list) or len(checks) != len(PREFLIGHT_CHECKS):
        raise RunnerError("PROFILE_CERTIFICATE_CHECKS_INVALID")
    by_id: dict[str, dict[str, Any]] = {}
    for check in checks:
        _exact_fields(check, {"id", "status", "evidence_sha256"},
                      "PROFILE_CERTIFICATE_CHECK_INVALID")
        if (check["id"] not in PREFLIGHT_CHECKS or check["id"] in by_id
                or check["status"] != "PASS" or not isinstance(check["evidence_sha256"], str)
                or not HEX64.fullmatch(check["evidence_sha256"])):
            raise RunnerError("PROFILE_CERTIFICATE_CHECK_INVALID")
        by_id[check["id"]] = check
    if set(by_id) != PREFLIGHT_CHECKS:
        raise RunnerError("PROFILE_CERTIFICATE_CHECK_SET_INVALID")
    backup_reference = _exact_fields(value["day2_backup_recovery"], {"path", "sha256"},
                                     "DAY2_BACKUP_REFERENCE_INVALID")
    backup_path = _absolute_local_path(backup_reference["path"], "DAY2_BACKUP_PATH_INVALID")
    _private_regular(backup_path, "DAY2_BACKUP_EVIDENCE_NOT_PRIVATE")
    if (not isinstance(backup_reference["sha256"], str)
            or not HEX64.fullmatch(backup_reference["sha256"])):
        raise RunnerError("DAY2_BACKUP_HASH_INVALID")
    backup, backup_sha256 = load_json_with_digest(backup_path)
    if backup_sha256 != backup_reference["sha256"]:
        raise RunnerError("DAY2_BACKUP_HASH_MISMATCH")
    _exact_fields(backup, {
        "schema_version", "evidence_id", "status", "candidate", "encrypted",
        "access_controlled", "schedule_seconds", "retention_days", "rpo_seconds",
        "isolated_restore", "reviewer_role", "reviewed_at_epoch", "expires_at_epoch",
    }, "DAY2_BACKUP_EVIDENCE_FIELDS_INVALID")
    candidate = _exact_fields(backup["candidate"], {
        "component_id", "environment", "commit", "tree", "artifact_sha256",
    },
                              "DAY2_BACKUP_CANDIDATE_INVALID")
    if (candidate["component_id"] != "prd-studio"
            or candidate["environment"] != "protected-staging"
            or not all(isinstance(candidate[key], str)
                       and re.fullmatch(r"[0-9a-f]{40}", candidate[key])
                       for key in ("commit", "tree"))
            or not isinstance(candidate["artifact_sha256"], str)
            or HEX64.fullmatch(candidate["artifact_sha256"]) is None):
        raise RunnerError("DAY2_BACKUP_CANDIDATE_INVALID")
    restore = _exact_fields(backup["isolated_restore"], {"status", "tested_at_epoch", "target_class"},
                            "DAY2_BACKUP_RESTORE_INVALID")
    if (backup["schema_version"] != "1.0" or backup["status"] != "PASS"
            or not isinstance(backup["evidence_id"], str)
            or not IDENTIFIER.fullmatch(backup["evidence_id"])
            or backup["encrypted"] is not True or backup["access_controlled"] is not True
            or type(backup["schedule_seconds"]) is not int or backup["schedule_seconds"] > 86400
            or backup["schedule_seconds"] < 300
            or type(backup["retention_days"]) is not int or backup["retention_days"] < 7
            or type(backup["rpo_seconds"]) is not int or backup["rpo_seconds"] < 1
            or backup["rpo_seconds"] > backup["schedule_seconds"]
            or restore.get("status") != "PASS" or restore.get("target_class") != "isolated"
            or type(restore.get("tested_at_epoch")) is not int
            or backup["reviewer_role"] != "independent-operations-reviewer"
            or type(backup["reviewed_at_epoch"]) is not int
            or type(backup["expires_at_epoch"]) is not int
            or backup["reviewed_at_epoch"] > now + 300
            or restore["tested_at_epoch"] > backup["reviewed_at_epoch"]
            or backup["expires_at_epoch"] <= now
            or backup["expires_at_epoch"] - backup["reviewed_at_epoch"] > 30 * 86400):
        raise RunnerError("DAY2_BACKUP_EVIDENCE_NOT_READY")
    if by_id["day2-backup-recovery"]["evidence_sha256"] != backup_sha256:
        raise RunnerError("DAY2_BACKUP_CERTIFICATE_BINDING_MISMATCH")
    return contract_sha256, backup


def load_connection_profile(path: pathlib.Path) -> ConnectionProfile:
    path = path.resolve(strict=True)
    _private_regular(path, "PROFILE_FILE_NOT_PRIVATE")
    value = load_json(path)
    contract_sha256, day2_backup = _validate_profile(value)
    return ConnectionProfile(path=path, value=value, contract_sha256=contract_sha256,
                             day2_backup_recovery=day2_backup)
