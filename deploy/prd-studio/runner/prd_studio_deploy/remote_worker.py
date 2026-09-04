#!/usr/bin/env python3
"""Standalone remote worker held open by SSH for one complete deployment slot.

This module deliberately uses only the Python standard library.  The local
supervisor transmits its exact source to the target, then speaks one-line JSON.
The worker opens the pre-existing account-wide flock once and retains the file
descriptor until the terminal close request or process termination.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import secrets
from typing import Any

GLOBAL_LOCK = "/var/lock/bankkaro-protected-staging.lock"
APP_ROOT = pathlib.Path("/opt/prd-studio")
RELEASES_ROOT = APP_ROOT / "releases"
CURRENT_LINK = APP_ROOT / "current"
RUNTIME_DIR = pathlib.Path("/run/prd-studio")
HTTP_SOCKET = RUNTIME_DIR / "http.sock"
WRITE_FENCE_DIR = pathlib.Path("/var/lib/prd-studio/deployment-control")
WRITE_FENCE = WRITE_FENCE_DIR / "write-fence"
SYSTEMD_UNIT = pathlib.Path("/etc/systemd/system/prd-studio.service")
RELEASE_ENV = pathlib.Path("/etc/prd-studio/release.env")
STAGING_ROOT = pathlib.Path("/var/lib/prd-studio/deployment-staging")
BACKUP_ROOT = pathlib.Path("/var/lib/prd-studio/deployment-backups")
PRIVATE_STATE_ROOT = pathlib.Path("/var/lib/prd-studio")
DATABASE = "prd_studio"
SERVICE = "prd-studio.service"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
RUNTIME_ENV_KEYS = {
    "DB_HOST", "DB_PORT", "DB_SOCKET_PATH", "DB_USER", "DB_PASSWORD_FILE",
    "DB_NAME", "DB_SSL_MODE",
}
MIGRATION_ENV_KEYS = {
    "MIGRATION_DB_HOST", "MIGRATION_DB_PORT", "MIGRATION_DB_SOCKET_PATH",
    "MIGRATION_DB_USER", "MIGRATION_DB_PASSWORD_FILE", "MIGRATION_DB_NAME",
    "MIGRATION_DB_SSL_MODE",
}
APPLICATION_CONFIG_DIR = pathlib.Path("/etc/prd-studio")
RUNTIME_DB_USER = "prd_studio_runtime"
MIGRATION_DB_USER = "prd_studio_migration"
FATAL_LOG_EVENTS = {
    "startup_configuration_failed", "server_failed", "shutdown_timed_out",
    "database_close_failed", "schema_apply_failed", "write_fence_invalid",
    "write_fence_check_failed", "invalid_stored_data",
}
ALLOWED_APP_LOG_RECORDS = {("server_listening", None)}
ACTIVE_CHILD: subprocess.Popen[bytes] | None = None
WORKER_INSTANCE: "Worker | None" = None


class WorkerError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise WorkerError(reason_code)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: pathlib.Path, maximum: int = 4 * 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                 "INPUT_NOT_REGULAR")
        _require(0 <= before.st_size <= maximum, "INPUT_SIZE_INVALID")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(fd, min(remaining, 1024 * 1024))
            _require(bool(block), "INPUT_READ_INCOMPLETE")
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        _require(identity(before) == identity(after), "INPUT_CHANGED_DURING_READ")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha256_file(path: pathlib.Path, maximum: int = 1024 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_size <= maximum, "DIGEST_INPUT_INVALID")
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        _require(identity(before) == identity(after), "DIGEST_INPUT_CHANGED")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _kill_child() -> None:
    global ACTIVE_CHILD
    child = ACTIVE_CHILD
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 0.5
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _signal_exit(_number: int, _frame: Any) -> None:
    _kill_child()
    if WORKER_INSTANCE is not None:
        WORKER_INSTANCE.emergency_rollback()
    raise SystemExit(143)


def _run(argv: list[str], timeout: float, *, env: dict[str, str] | None = None,
         stdin_data: bytes | None = None,
         max_output: int = 256 * 1024) -> tuple[int, bytes, bytes]:
    global ACTIVE_CHILD
    _require(bool(argv) and timeout > 0, "CHILD_ARGUMENT_INVALID")
    child_env = ({"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
                 if env is None else dict(env))
    input_file = tempfile.TemporaryFile(mode="w+b") if stdin_data is not None else None
    if input_file is not None:
        input_file.write(stdin_data or b"")
        input_file.seek(0)
    child = subprocess.Popen(
        argv, stdin=input_file if input_file is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=child_env, start_new_session=True,
    )
    ACTIVE_CHILD = child
    assert child.stdout is not None and child.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_child()
                raise WorkerError("REMOTE_CHILD_TIMEOUT")
            for key, _ in selector.select(min(remaining, 0.1)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(block)
                if len(output[key.data]) > max_output:
                    _kill_child()
                    raise WorkerError("REMOTE_CHILD_OUTPUT_LIMIT")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_child()
            raise WorkerError("REMOTE_CHILD_TIMEOUT")
        child.wait(timeout=remaining)
        return child.returncode, bytes(output["stdout"]), bytes(output["stderr"])
    except subprocess.TimeoutExpired:
        _kill_child()
        raise WorkerError("REMOTE_CHILD_TIMEOUT") from None
    finally:
        selector.close()
        ACTIVE_CHILD = None
        if input_file is not None:
            input_file.close()


def _atomic_write(path: pathlib.Path, payload: bytes, mode: int) -> None:
    _require(path.is_absolute() and path.parent.is_dir(), "OUTPUT_PARENT_MISSING")
    temporary = path.parent / ("." + path.name + ".new")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, mode)
    complete = False
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            _require(count > 0, "OUTPUT_WRITE_INCOMPLETE")
            view = view[count:]
        os.fsync(fd)
        complete = True
    finally:
        os.close(fd)
        if not complete:
            temporary.unlink(missing_ok=True)
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _write_exclusive(path: pathlib.Path, value: dict[str, Any], mode: int = 0o400) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            _require(count > 0, "OUTPUT_WRITE_INCOMPLETE")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return _sha256_bytes(payload)


def _parse_env(path: pathlib.Path, *, kind: str) -> dict[str, str]:
    raw = _read_regular(path, 64 * 1024)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkerError("APPLICATION_ENV_ENCODING_INVALID") from error
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        _require("=" in line, "APPLICATION_ENV_LINE_INVALID")
        key, value = line.split("=", 1)
        _require(ENV_NAME.fullmatch(key) is not None and key not in result,
                 "APPLICATION_ENV_KEY_INVALID")
        _require("\x00" not in value and "\r" not in value and "\n" not in value,
                 "APPLICATION_ENV_VALUE_INVALID")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    if kind == "runtime":
        _require(set(result) == RUNTIME_ENV_KEYS, "RUNTIME_ENV_KEY_SET_INVALID")
        _require(result == {
            "DB_HOST": "127.0.0.1", "DB_PORT": "3306",
            "DB_SOCKET_PATH": result.get("DB_SOCKET_PATH", ""),
            "DB_USER": RUNTIME_DB_USER,
            "DB_PASSWORD_FILE": "/etc/prd-studio/db-password",
            "DB_NAME": DATABASE, "DB_SSL_MODE": "disabled",
        }, "RUNTIME_ENV_VALUE_INVALID")
    elif kind == "migration":
        _require(set(result) == MIGRATION_ENV_KEYS, "MIGRATION_ENV_KEY_SET_INVALID")
        _require(result["MIGRATION_DB_USER"] == MIGRATION_DB_USER
                 and result["MIGRATION_DB_NAME"] == DATABASE
                 and result["MIGRATION_DB_PASSWORD_FILE"] == "/etc/prd-studio/migration-password"
                 and result["MIGRATION_DB_SSL_MODE"] == "disabled",
                 "MIGRATION_ENV_VALUE_INVALID")
    else:
        raise WorkerError("ENVIRONMENT_KIND_INVALID")
    return result


def _mysql(profile: dict[str, Any], sql: str, timeout: float = 10) -> list[str]:
    rc, stdout, _stderr = _run([
        "/usr/bin/mysql", "--no-defaults", "--protocol=socket",
        f"--socket={profile['remote']['mysql_socket_path']}", "--user=root", "--batch",
        "--skip-column-names", "--raw", "-e", sql,
    ], timeout, max_output=64 * 1024)
    _require(rc == 0, "DATABASE_COMMAND_FAILED")
    try:
        return stdout.decode("ascii").strip().splitlines()
    except UnicodeDecodeError as error:
        raise WorkerError("DATABASE_RESULT_ENCODING_INVALID") from error


def _mysql_input(profile: dict[str, Any], sql: bytes, *, database: str | None = None,
                 timeout: float = 15) -> None:
    argv = ["/usr/bin/mysql", "--no-defaults", "--protocol=socket",
            f"--socket={profile['remote']['mysql_socket_path']}", "--user=root"]
    if database is not None:
        _require(database == DATABASE, "DATABASE_ARGUMENT_INVALID")
        argv += ["--database", database]
    rc, _stdout, _stderr = _run(argv, timeout, stdin_data=sql, max_output=64 * 1024)
    _require(rc == 0, "DATABASE_INPUT_COMMAND_FAILED")


def _database_exists(profile: dict[str, Any]) -> bool:
    rows = _mysql(profile,
                  "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='prd_studio'", 5)
    _require(rows in (["0"], ["1"]), "DATABASE_EXISTENCE_RESULT_INVALID")
    return rows == ["1"]


def _validate_mysql_endpoint(profile: dict[str, Any]) -> dict[str, str]:
    remote = profile["remote"]
    socket_path = pathlib.Path(remote["mysql_socket_path"])
    expected = remote["mysql_socket_identity"]
    parent = os.lstat(socket_path.parent)
    details = os.lstat(socket_path)
    _require(stat.S_ISDIR(parent.st_mode) and not stat.S_ISLNK(parent.st_mode)
             and parent.st_uid == expected["parent_uid"]
             and parent.st_gid == expected["parent_gid"]
             and f"0{stat.S_IMODE(parent.st_mode):03o}" == expected["parent_mode"]
             and stat.S_IMODE(parent.st_mode) & 0o002 == 0,
             "MYSQL_SOCKET_PARENT_IDENTITY_MISMATCH")
    _require(stat.S_ISSOCK(details.st_mode) and not stat.S_ISLNK(details.st_mode)
             and details.st_uid == expected["uid"] and details.st_gid == expected["gid"]
             and f"0{stat.S_IMODE(details.st_mode):03o}" == expected["mode"],
             "MYSQL_SOCKET_IDENTITY_MISMATCH")
    rows = _mysql(profile, "SELECT @@socket,@@server_uuid", 5)
    _require(len(rows) == 1 and len(rows[0].split("\t")) == 2,
             "MYSQL_SERVER_IDENTITY_RESULT_INVALID")
    reported_socket, server_uuid = rows[0].split("\t")
    _require(pathlib.PurePosixPath(reported_socket) == pathlib.PurePosixPath(
        remote["mysql_socket_path"]), "MYSQL_REPORTED_SOCKET_MISMATCH")
    _require(_sha256_bytes(server_uuid.lower().encode("ascii"))
             == remote["mysql_server_uuid_sha256"], "MYSQL_SERVER_UUID_MISMATCH")
    return {"socket_identity": "matched", "server_uuid_sha256":
            remote["mysql_server_uuid_sha256"]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a protected credential to a redirect target."""

    def redirect_request(self, _req: Any, _fp: Any, _code: int, _msg: str,
                         _headers: Any, _newurl: str) -> None:
        return None


def _public_request(profile: dict[str, Any], path: str, *, method: str = "GET",
                    payload: dict[str, Any] | None = None,
                    credential: str = "valid", spoof_proxy: bool = False) -> tuple[int, bytes]:
    _require(path.startswith("/") and ".." not in path and "?" not in path,
             "PUBLIC_REQUEST_PATH_INVALID")
    base = profile["route"]["base_url"].rstrip("/")
    url = base + path
    data = None if payload is None else json.dumps(
        payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if credential == "valid":
        auth_path = pathlib.Path(profile["remote"]["authorization_header_file"])
        auth = _read_regular(auth_path, 8192).decode("ascii").strip()
        _require(auth.startswith(("Basic ", "Bearer ")) and "\n" not in auth and "\r" not in auth,
                 "AUTHORIZATION_HEADER_FILE_INVALID")
        headers["Authorization"] = auth
    elif credential == "invalid":
        headers["Authorization"] = "Basic ZGVwbG95LWludmFsaWQ6aW52YWxpZA=="
    else:
        _require(credential == "none", "PUBLIC_CREDENTIAL_MODE_INVALID")
    if spoof_proxy:
        headers["X-PRD-Authenticated"] = "1"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = ssl.create_default_context(cafile=profile["remote"]["tls_ca_file"])
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=8) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            _require(len(body) <= 2 * 1024 * 1024, "PUBLIC_RESPONSE_TOO_LARGE")
            return response.status, body
    except urllib.error.HTTPError as error:
        body = error.read(2 * 1024 * 1024 + 1)
        _require(len(body) <= 2 * 1024 * 1024, "PUBLIC_RESPONSE_TOO_LARGE")
        return error.code, body
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as error:
        raise WorkerError("PUBLIC_ROUTE_UNREACHABLE") from error


def _unix_request(path: str, *, trusted: bool) -> tuple[int, bytes]:
    _require(path.startswith("/") and ".." not in path, "UNIX_REQUEST_PATH_INVALID")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(HTTP_SOCKET))
        headers = [f"GET {path} HTTP/1.1", "Host: localhost", "Connection: close"]
        if trusted:
            headers.append("X-PRD-Authenticated: 1")
        connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = response.read(1024 * 1024 + 1)
        _require(len(body) <= 1024 * 1024, "UNIX_RESPONSE_TOO_LARGE")
        return response.status, body
    except (OSError, http.client.HTTPException) as error:
        raise WorkerError("UNIX_SOCKET_REQUEST_FAILED") from error
    finally:
        connection.close()


def _safe_json_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("HTTP_JSON_INVALID") from error
    _require(isinstance(value, dict), "HTTP_JSON_NOT_OBJECT")
    return value


def _capture_configuration(profile: dict[str, Any]) -> list[dict[str, Any]]:
    captured = []
    for expected in profile["configuration"]:
        path = pathlib.Path(expected["path"])
        details = os.lstat(path)
        _require(stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)
                 and details.st_nlink == 1, "CONFIGURATION_NOT_REGULAR")
        mode = f"0{stat.S_IMODE(details.st_mode):03o}"
        digest = _sha256_file(path, 4 * 1024 * 1024)
        _require(details.st_uid == expected["uid"] and details.st_gid == expected["gid"]
                 and mode == expected["mode"] and digest == expected["sha256"],
                 "CONFIGURATION_IDENTITY_MISMATCH")
        captured.append({"id": expected["id"], "sha256": digest, "mode": mode,
                         "uid": details.st_uid, "gid": details.st_gid})
    return captured


def _probe_out_of_scope(profile: dict[str, Any]) -> list[dict[str, str]]:
    values = []
    for probe in profile["out_of_scope"]:
        digest = _sha256_file(pathlib.Path(probe["path"]), 64 * 1024 * 1024)
        _require(digest == probe["sha256"], "OUT_OF_SCOPE_IDENTITY_MISMATCH")
        values.append({"id": probe["id"], "sha256": digest})
    return values


def _systemctl(*args: str, timeout: float = 10) -> tuple[int, bytes]:
    rc, stdout, _stderr = _run(["/usr/bin/systemctl", *args], timeout, max_output=64 * 1024)
    return rc, stdout


def _ensure_absent_baseline(state: dict[str, Any]) -> None:
    profile = state["profile"]
    managed = [CURRENT_LINK, SYSTEMD_UNIT, RELEASE_ENV,
               pathlib.Path(profile["remote"]["nginx_include_path"]),
               pathlib.Path(profile["remote"]["nginx_http_include_path"]), HTTP_SOCKET,
               WRITE_FENCE, WRITE_FENCE_DIR, RUNTIME_DIR, APP_ROOT, PRIVATE_STATE_ROOT,
               RELEASES_ROOT / state["candidate"]["artifact_sha256"],
               RELEASES_ROOT / ("." + state["candidate"]["artifact_sha256"] + ".new")]
    _require(all(not path.exists() and not path.is_symlink() for path in managed),
             "MANAGED_TARGET_NOT_ABSENT")
    active, _ = _systemctl("is-active", "--quiet", SERVICE, timeout=5)
    _require(active != 0, "SERVICE_ALREADY_ACTIVE")
    enabled, _ = _systemctl("is-enabled", "--quiet", SERVICE, timeout=5)
    _require(enabled != 0, "SERVICE_ALREADY_ENABLED")
    _require(not _database_exists(profile), "DATABASE_ALREADY_EXISTS")
    import pwd
    import grp
    try:
        pwd.getpwnam("prd-studio")
        raise WorkerError("SERVICE_ACCOUNT_ALREADY_EXISTS")
    except KeyError:
        pass
    try:
        grp.getgrnam("prd-studio")
        raise WorkerError("SERVICE_GROUP_ALREADY_EXISTS")
    except KeyError:
        pass
    try:
        grp.getgrnam("prd-studio-socket")
        raise WorkerError("SOCKET_GROUP_ALREADY_EXISTS")
    except KeyError:
        pass
    _require(not pathlib.Path("/etc/prd-studio").exists(), "APPLICATION_CONFIG_ALREADY_EXISTS")
    account_rows = _mysql(profile,
        "SELECT User,Host FROM mysql.user WHERE User IN "
        "('prd_studio_runtime','prd_studio_migration') ORDER BY User,Host", 5)
    _require(account_rows == [], "DATABASE_APPLICATION_ACCOUNT_ALREADY_EXISTS")


def _nginx_activation_baseline(profile: dict[str, Any]) -> dict[str, str]:
    remote = profile["remote"]
    parent_pairs = (
        ("tls", pathlib.Path(remote["nginx_tls_server_path"]),
         remote["nginx_tls_server_anchor"], remote["nginx_include_path"]),
        ("http", pathlib.Path(remote["nginx_http_parent_path"]),
         remote["nginx_http_parent_anchor"], remote["nginx_http_include_path"]),
    )
    evidence: dict[str, str] = {}
    rc, stdout, stderr = _run(["/usr/sbin/nginx", "-T"], 10, max_output=8 * 1024 * 1024)
    _require(rc == 0, "NGINX_ACTIVE_CONFIGURATION_INVALID")
    active = stdout + b"\n" + stderr
    reserved = (
        remote["nginx_include_path"].encode("ascii"),
        remote["nginx_http_include_path"].encode("ascii"),
        str(HTTP_SOCKET).encode("ascii"), b"X-PRD-Authenticated",
        b"location = /prd-studio", b"location /prd-studio/",
        b"zone=prd_studio_auth",
    )
    _require(all(item not in active for item in reserved),
             "NGINX_RESERVED_ROUTE_TOPOLOGY_NOT_ABSENT")
    for role, path, anchor, target in parent_pairs:
        payload = _read_regular(path, 4 * 1024 * 1024)
        anchor_bytes = anchor.encode("utf-8")
        include_bytes = ("include " + target + ";").encode("ascii")
        _require(payload.count(anchor_bytes) == 1 and include_bytes not in payload,
                 "NGINX_ACTIVATION_ANCHOR_INVALID")
        _require(("configuration file " + str(path) + ":").encode("utf-8") in active,
                 "NGINX_PARENT_NOT_ACTIVE")
        evidence[role + "_parent_sha256"] = _sha256_bytes(payload)
    return evidence


def _nginx_activation_installed(profile: dict[str, Any]) -> str:
    """Prove the expanded active config contains one managed auth path only."""
    remote = profile["remote"]
    rc, stdout, stderr = _run(["/usr/sbin/nginx", "-T"], 10,
                              max_output=8 * 1024 * 1024)
    _require(rc == 0, "NGINX_ACTIVE_CONFIGURATION_INVALID")
    active = stdout + b"\n" + stderr
    exact_once = (
        ("configuration file " + remote["nginx_include_path"] + ":").encode("ascii"),
        ("configuration file " + remote["nginx_http_include_path"] + ":").encode("ascii"),
        b"location = /prd-studio", b"location /prd-studio/",
        b"proxy_set_header X-PRD-Authenticated 1;",
        b"proxy_pass http://unix:/run/prd-studio/http.sock:;",
        b"zone=prd_studio_auth:1m rate=5r/s;",
    )
    _require(all(active.count(item) == 1 for item in exact_once),
             "NGINX_MANAGED_ROUTE_TOPOLOGY_MISMATCH")
    _require(active.count(b"X-PRD-Authenticated") == 1
             and active.count(str(HTTP_SOCKET).encode("ascii")) == 1,
             "NGINX_TRUSTED_PROXY_TOPOLOGY_AMBIGUOUS")
    return _sha256_bytes(active)


def _replace_regular(path: pathlib.Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    _atomic_write(path, payload, mode)
    os.chown(path, uid, gid)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _patch_nginx_parents(state: dict[str, Any]) -> dict[str, str]:
    profile = state["profile"]
    remote = profile["remote"]
    entries = (
        ("tls", pathlib.Path(remote["nginx_tls_server_path"]),
         remote["nginx_tls_server_anchor"], remote["nginx_include_path"]),
        ("http", pathlib.Path(remote["nginx_http_parent_path"]),
         remote["nginx_http_parent_anchor"], remote["nginx_http_include_path"]),
    )
    saved: dict[str, dict[str, Any]] = {}
    state["nginx_parent_snapshots"] = saved
    result: dict[str, str] = {}
    for role, path, anchor, include_path in entries:
        details = os.lstat(path)
        _require(stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)
                 and details.st_nlink == 1, "NGINX_PARENT_NOT_REGULAR")
        original = _read_regular(path, 4 * 1024 * 1024)
        anchor_bytes = anchor.encode("utf-8")
        _require(original.count(anchor_bytes) == 1, "NGINX_ACTIVATION_ANCHOR_INVALID")
        directive = ("\n    include " + include_path + ";").encode("ascii")
        _require(directive.strip() not in original, "NGINX_INCLUDE_ALREADY_PRESENT")
        snapshot = BACKUP_ROOT / f"{state['release_id']}.nginx-{role}-parent.bin"
        _require(not snapshot.exists(), "NGINX_PARENT_SNAPSHOT_EXISTS")
        _atomic_write(snapshot, original, 0o400)
        mode = stat.S_IMODE(details.st_mode)
        saved[role] = {"path": str(path), "snapshot": str(snapshot),
                       "sha256": _sha256_bytes(original), "mode": mode,
                       "uid": details.st_uid, "gid": details.st_gid}
        state["nginx_parents_patched"] = True
        patched = original.replace(anchor_bytes, anchor_bytes + directive, 1)
        _replace_regular(path, patched, mode, details.st_uid, details.st_gid)
        result[role + "_patched_sha256"] = _sha256_bytes(patched)
    return result


def _restore_nginx_parents(state: dict[str, Any]) -> None:
    snapshots = state.get("nginx_parent_snapshots", {})
    for role in ("tls", "http"):
        entry = snapshots.get(role)
        if not entry:
            continue
        snapshot = pathlib.Path(entry["snapshot"])
        payload = _read_regular(snapshot, 4 * 1024 * 1024)
        _require(_sha256_bytes(payload) == entry["sha256"], "NGINX_PARENT_SNAPSHOT_CHANGED")
        _replace_regular(pathlib.Path(entry["path"]), payload, entry["mode"],
                         entry["uid"], entry["gid"])
        _require(_sha256_file(pathlib.Path(entry["path"]), 4 * 1024 * 1024)
                 == entry["sha256"], "NGINX_PARENT_RESTORE_MISMATCH")
    state["nginx_parents_patched"] = False


def _establish_write_fence() -> None:
    import grp
    group = grp.getgrnam("prd-studio")
    state_parent = os.lstat(PRIVATE_STATE_ROOT)
    _require(stat.S_ISDIR(state_parent.st_mode) and not stat.S_ISLNK(state_parent.st_mode)
             and state_parent.st_uid == 0 and state_parent.st_gid == group.gr_gid
             and stat.S_IMODE(state_parent.st_mode) == 0o710,
             "PRIVATE_STATE_ROOT_IDENTITY_INVALID")
    if not WRITE_FENCE_DIR.exists():
        WRITE_FENCE_DIR.mkdir(mode=0o750)
        os.chown(WRITE_FENCE_DIR, 0, group.gr_gid)
    parent = os.lstat(WRITE_FENCE_DIR)
    _require(stat.S_ISDIR(parent.st_mode) and not stat.S_ISLNK(parent.st_mode)
             and parent.st_uid == 0 and parent.st_gid == group.gr_gid
             and stat.S_IMODE(parent.st_mode) == 0o750,
             "WRITE_FENCE_PARENT_INVALID")
    if not WRITE_FENCE.exists():
        fd = os.open(WRITE_FENCE, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o400)
        os.close(fd)
    fence = os.lstat(WRITE_FENCE)
    _require(stat.S_ISREG(fence.st_mode) and not stat.S_ISLNK(fence.st_mode)
             and fence.st_uid == 0 and fence.st_gid == 0 and fence.st_nlink == 1
             and stat.S_IMODE(fence.st_mode) == 0o400, "WRITE_FENCE_INVALID")


def _extract_archive(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with tarfile.open(archive, "r:") as bundle:
        members = bundle.getmembers()
        _require(1 <= len(members) <= 100000, "ARTIFACT_MEMBER_COUNT_INVALID")
        for member in members:
            pure = pathlib.PurePosixPath(member.name)
            _require(not pure.is_absolute() and ".." not in pure.parts
                     and pure.parts and pure.parts[0] == "app",
                     "ARTIFACT_PATH_INVALID")
            relative = pathlib.Path(*pure.parts[1:])
            target = destination / relative
            _require(member.isdir() or member.isfile() or member.issym(),
                     "ARTIFACT_MEMBER_TYPE_INVALID")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o755)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                _require(source is not None, "ARTIFACT_FILE_MISSING")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(target, flags, member.mode & 0o755)
                try:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(remaining, 1024 * 1024))
                        _require(bool(block), "ARTIFACT_FILE_TRUNCATED")
                        view = memoryview(block)
                        while view:
                            count = os.write(fd, view)
                            _require(count > 0, "ARTIFACT_FILE_WRITE_INCOMPLETE")
                            view = view[count:]
                        remaining -= len(block)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                _require(not pathlib.PurePosixPath(member.linkname).is_absolute()
                         and ".." not in pathlib.PurePosixPath(member.linkname).parts,
                         "ARTIFACT_SYMLINK_INVALID")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, target)


def evaluate_reset_guard(snapshot: dict[str, Any], expected_schema_sha256: str,
                         synthetic_id: str) -> tuple[bool, str]:
    """Pure rollback guard used by the live worker and conformance suite."""
    if snapshot == {"database_exists": False}:
        return True, "DATABASE_ALREADY_ABSENT"
    required = {
        "database_exists", "table_count", "known_table_count", "schema_row_count",
        "schema_version", "schema_checksum", "project_count", "synthetic_count",
        "synthetic_valid_count", "synthetic_payload_match",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != required:
        return False, "RESET_GUARD_EVIDENCE_INVALID"
    if (snapshot["database_exists"] is not True or snapshot["table_count"] != 2
            or snapshot["known_table_count"] != 2 or snapshot["schema_row_count"] != 1
            or snapshot["schema_version"] != 1
            or snapshot["schema_checksum"] != expected_schema_sha256):
        return False, "RESET_GUARD_SCHEMA_UNEXPECTED"
    count = snapshot["project_count"]
    if (count == 0 and snapshot["synthetic_count"] == 0
            and snapshot["synthetic_valid_count"] == 0
            and snapshot["synthetic_payload_match"] is True):
        return True, "RESET_GUARD_EMPTY"
    if (count == 1 and snapshot["synthetic_count"] == 1
            and snapshot["synthetic_valid_count"] == 1
            and snapshot["synthetic_payload_match"] is True):
        return True, "RESET_GUARD_SYNTHETIC_ONLY"
    return False, "RESET_GUARD_UNEXPECTED_DATA"


def _reset_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    profile = state["profile"]
    if not _database_exists(profile):
        return {"database_exists": False}
    fixture_id = state["synthetic_id"]
    table_rows = _mysql(profile,
        "SELECT COUNT(*),COALESCE(SUM(TABLE_NAME IN ('projects','schema_versions')),0) "
        "FROM information_schema.tables WHERE table_schema='prd_studio'", 8)
    _require(len(table_rows) == 1 and "\t" in table_rows[0], "RESET_GUARD_TABLE_RESULT_INVALID")
    table_count, known_count = [int(item) for item in table_rows[0].split("\t")]
    schema_rows = _mysql(profile,
        "SELECT COUNT(*),COALESCE(MAX(version),0),COALESCE(MAX(checksum),'') FROM prd_studio.schema_versions", 8)
    _require(len(schema_rows) == 1 and len(schema_rows[0].split("\t")) == 3,
             "RESET_GUARD_SCHEMA_RESULT_INVALID")
    schema_count_raw, schema_version_raw, checksum = schema_rows[0].split("\t")
    project_rows = _mysql(profile,
        "SELECT COUNT(*),"
        f"COALESCE(SUM(id='{fixture_id}'),0),"
        f"COALESCE(SUM(id='{fixture_id}' AND owner='automation' AND tech_lead='automation' "
        "AND qa='automation' AND designer='automation' AND tier='T3' "
        "AND status IN ('framing','building') AND row_version IN (1,2)),0) "
        "FROM prd_studio.projects", 8)
    _require(len(project_rows) == 1 and len(project_rows[0].split("\t")) == 3,
             "RESET_GUARD_PROJECT_RESULT_INVALID")
    project_count, synthetic_count, synthetic_valid = [int(item) for item in project_rows[0].split("\t")]
    payload_match = project_count == 0
    if project_count == 1 and synthetic_count == 1 and synthetic_valid == 1:
        detail_rows = _mysql(profile,
            f"SELECT title,owner,tech_lead,qa,designer,tier,status,row_version,HEX(CAST(data AS CHAR)) "
            f"FROM prd_studio.projects WHERE id='{fixture_id}'", 8)
        if len(detail_rows) == 1 and len(detail_rows[0].split("\t")) == 9:
            title, owner, tech, qa, designer, tier, status, project_version_raw, payload_hex = detail_rows[0].split("\t")
            try:
                actual_payload = json.loads(bytes.fromhex(payload_hex).decode("utf-8"))
                expected_create = json.loads(json.dumps(state["fixture"]["create"]["data"]))
                expected_update = json.loads(json.dumps(expected_create))
                expected_update["meta"]["title"] = state["fixture"]["update"]["title"]
                expected_update["meta"]["status"] = state["fixture"]["update"]["status"]
                expected_payload = expected_create if project_version_raw == "1" else expected_update
                payload_match = (
                    actual_payload == expected_payload
                    and title == expected_payload["meta"]["title"] and owner == "automation"
                    and tech == "automation" and qa == "automation" and designer == "automation"
                    and tier == "T3" and status == expected_payload["meta"]["status"]
                    and project_version_raw in {"1", "2"}
                )
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                payload_match = False
    return {
        "database_exists": True,
        "table_count": table_count,
        "known_table_count": known_count,
        "schema_row_count": int(schema_count_raw),
        "schema_version": int(schema_version_raw),
        "schema_checksum": checksum,
        "project_count": project_count,
        "synthetic_count": synthetic_count,
        "synthetic_valid_count": synthetic_valid,
        "synthetic_payload_match": payload_match,
    }


class Worker:
    def __init__(self) -> None:
        self.state: dict[str, Any] | None = None
        self.lock_fd: int | None = None

    def initialize(self, payload: dict[str, Any]) -> dict[str, Any]:
        _require(self.state is None and isinstance(payload, dict), "WORKER_ALREADY_INITIALIZED")
        required = {"profile", "manifest", "overlay", "assets", "fixture"}
        _require(set(payload) == required, "WORKER_INITIALIZATION_FIELDS_INVALID")
        manifest = payload["manifest"]
        _require(isinstance(manifest, dict) and manifest.get("environment") == "protected-staging"
                 and manifest.get("lane") == "stateful_backend", "WORKER_MANIFEST_SCOPE_INVALID")
        components = manifest.get("components")
        _require(isinstance(components, list) and len(components) == 1
                 and components[0].get("id") == "prd-studio", "WORKER_COMPONENT_INVALID")
        candidate = components[0].get("candidate", {})
        _require(HEX40.fullmatch(str(candidate.get("commit", ""))) is not None
                 and HEX40.fullmatch(str(candidate.get("tree", ""))) is not None
                 and HEX64.fullmatch(str(candidate.get("artifact_sha256", ""))) is not None,
                 "WORKER_CANDIDATE_INVALID")
        release_id = manifest.get("release_id")
        _require(isinstance(release_id, str) and SAFE_ID.fullmatch(release_id) is not None,
                 "WORKER_RELEASE_ID_INVALID")
        synthetic_id = "deploy-smoke-" + hashlib.sha256(release_id.encode()).hexdigest()[:20]
        machine_id = _read_regular(pathlib.Path("/etc/machine-id"), 256).strip()
        _require(bool(machine_id)
                 and _sha256_bytes(machine_id)
                 == payload["profile"]["remote"].get("machine_id_sha256"),
                 "REMOTE_MACHINE_ID_MISMATCH")
        lock_fd = os.open(GLOBAL_LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(lock_fd)
        _require(stat.S_ISREG(details.st_mode) and details.st_uid == 0
                 and details.st_nlink == 1 and stat.S_IMODE(details.st_mode) & 0o022 == 0,
                 "GLOBAL_LOCK_IDENTITY_INVALID")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise WorkerError("GLOBAL_LOCK_BUSY") from error
        self.lock_fd = lock_fd
        mysql_identity = _validate_mysql_endpoint(payload["profile"])
        self.state = dict(payload)
        self.state.update({
            "release_id": release_id,
            "candidate": candidate,
            "synthetic_id": synthetic_id,
            "journal_cursor": None,
            "release_created": False,
            "temporary_release_created": False,
            "mutation_armed": False,
            "commit_prepared": False,
            "commit_in_doubt": False,
            "terminal": False,
            "app_root_created": False,
            "private_state_root_created": False,
            "service_group_created": False,
            "socket_group_created": False,
            "service_user_created": False,
            "nginx_socket_membership_added": False,
            "database_created": False,
            "database_create_attempted": False,
            "database_provisioning_started": False,
            "runtime_user_created": False,
            "migration_user_created": False,
            "schema_completed": False,
            "service_started": False,
            "nginx_parents_patched": False,
            "application_config_created": False,
            "runtime_password": None,
            "migration_password": None,
            "staged_path": str(STAGING_ROOT / release_id / (candidate["artifact_sha256"] + ".tar")),
        })
        return {"status": "PASS", "reason_code": "GLOBAL_LOCK_ACQUIRED",
                "evidence": {"lock_id": "bankkaro-protected-staging", "exclusive": True,
                             "mysql_server_uuid_sha256":
                                 mysql_identity["server_uuid_sha256"]}}

    def initialize_reconcile(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct only the exact, durable post-verification participant state."""
        _require(self.state is None and isinstance(payload, dict), "WORKER_ALREADY_INITIALIZED")
        _require(set(payload) == {"profile", "manifest", "overlay", "assets", "fixture"},
                 "WORKER_INITIALIZATION_FIELDS_INVALID")
        manifest = payload["manifest"]
        components = manifest.get("components") if isinstance(manifest, dict) else None
        _require(manifest.get("environment") == "protected-staging"
                 and manifest.get("lane") == "stateful_backend"
                 and isinstance(components, list) and len(components) == 1
                 and components[0].get("id") == "prd-studio",
                 "WORKER_RECONCILE_SCOPE_INVALID")
        candidate = components[0].get("candidate", {})
        release_id = manifest.get("release_id")
        _require(isinstance(release_id, str) and SAFE_ID.fullmatch(release_id) is not None
                 and HEX40.fullmatch(str(candidate.get("commit", ""))) is not None
                 and HEX40.fullmatch(str(candidate.get("tree", ""))) is not None
                 and HEX64.fullmatch(str(candidate.get("artifact_sha256", ""))) is not None,
                 "WORKER_RECONCILE_IDENTITY_INVALID")
        machine_id = _read_regular(pathlib.Path("/etc/machine-id"), 256).strip()
        _require(bool(machine_id) and _sha256_bytes(machine_id)
                 == payload["profile"]["remote"].get("machine_id_sha256"),
                 "REMOTE_MACHINE_ID_MISMATCH")
        lock_fd = os.open(GLOBAL_LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(lock_fd)
        _require(stat.S_ISREG(details.st_mode) and details.st_uid == 0
                 and details.st_nlink == 1 and stat.S_IMODE(details.st_mode) & 0o022 == 0,
                 "GLOBAL_LOCK_IDENTITY_INVALID")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise WorkerError("GLOBAL_LOCK_BUSY") from error
        self.lock_fd = lock_fd
        _validate_mysql_endpoint(payload["profile"])
        recovery = BACKUP_ROOT / f"{release_id}.recovery-armed.json"
        prepared = BACKUP_ROOT / f"{release_id}.success-prepared.json"
        in_doubt = BACKUP_ROOT / f"{release_id}.global-commit-in-doubt.json"
        disarmed = BACKUP_ROOT / f"{release_id}.recovery-disarmed.json"
        _require(PRIVATE_STATE_ROOT.is_dir() and APP_ROOT.is_dir()
                 and (in_doubt.is_file() or disarmed.is_file()),
                 "GLOBAL_COMMIT_RECONCILE_STATE_MISSING")
        already_finalized = (disarmed.is_file() and not recovery.exists()
                             and not in_doubt.exists() and not WRITE_FENCE.exists())
        if not already_finalized:
            _require(recovery.is_file() and prepared.is_file() and in_doubt.is_file()
                     and WRITE_FENCE.is_file(), "GLOBAL_COMMIT_RECONCILE_STATE_INVALID")
        config_by_path = {entry["path"]: entry for entry in payload["profile"]["configuration"]}
        snapshots: dict[str, dict[str, Any]] = {}
        for role, path_key in (("tls", "nginx_tls_server_path"),
                               ("http", "nginx_http_parent_path")):
            parent_path = payload["profile"]["remote"][path_key]
            expected = config_by_path[parent_path]
            snapshot = BACKUP_ROOT / f"{release_id}.nginx-{role}-parent.bin"
            if not already_finalized:
                _require(snapshot.is_file() and _sha256_file(snapshot, 4 * 1024 * 1024)
                         == expected["sha256"], "NGINX_PARENT_RECONCILE_SNAPSHOT_INVALID")
            snapshots[role] = {"path": parent_path, "snapshot": str(snapshot),
                               "sha256": expected["sha256"],
                               "mode": int(expected["mode"], 8),
                               "uid": expected["uid"], "gid": expected["gid"]}
        synthetic_id = "deploy-smoke-" + hashlib.sha256(release_id.encode()).hexdigest()[:20]
        self.state = dict(payload)
        self.state.update({
            "release_id": release_id, "candidate": candidate, "synthetic_id": synthetic_id,
            "journal_cursor": None, "release_created": True,
            "temporary_release_created": False, "mutation_armed": not already_finalized,
            "commit_prepared": not already_finalized, "commit_in_doubt": not already_finalized,
            "terminal": already_finalized, "app_root_created": True,
            "private_state_root_created": True, "service_group_created": True,
            "socket_group_created": True, "service_user_created": True,
            "nginx_socket_membership_added": True, "database_created": True,
            "database_create_attempted": True, "database_provisioning_started": True,
            "runtime_user_created": True, "migration_user_created": False,
            "schema_completed": True, "service_started": True,
            "nginx_parents_patched": not already_finalized,
            "application_config_created": True, "runtime_password": None,
            "migration_password": None, "nginx_parent_snapshots": snapshots,
            "initial_out_of_scope": [
                {"id": item["id"], "sha256": item["sha256"]}
                for item in payload["profile"]["out_of_scope"]],
            "schema_sha256": _sha256_file(
                RELEASES_ROOT / candidate["artifact_sha256"] / "schema.sql", 1024 * 1024),
            "recovery_armed_path": str(recovery),
            "recovery_armed_sha256": (_sha256_file(recovery, 8192)
                                      if recovery.is_file() else None),
            "success_prepared_path": str(prepared),
            "success_prepared_sha256": (_sha256_file(prepared, 8192)
                                        if prepared.is_file() else None),
            "commit_in_doubt_path": str(in_doubt),
            "commit_in_doubt_sha256": (_sha256_file(in_doubt, 8192)
                                       if in_doubt.is_file() else None),
            "already_finalized": already_finalized,
            "staged_path": str(STAGING_ROOT / release_id
                               / (candidate["artifact_sha256"] + ".tar")),
        })
        return {"status": "PASS", "reason_code": "RECONCILE_LOCK_ACQUIRED",
                "evidence": {"exclusive": True, "already_finalized": already_finalized,
                             "write_fenced": WRITE_FENCE.exists()}}

    def _gate_live_baseline(self) -> dict[str, Any]:
        assert self.state is not None
        _ensure_absent_baseline(self.state)
        status, _body = _public_request(self.state["profile"], "/")
        _require(status == self.state["profile"]["remote"]["expected_absence_status"],
                 "ABSENT_ROUTE_STATUS_MISMATCH")
        rollback = self.state["manifest"]["components"][0]["rollback"]
        return {"baseline": "canonical-absence", "commit": rollback["commit"],
                "tree": rollback["tree"], "artifact_sha256": rollback["artifact_sha256"]}

    def _gate_configuration(self) -> dict[str, Any]:
        assert self.state is not None
        profile = self.state["profile"]
        captured = _capture_configuration(profile)
        logging_rows = _mysql(profile, "SELECT @@GLOBAL.general_log,@@GLOBAL.slow_query_log", 5)
        _require(logging_rows == ["0\t0"], "DATABASE_QUERY_LOGGING_UNSAFE")
        audit_rows = _mysql(profile,
            "SELECT COUNT(*) FROM information_schema.PLUGINS WHERE PLUGIN_STATUS='ACTIVE' "
            "AND LOWER(PLUGIN_NAME) LIKE '%audit%'", 5)
        _require(audit_rows == ["0"], "DATABASE_AUDIT_CAPTURE_UNSAFE")
        version_rows = _mysql(profile, "SELECT VERSION()", 5)
        _require(len(version_rows) == 1 and "mariadb" not in version_rows[0].lower(),
                 "DATABASE_RUNTIME_UNSUPPORTED")
        version_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_rows[0])
        _require(version_match is not None and tuple(map(int, version_match.groups())) >= (8, 0, 16),
                 "DATABASE_RUNTIME_UNSUPPORTED")
        for executable in ("/usr/bin/node", "/usr/bin/mysql", "/usr/bin/systemctl",
                           "/usr/sbin/nginx", "/usr/sbin/runuser", "/usr/bin/journalctl",
                           "/usr/bin/ss", "/usr/bin/env", "/usr/sbin/groupadd",
                           "/usr/sbin/useradd", "/usr/sbin/usermod", "/usr/sbin/userdel",
                           "/usr/sbin/groupdel", "/usr/bin/gpasswd", "/usr/bin/id",
                           "/usr/bin/cat", "/usr/bin/test", "/usr/bin/setpriv"):
            details = os.lstat(executable)
            _require(stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode)
                     and details.st_uid == 0 and stat.S_IMODE(details.st_mode) & 0o022 == 0,
                     "REQUIRED_EXECUTABLE_INVALID")
        node_rc, node_out, _node_err = _run(["/usr/bin/node", "--version"], 3, max_output=1024)
        _require(node_rc == 0 and node_out == b"v22.22.2\n", "NODE_RUNTIME_VERSION_MISMATCH")
        import pwd
        import grp
        nginx_user = pwd.getpwnam(profile["remote"]["nginx_worker_user"])
        nginx_group = grp.getgrnam(profile["remote"]["nginx_worker_group"])
        _require(nginx_user.pw_gid == nginx_group.gr_gid
                 or nginx_user.pw_name in nginx_group.gr_mem,
                 "NGINX_WORKER_GROUP_BINDING_INVALID")
        activation = _nginx_activation_baseline(profile)
        self.state["nginx_parent_baseline"] = activation
        probes = _probe_out_of_scope(profile)
        self.state["initial_out_of_scope"] = probes
        return {"configuration_count": len(captured),
                "configuration_digest": _sha256_bytes(json.dumps(captured, sort_keys=True).encode()),
                "out_of_scope_count": len(probes), "node_version": "v22.22.2",
                "database_statement_logs": "disabled",
                "node_sha256": _sha256_file(pathlib.Path("/usr/bin/node"), 256 * 1024 * 1024),
                "mysql_version": version_rows[0],
                "nginx_activation_sha256": _sha256_bytes(
                    json.dumps(activation, sort_keys=True).encode())}

    def _gate_private_backup(self) -> dict[str, Any]:
        assert self.state is not None
        _ensure_absent_baseline(self.state)
        release_id = self.state["release_id"]
        rollback = self.state["manifest"]["components"][0]["rollback"]
        overlay_bytes = (json.dumps(self.state["overlay"], sort_keys=True,
                                    separators=(",", ":")) + "\n").encode("ascii")
        declared_overlay = self.state["manifest"]["overlays"]
        _require(len(declared_overlay) == 1
                 and declared_overlay[0]["id"] == "prd-studio-canonical-absence"
                 and declared_overlay[0]["sha256"] == _sha256_bytes(overlay_bytes)
                 and self.state["overlay"]["absence_artifact_sha256"] == rollback["artifact_sha256"],
                 "ABSENCE_RECOVERY_BINDING_INVALID")
        value = {"schema_version": "1.0", "release_id": release_id,
                 "baseline": "canonical-absence", "database_exists": False,
                 "absence_artifact_sha256": rollback["artifact_sha256"],
                 "overlay_sha256": declared_overlay[0]["sha256"]}
        digest = _sha256_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
        return {"backup_id": f"{release_id}.absence", "backup_sha256": digest,
                "database_exists": False, "recovery_handle": "canonical-absence-artifact",
                "absence_artifact_sha256": rollback["artifact_sha256"],
                "overlay_sha256": declared_overlay[0]["sha256"]}

    def _gate_exact_install(self) -> dict[str, Any]:
        assert self.state is not None
        candidate = self.state["candidate"]
        stage = pathlib.Path(self.state["staged_path"])
        _require(_sha256_file(stage) == candidate["artifact_sha256"],
                 "STAGED_ARTIFACT_HASH_MISMATCH")
        _require(RELEASES_ROOT.is_dir(), "RELEASES_ROOT_MISSING")
        RUNTIME_DIR.mkdir(mode=0o750, exist_ok=True)
        # chown by account lookup without shell; the fallback is rejected below.
        import pwd
        import grp
        account = pwd.getpwnam("prd-studio")
        service_group = grp.getgrnam("prd-studio")
        _require(account.pw_gid == service_group.gr_gid, "SERVICE_ACCOUNT_GROUP_MISMATCH")
        nginx_user = self.state["profile"]["remote"]["nginx_worker_user"]
        nginx_account = pwd.getpwnam(nginx_user)
        nginx_group = grp.getgrnam("prd-studio-socket")
        _require(nginx_user in nginx_group.gr_mem,
                 "NGINX_WORKER_CANNOT_ACCESS_SOCKET")
        _require(nginx_group.gr_gid != service_group.gr_gid,
                 "NGINX_AND_SECRET_GROUP_NOT_SEPARATE")
        os.chown(RUNTIME_DIR, account.pw_uid, nginx_group.gr_gid)
        _establish_write_fence()
        release_dir = RELEASES_ROOT / candidate["artifact_sha256"]
        temporary = RELEASES_ROOT / ("." + candidate["artifact_sha256"] + ".new")
        _require(not release_dir.exists() and not temporary.exists(), "RELEASE_TARGET_ALREADY_EXISTS")
        temporary.mkdir(mode=0o755)
        self.state["temporary_release_created"] = True
        try:
            _extract_archive(stage, temporary)
            try:
                source_identity = json.loads(
                    _read_regular(temporary / ".release-source.json", 8192))
            except json.JSONDecodeError as error:
                raise WorkerError("RELEASE_SOURCE_IDENTITY_INVALID") from error
            _require(isinstance(source_identity, dict) and set(source_identity) == {
                "schema_version", "commit", "tree", "node_version", "npm_version",
                "build_os", "build_arch", "package_lock_sha256",
            }, "RELEASE_SOURCE_IDENTITY_INVALID")
            _require(source_identity["schema_version"] == "1.0"
                     and source_identity["commit"] == candidate["commit"]
                     and source_identity["tree"] == candidate["tree"]
                     and source_identity["node_version"] == "v22.22.2"
                     and source_identity["npm_version"] == "10.9.7"
                     and source_identity["build_os"] == "linux"
                     and source_identity["build_arch"] == "x86_64"
                     and isinstance(source_identity["package_lock_sha256"], str)
                     and HEX64.fullmatch(source_identity["package_lock_sha256"]) is not None
                     and _sha256_file(temporary / "package-lock.json", 4 * 1024 * 1024)
                     == source_identity["package_lock_sha256"],
                     "RELEASE_SOURCE_IDENTITY_MISMATCH")
            release_metadata = {
                "schema_version": "1.0", "commit": candidate["commit"],
                "tree": candidate["tree"], "artifact_sha256": candidate["artifact_sha256"],
            }
            _atomic_write(temporary / ".release-identity.json",
                          (json.dumps(release_metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                          0o444)
            os.rename(temporary, release_dir)
            self.state["temporary_release_created"] = False
            self.state["release_created"] = True
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        link_temp = CURRENT_LINK.parent / ".current.new"
        _require(not CURRENT_LINK.exists() and not CURRENT_LINK.is_symlink() and not link_temp.exists(),
                 "CURRENT_LINK_NOT_ABSENT")
        os.symlink(str(release_dir), link_temp)
        os.rename(link_temp, CURRENT_LINK)
        assets = self.state["assets"]
        unit = assets["systemd_unit"].encode("utf-8")
        nginx_source = assets["nginx_include"]
        marker = "/etc/nginx/protected-staging.htpasswd"
        _require(nginx_source.count(marker) == 1, "NGINX_AUTH_BINDING_MARKER_INVALID")
        nginx = nginx_source.replace(
            marker, self.state["profile"]["remote"]["nginx_auth_file"]).encode("utf-8")
        nginx_http = assets["nginx_http_include"].encode("utf-8")
        nginx_path = pathlib.Path(self.state["profile"]["remote"]["nginx_include_path"])
        nginx_http_path = pathlib.Path(self.state["profile"]["remote"]["nginx_http_include_path"])
        _require(not SYSTEMD_UNIT.exists() and not nginx_path.exists()
                 and not nginx_http_path.exists() and not RELEASE_ENV.exists(),
                 "MANAGED_CONFIG_NOT_ABSENT")
        _atomic_write(SYSTEMD_UNIT, unit, 0o644)
        _atomic_write(nginx_path, nginx, 0o644)
        _atomic_write(nginx_http_path, nginx_http, 0o644)
        parent_evidence = _patch_nginx_parents(self.state)
        release_env = (
            f"RELEASE_ID={self.state['release_id']}\n"
            f"RELEASE_COMMIT={candidate['commit']}\n"
            f"RELEASE_TREE={candidate['tree']}\n"
            f"RELEASE_ARTIFACT_SHA256={candidate['artifact_sha256']}\n"
        ).encode("ascii")
        _atomic_write(RELEASE_ENV, release_env, 0o644)
        rc, _ = _systemctl("daemon-reload", timeout=10)
        _require(rc == 0, "SYSTEMD_DAEMON_RELOAD_FAILED")
        return {"artifact_sha256": candidate["artifact_sha256"],
                "release_target": candidate["artifact_sha256"],
                "systemd_unit_sha256": _sha256_bytes(unit),
                "nginx_include_sha256": _sha256_bytes(nginx),
                "nginx_http_include_sha256": _sha256_bytes(nginx_http),
                "nginx_parent_patch_sha256": _sha256_bytes(
                    json.dumps(parent_evidence, sort_keys=True).encode())}

    def _gate_state_transition(self) -> dict[str, Any]:
        assert self.state is not None
        _require(WRITE_FENCE.is_file(), "WRITE_FENCE_MISSING")
        profile = self.state["profile"]
        _require(not _database_exists(profile), "DATABASE_CREATED_OUTSIDE_TRANSITION")
        release_dir = RELEASES_ROOT / self.state["candidate"]["artifact_sha256"]
        schema_sha = _sha256_file(release_dir / "schema.sql", 1024 * 1024)
        self.state["schema_sha256"] = schema_sha
        self.state["database_create_attempted"] = True
        self.state["database_provisioning_started"] = True
        _mysql(profile,
               "CREATE DATABASE prd_studio CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci", 10)
        self.state["database_created"] = True

        import pwd
        import grp
        service_account = pwd.getpwnam("prd-studio")
        service_group = grp.getgrnam("prd-studio")
        nginx_group = grp.getgrnam(profile["remote"]["nginx_worker_group"])
        socket_group = grp.getgrnam("prd-studio-socket")
        config_dir = APPLICATION_CONFIG_DIR
        config_details = os.lstat(config_dir)
        _require(stat.S_ISDIR(config_details.st_mode) and not stat.S_ISLNK(config_details.st_mode)
                 and config_details.st_uid == 0 and config_details.st_gid == service_group.gr_gid
                 and stat.S_IMODE(config_details.st_mode) == 0o750,
                 "APPLICATION_CONFIG_DIRECTORY_INVALID")
        _require(socket_group.gr_gid != service_group.gr_gid
                 and socket_group.gr_gid != nginx_group.gr_gid,
                 "SOCKET_GROUP_NOT_DEDICATED")

        migration_password = secrets.token_urlsafe(36)
        _require(re.fullmatch(r"[A-Za-z0-9_-]{40,80}", migration_password) is not None,
                 "GENERATED_PASSWORD_INVALID")
        self.state["migration_password"] = migration_password
        _mysql_input(profile, (
            "CREATE USER 'prd_studio_migration'@'localhost' IDENTIFIED BY '"
            + migration_password + "';"
        ).encode("ascii"), timeout=8)
        self.state["migration_user_created"] = True
        _mysql_input(profile, (
            "GRANT SELECT,INSERT,CREATE ON prd_studio.* "
            "TO 'prd_studio_migration'@'localhost';FLUSH PRIVILEGES;"
        ).encode("ascii"), timeout=8)
        migration_password_path = config_dir / "migration-password"
        _atomic_write(migration_password_path, (migration_password + "\n").encode("ascii"), 0o400)
        os.chown(migration_password_path, service_account.pw_uid, service_group.gr_gid)
        migration_env_path = config_dir / "migration.env"
        migration_env_bytes = (
            "MIGRATION_DB_HOST=127.0.0.1\nMIGRATION_DB_PORT=3306\n"
            "MIGRATION_DB_SOCKET_PATH=" + profile["remote"]["mysql_socket_path"] + "\n"
            "MIGRATION_DB_USER=prd_studio_migration\n"
            "MIGRATION_DB_PASSWORD_FILE=/etc/prd-studio/migration-password\n"
            "MIGRATION_DB_NAME=prd_studio\nMIGRATION_DB_SSL_MODE=disabled\n"
        ).encode("ascii")
        _atomic_write(migration_env_path, migration_env_bytes, 0o400)
        os.chown(migration_env_path, service_account.pw_uid, service_group.gr_gid)
        migration_env = _parse_env(migration_env_path, kind="migration")
        migration_argv = [
            "/usr/sbin/runuser", "--user", "prd-studio", "--", "/usr/bin/env", "-i",
            "PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", "DOTENV_CONFIG_QUIET=true",
            *[f"{key}={migration_env[key]}" for key in sorted(migration_env)],
            "/usr/bin/node", str(release_dir / "scripts/apply-schema.js"),
        ]

        def run_schema(expected_event: str) -> None:
            rc, stdout, stderr = _run(migration_argv, 35, max_output=64 * 1024)
            _require(rc == 0 and not stderr, "SCHEMA_RUNNER_FAILED")
            lines = [line for line in stdout.splitlines() if line]
            _require(len(lines) == 1, "SCHEMA_RUNNER_OUTPUT_INVALID")
            try:
                record = json.loads(lines[0])
            except json.JSONDecodeError as error:
                raise WorkerError("SCHEMA_RUNNER_OUTPUT_INVALID") from error
            _require(record == {"level": "info", "event": expected_event},
                     "SCHEMA_RUNNER_OUTCOME_INVALID")

        run_schema("schema_applied")
        run_schema("schema_already_current")
        self.state["schema_completed"] = True
        _mysql_input(profile,
                     b"DROP USER 'prd_studio_migration'@'localhost';FLUSH PRIVILEGES;", timeout=8)
        self.state["migration_user_created"] = False
        _require(_mysql(profile,
            "SELECT COUNT(*) FROM mysql.user WHERE User='prd_studio_migration' AND Host='localhost'", 5)
            == ["0"], "MIGRATION_ACCOUNT_REMOVAL_FAILED")
        migration_env_path.unlink()
        migration_password_path.unlink()

        runtime_password = secrets.token_urlsafe(36)
        _require(re.fullmatch(r"[A-Za-z0-9_-]{40,80}", runtime_password) is not None,
                 "GENERATED_PASSWORD_INVALID")
        _mysql_input(profile, (
            "CREATE USER 'prd_studio_runtime'@'localhost' IDENTIFIED BY '"
            + runtime_password + "';"
        ).encode("ascii"), timeout=8)
        self.state["runtime_user_created"] = True
        _mysql_input(profile,
            b"GRANT SELECT ON prd_studio.schema_versions TO 'prd_studio_runtime'@'localhost';"
            b"GRANT SELECT,INSERT,UPDATE ON prd_studio.projects TO 'prd_studio_runtime'@'localhost';"
            b"FLUSH PRIVILEGES;", timeout=8)
        grants = {line.replace("`", "") for line in _mysql(
            profile, "SHOW GRANTS FOR 'prd_studio_runtime'@'localhost'", 5)}
        expected_grants = {
            "GRANT USAGE ON *.* TO prd_studio_runtime@localhost",
            "GRANT SELECT ON prd_studio.schema_versions TO prd_studio_runtime@localhost",
            "GRANT SELECT, INSERT, UPDATE ON prd_studio.projects TO prd_studio_runtime@localhost",
        }
        _require(grants == expected_grants, "RUNTIME_DATABASE_GRANTS_INVALID")
        grants_sha256 = _sha256_bytes("\n".join(sorted(grants)).encode("ascii"))
        self.state["runtime_password"] = runtime_password
        password_path = config_dir / "db-password"
        _atomic_write(password_path, (runtime_password + "\n").encode("ascii"), 0o400)
        os.chown(password_path, service_account.pw_uid, service_group.gr_gid)
        application_env = (
            "DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_SOCKET_PATH="
            + profile["remote"]["mysql_socket_path"]
            + "\nDB_USER=prd_studio_runtime\nDB_PASSWORD_FILE=/etc/prd-studio/db-password\n"
              "DB_NAME=prd_studio\nDB_SSL_MODE=disabled\n"
        ).encode("ascii")
        _atomic_write(config_dir / "prd-studio.env", application_env, 0o400)
        _parse_env(config_dir / "prd-studio.env", kind="runtime")
        rows = _mysql(profile,
            "SELECT COUNT(*),COALESCE(MAX(version),0),COALESCE(MAX(checksum),'') FROM prd_studio.schema_versions;"
            "SELECT COUNT(*) FROM prd_studio.projects", 8)
        _require(len(rows) == 2 and rows[0] == f"1\t1\t{schema_sha}" and rows[1] == "0",
                 "SCHEMA_OR_EMPTY_STATE_MISMATCH")
        cursor_rc, cursor_out, _ = _run([
            "/usr/bin/journalctl", "-u", SERVICE, "-n", "0", "--show-cursor", "--no-pager",
        ], 5, max_output=32 * 1024)
        _require(cursor_rc == 0, "JOURNAL_CURSOR_FAILED")
        cursors = [line[11:] for line in cursor_out.decode("utf-8", "strict").splitlines()
                   if line.startswith("-- cursor: ")]
        _require(len(cursors) == 1 and 1 <= len(cursors[0]) <= 512,
                 "JOURNAL_CURSOR_INVALID")
        self.state["journal_cursor"] = cursors[0]
        rc, _ = _systemctl("start", SERVICE, timeout=20)
        _require(rc == 0, "SERVICE_START_FAILED")
        self.state["service_started"] = True
        nginx_rc, _stdout, _stderr = _run(["/usr/sbin/nginx", "-t"], 8, max_output=64 * 1024)
        _require(nginx_rc == 0, "NGINX_CONFIGURATION_INVALID")
        nginx_reload, _ = _systemctl("reload", "nginx.service", timeout=10)
        _require(nginx_reload == 0, "NGINX_RELOAD_FAILED")
        active_nginx_sha256 = _nginx_activation_installed(profile)
        return {"classification": "reset_to_empty", "schema_version": 1,
                "schema_sha256": schema_sha, "project_count": 0,
                "runtime_grants_sha256": grants_sha256,
                "migration_identity_removed": True,
                "active_nginx_sha256": active_nginx_sha256,
                "schema_runner_passes": 2}

    def _gate_live_identity(self) -> dict[str, Any]:
        assert self.state is not None
        candidate = self.state["candidate"]
        status, body = _unix_request("/prd-studio/readyz", trusted=True)
        _require(status == 200, "LIVE_IDENTITY_NOT_READY")
        document = _safe_json_body(body)
        identity = document.get("identity")
        expected = {"commit": candidate["commit"], "tree": candidate["tree"],
                    "artifactSha256": candidate["artifact_sha256"]}
        _require(document.get("status") == "ready" and identity == expected,
                 "LIVE_IDENTITY_MISMATCH")
        _require(CURRENT_LINK.is_symlink() and os.readlink(CURRENT_LINK) == str(
            RELEASES_ROOT / candidate["artifact_sha256"]), "CURRENT_LINK_IDENTITY_MISMATCH")
        return {"commit": candidate["commit"], "tree": candidate["tree"],
                "artifact_sha256": candidate["artifact_sha256"]}

    def _gate_serving_topology(self) -> dict[str, Any]:
        rc, output = _systemctl("show", SERVICE, "--property=ActiveState", "--property=SubState",
                                "--property=MainPID", "--property=ExecMainPID",
                                "--property=ControlGroup", timeout=8)
        _require(rc == 0, "SERVICE_TOPOLOGY_QUERY_FAILED")
        values = {}
        for line in output.decode("ascii", "strict").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        _require(values.get("ActiveState") == "active" and values.get("SubState") == "running"
                 and int(values.get("MainPID", "0")) > 1
                 and values.get("ExecMainPID") == values.get("MainPID"),
                 "SERVICE_TOPOLOGY_MISMATCH")
        socket_info = os.lstat(HTTP_SOCKET)
        import grp
        import pwd
        group = grp.getgrnam("prd-studio-socket")
        account = pwd.getpwnam("prd-studio")
        _require(stat.S_ISSOCK(socket_info.st_mode) and socket_info.st_uid == account.pw_uid
                 and socket_info.st_gid == group.gr_gid
                 and stat.S_IMODE(socket_info.st_mode) == 0o660,
                 "HTTP_LISTENER_NOT_ACCESSIBLE_UNIX_SOCKET")
        assert self.state is not None
        capture = BACKUP_ROOT / f"{self.state['release_id']}.sockets.txt"
        _require(not capture.exists(), "SOCKET_CAPTURE_ALREADY_EXISTS")
        fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            child = subprocess.Popen(["/usr/bin/ss", "-H", "-xlpn"], stdin=subprocess.DEVNULL,
                                     stdout=fd, stderr=subprocess.DEVNULL, start_new_session=True,
                                     env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise WorkerError("SOCKET_CAPTURE_TIMEOUT") from None
            _require(child.returncode == 0, "SOCKET_CAPTURE_FAILED")
            os.fsync(fd)
        finally:
            os.close(fd)
        captured = _read_regular(capture, 1024 * 1024).decode("utf-8", "strict")
        main_pid = values["MainPID"]
        matching = [line for line in captured.splitlines() if str(HTTP_SOCKET) in line]
        _require(len(matching) == 1 and f"pid={main_pid}," in matching[0],
                 "SOCKET_OWNER_TOPOLOGY_MISMATCH")
        control_group = values.get("ControlGroup", "")
        _require(re.fullmatch(r"/[A-Za-z0-9_.@:/\\-]{1,512}", control_group) is not None
                 and ".." not in pathlib.PurePosixPath(control_group).parts,
                 "SERVICE_CGROUP_INVALID")
        cgroup_source = pathlib.Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
        cgroup_capture = BACKUP_ROOT / f"{self.state['release_id']}.cgroup-procs.txt"
        _require(not cgroup_capture.exists(), "CGROUP_CAPTURE_ALREADY_EXISTS")
        cgroup_fd = os.open(cgroup_capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            child = subprocess.Popen(["/usr/bin/cat", str(cgroup_source)],
                                     stdin=subprocess.DEVNULL, stdout=cgroup_fd,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
            child.wait(timeout=5)
            _require(child.returncode == 0, "CGROUP_CAPTURE_FAILED")
            os.fsync(cgroup_fd)
        finally:
            os.close(cgroup_fd)
        process_ids = _read_regular(cgroup_capture, 64 * 1024).decode("ascii", "strict").splitlines()
        _require(process_ids == [main_pid], "SERVICE_PROCESS_COUNT_MISMATCH")
        status_capture = BACKUP_ROOT / f"{self.state['release_id']}.main-status.txt"
        status_fd = os.open(status_capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            child = subprocess.Popen(["/usr/bin/cat", f"/proc/{main_pid}/status"],
                                     stdin=subprocess.DEVNULL, stdout=status_fd,
                                     stderr=subprocess.DEVNULL, start_new_session=True,
                                     env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
            child.wait(timeout=5)
            _require(child.returncode == 0, "PROCESS_STATUS_CAPTURE_FAILED")
            os.fsync(status_fd)
        finally:
            os.close(status_fd)
        status_lines = _read_regular(status_capture, 128 * 1024).decode("ascii", "strict").splitlines()
        group_lines = [line for line in status_lines if line.startswith("Groups:")]
        secret_group = grp.getgrnam("prd-studio")
        _require(len(group_lines) == 1
                 and set(group_lines[0].split()[1:]) == {
                     str(secret_group.gr_gid), str(group.gr_gid)},
                 "SERVICE_PROCESS_GROUP_BOUNDARY_INVALID")
        password_details = os.lstat(APPLICATION_CONFIG_DIR / "db-password")
        _require(password_details.st_uid == account.pw_uid
                 and password_details.st_gid == secret_group.gr_gid
                 and stat.S_IMODE(password_details.st_mode) == 0o400,
                 "RUNTIME_PASSWORD_IDENTITY_INVALID")
        for protected_path in (
            self.state["profile"]["remote"]["nginx_auth_file"],
            self.state["profile"]["remote"]["authorization_header_file"],
        ):
            auth_details = os.lstat(pathlib.Path(protected_path))
            owner_read = (auth_details.st_uid == account.pw_uid
                          and stat.S_IMODE(auth_details.st_mode) & 0o400)
            group_read = (auth_details.st_gid in {secret_group.gr_gid, group.gr_gid}
                          and stat.S_IMODE(auth_details.st_mode) & 0o040)
            world_read = stat.S_IMODE(auth_details.st_mode) & 0o004
            _require(not owner_read and not group_read and not world_read,
                     "APPLICATION_CAN_READ_PROXY_CREDENTIAL")
            probe_rc, _probe_out, _probe_err = _run([
                "/usr/bin/setpriv", f"--reuid={account.pw_uid}",
                f"--regid={group.gr_gid}", f"--groups={secret_group.gr_gid}", "--",
                "/usr/bin/test", "-r", protected_path,
            ], 3, max_output=1024)
            _require(probe_rc == 1, "APPLICATION_EFFECTIVE_READ_ACCESS_FORBIDDEN")
        id_rc, id_out, _id_err = _run([
            "/usr/bin/id", "-G", self.state["profile"]["remote"]["nginx_worker_user"],
        ], 3, max_output=4096)
        _require(id_rc == 0, "NGINX_GROUP_QUERY_FAILED")
        nginx_groups = set(id_out.decode("ascii", "strict").split())
        _require(str(group.gr_gid) in nginx_groups and str(secret_group.gr_gid) not in nginx_groups,
                 "NGINX_SECRET_GROUP_BOUNDARY_INVALID")
        for protected_path in (
            str(APPLICATION_CONFIG_DIR / "db-password"),
            str(APPLICATION_CONFIG_DIR / "prd-studio.env"),
            str(WRITE_FENCE),
        ):
            probe_rc, _probe_out, _probe_err = _run([
                "/usr/sbin/runuser", "--user",
                self.state["profile"]["remote"]["nginx_worker_user"], "--",
                "/usr/bin/test", "-r", protected_path,
            ], 3, max_output=1024)
            _require(probe_rc == 1, "NGINX_EFFECTIVE_READ_ACCESS_FORBIDDEN")
        nginx_rc, _stdout, _stderr = _run(["/usr/sbin/nginx", "-t"], 8, max_output=64 * 1024)
        _require(nginx_rc == 0, "NGINX_CONFIGURATION_INVALID")
        active_nginx_sha256 = _nginx_activation_installed(self.state["profile"])
        return {"service": SERVICE, "process_count": 1, "listener": "unix-socket",
                "nginx_configuration": "valid", "credential_groups_separated": True,
                "active_nginx_sha256": active_nginx_sha256}

    def _gate_health(self) -> dict[str, Any]:
        assert self.state is not None
        candidate = self.state["candidate"]
        digest = None
        for _ in range(3):
            status, body = _unix_request("/prd-studio/readyz", trusted=True)
            _require(status == 200, "READINESS_SAMPLE_FAILED")
            document = _safe_json_body(body)
            sample = _sha256_bytes(json.dumps(document, sort_keys=True).encode())
            _require(document.get("identity", {}).get("artifactSha256") == candidate["artifact_sha256"],
                     "READINESS_IDENTITY_MISMATCH")
            if digest is not None:
                _require(sample == digest, "READINESS_NOT_STABLE")
            digest = sample
            time.sleep(0.1)
        return {"sample_count": 3, "stable_response_sha256": digest}

    def _gate_auth(self) -> dict[str, Any]:
        rejected, _ = _unix_request("/prd-studio/api/projects", trusted=False)
        accepted, body = _unix_request("/prd-studio/api/projects", trusted=True)
        _require(rejected == 401 and accepted == 200, "TRUSTED_PROXY_AUTH_CONTRACT_FAILED")
        spoofed, _ = _public_request(self.state["profile"], "/api/projects",
                                     credential="none", spoof_proxy=True)
        wrong, _ = _public_request(self.state["profile"], "/api/projects", credential="invalid")
        edge_valid, edge_body = _public_request(self.state["profile"], "/api/projects")
        _require(spoofed == 401 and wrong in {401, 429} and edge_valid == 200,
                 "PUBLIC_EDGE_AUTH_CONTRACT_FAILED")
        try:
            listing = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkerError("AUTH_ACCEPTANCE_BODY_INVALID") from error
        _require(listing == [] and json.loads(edge_body.decode("utf-8")) == [],
                 "AUTH_ACCEPTANCE_STATE_NOT_EMPTY")
        return {"unauthenticated_status": 401, "trusted_proxy_status": 200,
                "spoofed_edge_status": 401, "wrong_edge_status": wrong,
                "valid_edge_status": 200, "session_created": False}

    def _gate_logs(self) -> dict[str, Any]:
        assert self.state is not None
        cursor = self.state.get("journal_cursor")
        _require(isinstance(cursor, str), "JOURNAL_CURSOR_MISSING")
        capture_dir = BACKUP_ROOT
        capture = capture_dir / f"{self.state['release_id']}.journal.jsonl"
        _require(not capture.exists(), "JOURNAL_CAPTURE_ALREADY_EXISTS")
        fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            child = subprocess.Popen([
                "/usr/bin/journalctl", "-u", SERVICE, f"--after-cursor={cursor}",
                "--output=json", "--no-pager",
            ], stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.DEVNULL,
               start_new_session=True)
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise WorkerError("JOURNAL_CAPTURE_TIMEOUT") from None
            _require(child.returncode == 0, "JOURNAL_CAPTURE_FAILED")
            os.fsync(fd)
        finally:
            os.close(fd)
        raw = _read_regular(capture, 4 * 1024 * 1024)
        protected = [str(self.state.get("runtime_password") or ""),
                     str(self.state.get("migration_password") or "")]
        protected.append(_read_regular(pathlib.Path(
            self.state["profile"]["remote"]["authorization_header_file"]), 8192).decode("ascii").strip())
        _require(all(not value or value.encode("utf-8") not in raw for value in protected),
                 "PROTECTED_VALUE_FOUND_IN_LOGS")
        count = 0
        for line in raw.splitlines():
            if not line:
                continue
            count += 1
            try:
                journal = json.loads(line)
            except (json.JSONDecodeError, TypeError) as error:
                raise WorkerError("JOURNAL_RECORD_INVALID") from error
            _require(isinstance(journal, dict) and isinstance(journal.get("MESSAGE"), str),
                     "JOURNAL_RECORD_INVALID")
            raw_message = journal["MESSAGE"]
            source = journal.get("_COMM") or journal.get("SYSLOG_IDENTIFIER")
            if source == "node":
                try:
                    message = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError) as error:
                    raise WorkerError("APPLICATION_LOG_UNSTRUCTURED") from error
                _require(isinstance(message, dict)
                         and set(message).issubset({"level", "event", "code", "signal"})
                         and set(message).issuperset({"level", "event"}),
                         "APPLICATION_LOG_FIELDS_INVALID")
                event_code = (message.get("event"), message.get("code"))
                _require(message.get("level") == "info"
                         and event_code in ALLOWED_APP_LOG_RECORDS
                         and message.get("event") not in FATAL_LOG_EVENTS
                         and message.get("code") != "INVALID_STORED_DATA",
                         "APPLICATION_LOG_EVENT_NOT_ALLOWED")
            elif source == "systemd":
                _require(re.fullmatch(
                    r"Started (?:prd-studio\.service - )?BankKaro PRD Studio\.",
                    raw_message) is not None,
                    "SERVICE_MANAGER_LOG_NOT_ALLOWED")
            else:
                raise WorkerError("JOURNAL_SOURCE_NOT_ALLOWED")
        self.state["migration_password"] = None
        return {"record_count": count, "capture_sha256": _sha256_bytes(raw),
                "protected_value_count": 0}

    def _gate_out_of_scope(self) -> dict[str, Any]:
        assert self.state is not None
        after = _probe_out_of_scope(self.state["profile"])
        _require(after == self.state.get("initial_out_of_scope"), "OUT_OF_SCOPE_CHANGED")
        return {"probe_count": len(after),
                "probe_set_sha256": _sha256_bytes(json.dumps(after, sort_keys=True).encode())}

    def _gate_crud(self) -> dict[str, Any]:
        assert self.state is not None
        _require(WRITE_FENCE.is_file(), "WRITE_FENCE_MISSING_BEFORE_CRUD")
        backup_attestation = self.state["manifest"].get("attestations", {}).get(
            "provider_contract_sha256")
        _require(isinstance(backup_attestation, str)
                 and HEX64.fullmatch(backup_attestation) is not None
                 and backup_attestation == self.state["profile"].get(
                     "day2_backup_recovery", {}).get("sha256"),
                 "DAY2_BACKUP_RECOVERY_NOT_ATTESTED")
        WRITE_FENCE.unlink()
        try:
            fixture = json.loads(json.dumps(self.state["fixture"]))
            project_id = self.state["synthetic_id"]
            create = {"id": project_id, "slug": fixture["create"]["slug"],
                      "data": fixture["create"]["data"]}
            status, body = _public_request(self.state["profile"], "/api/projects",
                                           method="POST", payload=create)
            _require(status == 201 and _safe_json_body(body).get("version") == 1,
                     "SYNTHETIC_CREATE_FAILED")
            status, body = _public_request(self.state["profile"], f"/api/projects/{project_id}")
            read_one = _safe_json_body(body)
            _require(status == 200 and read_one.get("version") == 1
                     and read_one.get("data") == create["data"], "SYNTHETIC_READ_FAILED")
            updated = json.loads(json.dumps(create["data"]))
            updated["meta"]["title"] = fixture["update"]["title"]
            updated["meta"]["status"] = fixture["update"]["status"]
            status, body = _public_request(self.state["profile"], f"/api/projects/{project_id}",
                                           method="PUT", payload={"version": 1, "data": updated})
            _require(status == 200 and _safe_json_body(body).get("version") == 2,
                     "SYNTHETIC_UPDATE_FAILED")
            status, body = _public_request(self.state["profile"], f"/api/projects/{project_id}")
            read_two = _safe_json_body(body)
            _require(status == 200 and read_two.get("version") == 2
                     and read_two.get("data") == updated, "SYNTHETIC_SECOND_READ_FAILED")
            rows = _mysql(self.state["profile"],
                          f"DELETE FROM prd_studio.projects WHERE id='{project_id}'; SELECT ROW_COUNT()", 8)
            _require(rows == ["1"], "SYNTHETIC_CLEANUP_FAILED")
            remaining = _mysql(self.state["profile"], "SELECT COUNT(*) FROM prd_studio.projects", 5)
            _require(remaining == ["0"], "SYNTHETIC_CLEANUP_NOT_EMPTY")
            result = {"fixture_id": fixture["fixture_id"], "created_version": 1,
                      "updated_version": 2, "residual_rows": 0,
                      "final_payload_sha256": _sha256_bytes(
                          json.dumps(updated, sort_keys=True, separators=(",", ":")).encode()),
                      "day2_backup_recovery_sha256": backup_attestation,
                      "write_fence_reestablished": True}
        finally:
            _establish_write_fence()
        return result

    def _incident_containment(self, reason: str) -> None:
        assert self.state is not None
        profile = self.state["profile"]
        accounts_locked = False
        try:
            accounts = _mysql(profile,
                "SELECT User FROM mysql.user WHERE Host='localhost' AND User IN "
                "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
            for account_name in accounts:
                if account_name in {RUNTIME_DB_USER, MIGRATION_DB_USER}:
                    _mysql(profile, f"ALTER USER '{account_name}'@'localhost' ACCOUNT LOCK", 5)
            locked = _mysql(profile,
                "SELECT User,account_locked FROM mysql.user WHERE Host='localhost' AND User IN "
                "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
            accounts_locked = locked == [name + "\tY" for name in accounts]
        except Exception:
            accounts_locked = False
        nginx_path = pathlib.Path(profile["remote"]["nginx_include_path"])
        if self.state.get("nginx_parents_patched"):
            _restore_nginx_parents(self.state)
        nginx_path.unlink(missing_ok=True)
        pathlib.Path(profile["remote"]["nginx_http_include_path"]).unlink(missing_ok=True)
        if self.state.get("nginx_socket_membership_added"):
            remove_rc, _out, _err = _run([
                "/usr/bin/gpasswd", "-d", profile["remote"]["nginx_worker_user"],
                "prd-studio-socket",
            ], 8, max_output=64 * 1024)
            _require(remove_rc == 0, "INCIDENT_NGINX_GROUP_REMOVAL_FAILED")
            self.state["nginx_socket_membership_added"] = False
        containment_rc, _out, _err = _run(["/usr/sbin/nginx", "-t"], 8,
                                          max_output=64 * 1024)
        _require(containment_rc == 0, "INCIDENT_CONTAINMENT_NGINX_INVALID")
        containment_reload, _ = _systemctl("reload", "nginx.service", timeout=10)
        _require(containment_reload == 0, "INCIDENT_CONTAINMENT_NGINX_RELOAD_FAILED")
        incident = BACKUP_ROOT / f"{self.state['release_id']}.incident.json"
        if not incident.exists():
            _write_exclusive(incident, {
                "schema_version": "1.0", "release_id": self.state["release_id"],
                "status": "INCIDENT", "reason_code": reason,
                "database_preserved": True, "application_accounts_locked": accounts_locked,
                "route_withdrawn": True, "recorded_epoch": int(time.time()),
            })
        raise WorkerError(reason)

    def _rollback_impl(self) -> dict[str, Any]:
        assert self.state is not None
        profile = self.state["profile"]
        import grp
        try:
            grp.getgrnam("prd-studio")
            _establish_write_fence()
        except KeyError:
            _require(not SYSTEMD_UNIT.exists() and not _database_exists(profile),
                     "ROLLBACK_SERVICE_GROUP_MISSING")
        if SYSTEMD_UNIT.exists():
            stop_rc, _ = _systemctl("disable", "--now", SERVICE, timeout=15)
            _require(stop_rc == 0, "ROLLBACK_SERVICE_STOP_FAILED")
            show_rc, show_out = _systemctl("show", SERVICE, "--property=ActiveState",
                                           "--property=MainPID", timeout=5)
            _require(show_rc == 0 and b"ActiveState=inactive" in show_out
                     and b"MainPID=0" in show_out, "ROLLBACK_SERVICE_NOT_STOPPED")
        else:
            active_rc, _ = _systemctl("is-active", "--quiet", SERVICE, timeout=5)
            _require(active_rc != 0, "ROLLBACK_UNMANAGED_SERVICE_ACTIVE")

        try:
            existing_accounts = _mysql(profile,
                "SELECT User FROM mysql.user WHERE Host='localhost' AND User IN "
                "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
            _require(not existing_accounts or self.state.get("database_provisioning_started"),
                     "UNEXPECTED_DATABASE_APPLICATION_ACCOUNT")
            for account_name in existing_accounts:
                owned = ((account_name == RUNTIME_DB_USER and self.state.get("runtime_user_created"))
                         or (account_name == MIGRATION_DB_USER
                             and self.state.get("migration_user_created")))
                _require(bool(owned), "AMBIGUOUS_DATABASE_ACCOUNT_OWNERSHIP")
            for account_name in existing_accounts:
                _require(account_name in {RUNTIME_DB_USER, MIGRATION_DB_USER},
                         "DATABASE_ACCOUNT_RESULT_INVALID")
                _mysql(profile, f"ALTER USER '{account_name}'@'localhost' ACCOUNT LOCK", 5)
            if existing_accounts:
                locked = _mysql(profile,
                    "SELECT User,account_locked FROM mysql.user WHERE Host='localhost' AND User IN "
                    "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
                _require(locked == [name + "\tY" for name in existing_accounts],
                         "DATABASE_ACCOUNT_LOCK_FAILED")
            if (self.state.get("database_created") and not self.state.get("schema_completed")
                    and not self.state.get("service_started") and _database_exists(profile)):
                snapshot = {"database_exists": True, "attempt_created_unserved_partial": True}
                permitted, reason = True, "RESET_GUARD_UNSERVED_PARTIAL_CREATED_BY_ATTEMPT"
            else:
                snapshot = _reset_snapshot(self.state)
                permitted, reason = evaluate_reset_guard(
                    snapshot, self.state.get("schema_sha256", ""), self.state["synthetic_id"])
        except WorkerError as error:
            self._incident_containment(error.reason_code)
            raise AssertionError("unreachable")
        if not permitted:
            self._incident_containment(reason)
        if snapshot.get("database_exists"):
            _mysql(profile, "DROP DATABASE prd_studio", 15)
        for account_name in existing_accounts:
            _mysql(profile, f"DROP USER '{account_name}'@'localhost'", 8)
        if existing_accounts:
            _mysql(profile, "FLUSH PRIVILEGES", 5)
        _require(_mysql(profile,
            "SELECT COUNT(*) FROM mysql.user WHERE Host='localhost' AND User IN "
            "('prd_studio_runtime','prd_studio_migration')", 5) == ["0"],
            "ROLLBACK_DATABASE_ACCOUNT_REMAINS")
        self.state["runtime_user_created"] = False
        self.state["migration_user_created"] = False
        nginx_path = pathlib.Path(self.state["profile"]["remote"]["nginx_include_path"])
        if self.state.get("nginx_parents_patched"):
            _restore_nginx_parents(self.state)
        nginx_path.unlink(missing_ok=True)
        pathlib.Path(self.state["profile"]["remote"]["nginx_http_include_path"]).unlink(missing_ok=True)
        if self.state.get("nginx_socket_membership_added"):
            remove_rc, _out, _err = _run([
                "/usr/bin/gpasswd", "-d", profile["remote"]["nginx_worker_user"],
                "prd-studio-socket",
            ], 8, max_output=64 * 1024)
            _require(remove_rc == 0, "ROLLBACK_NGINX_GROUP_REMOVAL_FAILED")
            self.state["nginx_socket_membership_added"] = False
        nginx_rc, _stdout, _stderr = _run(["/usr/sbin/nginx", "-t"], 8, max_output=64 * 1024)
        _require(nginx_rc == 0, "ROLLBACK_NGINX_CONFIGURATION_INVALID")
        reload_rc, _ = _systemctl("reload", "nginx.service", timeout=10)
        _require(reload_rc == 0, "ROLLBACK_NGINX_RELOAD_FAILED")
        SYSTEMD_UNIT.unlink(missing_ok=True)
        RELEASE_ENV.unlink(missing_ok=True)
        config_dir = APPLICATION_CONFIG_DIR
        if self.state.get("application_config_created") and config_dir.exists():
            shutil.rmtree(config_dir)
        if CURRENT_LINK.is_symlink() or CURRENT_LINK.exists():
            CURRENT_LINK.unlink()
        release_dir = RELEASES_ROOT / self.state["candidate"]["artifact_sha256"]
        if release_dir.exists():
            identity = release_dir / ".release-identity.json"
            _require(identity.is_file(), "ROLLBACK_RELEASE_IDENTITY_MISSING")
            try:
                release_identity = json.loads(_read_regular(identity, 8192))
            except json.JSONDecodeError as error:
                raise WorkerError("ROLLBACK_RELEASE_IDENTITY_INVALID") from error
            _require(release_identity.get("artifact_sha256")
                     == self.state["candidate"]["artifact_sha256"],
                     "ROLLBACK_RELEASE_IDENTITY_MISMATCH")
            shutil.rmtree(release_dir)
        temporary = RELEASES_ROOT / ("." + self.state["candidate"]["artifact_sha256"] + ".new")
        if self.state.get("temporary_release_created") and temporary.exists():
            shutil.rmtree(temporary)
        daemon_rc, _ = _systemctl("daemon-reload", timeout=10)
        _require(daemon_rc == 0, "ROLLBACK_SYSTEMD_RELOAD_FAILED")
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        if WRITE_FENCE.exists():
            WRITE_FENCE.unlink()
        if WRITE_FENCE_DIR.exists():
            WRITE_FENCE_DIR.rmdir()
        stage = pathlib.Path(self.state["staged_path"])
        stage.unlink(missing_ok=True)
        if self.state.get("private_state_root_created") and PRIVATE_STATE_ROOT.exists():
            shutil.rmtree(PRIVATE_STATE_ROOT)
        if self.state.get("app_root_created") and APP_ROOT.exists():
            shutil.rmtree(APP_ROOT)
        import pwd
        try:
            pwd.getpwnam("prd-studio")
            _require(bool(self.state.get("service_user_created")),
                     "AMBIGUOUS_SERVICE_USER_OWNERSHIP")
            user_rc, _out, _err = _run(["/usr/sbin/userdel", "prd-studio"], 8, max_output=64 * 1024)
            _require(user_rc == 0, "ROLLBACK_SERVICE_USER_REMOVAL_FAILED")
        except KeyError:
            pass
        try:
            grp.getgrnam("prd-studio")
            _require(bool(self.state.get("service_group_created")),
                     "AMBIGUOUS_SERVICE_GROUP_OWNERSHIP")
            group_rc, _out, _err = _run(["/usr/sbin/groupdel", "prd-studio"], 8, max_output=64 * 1024)
            _require(group_rc == 0, "ROLLBACK_SERVICE_GROUP_REMOVAL_FAILED")
        except KeyError:
            pass
        try:
            grp.getgrnam("prd-studio-socket")
            _require(bool(self.state.get("socket_group_created")),
                     "AMBIGUOUS_SOCKET_GROUP_OWNERSHIP")
            socket_group_rc, _out, _err = _run(
                ["/usr/sbin/groupdel", "prd-studio-socket"], 8, max_output=64 * 1024)
            _require(socket_group_rc == 0, "ROLLBACK_SOCKET_GROUP_REMOVAL_FAILED")
        except KeyError:
            pass
        return {"reset_guard": reason, "database_absent": True,
                "managed_target_absent": True}

    def _best_effort_incident_containment(self, reason: str) -> None:
        """Independently repeat every containment boundary after rollback failure."""
        assert self.state is not None
        profile = self.state["profile"]
        fenced = stopped = accounts_locked = route_withdrawn = False
        try:
            _establish_write_fence()
            fenced = True
        except Exception:
            pass
        try:
            _systemctl("disable", "--now", SERVICE, timeout=15)
            check_rc, check_out = _systemctl(
                "show", SERVICE, "--property=ActiveState", "--property=MainPID", timeout=5)
            stopped = (check_rc == 0 and b"ActiveState=inactive" in check_out
                       and b"MainPID=0" in check_out)
        except Exception:
            pass
        try:
            accounts = _mysql(profile,
                "SELECT User FROM mysql.user WHERE Host='localhost' AND User IN "
                "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
            for account_name in accounts:
                if account_name in {RUNTIME_DB_USER, MIGRATION_DB_USER}:
                    _mysql(profile, f"ALTER USER '{account_name}'@'localhost' ACCOUNT LOCK", 5)
            locked = _mysql(profile,
                "SELECT User,account_locked FROM mysql.user WHERE Host='localhost' AND User IN "
                "('prd_studio_runtime','prd_studio_migration') ORDER BY User", 5)
            accounts_locked = locked == [name + "\tY" for name in accounts]
        except Exception:
            pass
        try:
            if self.state.get("nginx_parents_patched"):
                _restore_nginx_parents(self.state)
            pathlib.Path(profile["remote"]["nginx_include_path"]).unlink(missing_ok=True)
            pathlib.Path(profile["remote"]["nginx_http_include_path"]).unlink(missing_ok=True)
            check_rc, _out, _err = _run(["/usr/sbin/nginx", "-t"], 8,
                                        max_output=64 * 1024)
            if check_rc == 0:
                reload_rc, _ = _systemctl("reload", "nginx.service", timeout=10)
                route_withdrawn = reload_rc == 0
        except Exception:
            pass
        if BACKUP_ROOT.is_dir():
            marker = BACKUP_ROOT / f"{self.state['release_id']}.incident.json"
            if not marker.exists():
                try:
                    _write_exclusive(marker, {
                        "schema_version": "1.0", "release_id": self.state["release_id"],
                        "status": "INCIDENT", "reason_code": reason,
                        "write_fenced": fenced, "service_stopped": stopped,
                        "application_accounts_locked": accounts_locked,
                        "route_withdrawn": route_withdrawn,
                        "database_preserved": True, "recorded_epoch": int(time.time()),
                    })
                except Exception:
                    pass

    def _rollback(self) -> dict[str, Any]:
        try:
            return self._rollback_impl()
        except BaseException as error:
            reason = error.reason_code if isinstance(error, WorkerError) else "ROLLBACK_UNEXPECTED"
            self._best_effort_incident_containment(reason)
            if isinstance(error, WorkerError):
                raise
            raise WorkerError(reason) from error

    def emergency_rollback(self) -> None:
        if not self.state or not self.state.get("mutation_armed") or self.state.get("terminal"):
            return
        if self.state.get("commit_in_doubt"):
            # The canonical controller may or may not already be terminal.  The
            # durable fence is the safety boundary; preserve the exact participant
            # state for the separately locked receipt-driven reconciler.
            return
        try:
            self._rollback()
            self.state["terminal"] = True
            self.state["mutation_armed"] = False
        except Exception as error:
            # _rollback performs containment before raising on an unsafe reset guard.
            if BACKUP_ROOT.is_dir():
                marker = BACKUP_ROOT / f"{self.state['release_id']}.incident.json"
                if not marker.exists():
                    reason = error.reason_code if isinstance(error, WorkerError) else "EMERGENCY_ROLLBACK_FAILED"
                    try:
                        _write_exclusive(marker, {
                            "schema_version": "1.0", "release_id": self.state["release_id"],
                            "status": "INCIDENT", "reason_code": reason,
                            "recorded_epoch": int(time.time()),
                        })
                    except Exception:
                        pass

    def _gate_rollback_verification(self) -> dict[str, Any]:
        assert self.state is not None
        _ensure_absent_baseline(self.state)
        restored_configuration = _capture_configuration(self.state["profile"])
        _nginx_activation_baseline(self.state["profile"])
        status, _body = _public_request(self.state["profile"], "/")
        _require(status == self.state["profile"]["remote"]["expected_absence_status"],
                 "ROLLBACK_ROUTE_STATUS_MISMATCH")
        _require(_probe_out_of_scope(self.state["profile"]) == self.state.get("initial_out_of_scope"),
                 "ROLLBACK_OUT_OF_SCOPE_CHANGED")
        rollback = self.state["manifest"]["components"][0]["rollback"]
        return {"commit": rollback["commit"], "tree": rollback["tree"],
                "artifact_sha256": rollback["artifact_sha256"],
                "state_integrity": "canonical-absence",
                "restored_configuration_sha256": _sha256_bytes(
                    json.dumps(restored_configuration, sort_keys=True).encode())}

    def request(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise WorkerError("RUNNER_EXECUTION_NOT_CERTIFIED")

        # Unreachable until the recovery protocol receives independent fault
        # certification. Kept as reviewable implementation material only.
        if op == "initialize":
            return self.initialize(payload)
        if op == "initialize-reconcile":
            raise WorkerError("RUNNER_EXECUTION_NOT_CERTIFIED")
        _require(self.state is not None and self.lock_fd is not None, "WORKER_NOT_INITIALIZED")
        _require(fcntl.fcntl(self.lock_fd, fcntl.F_GETFD) >= 0, "GLOBAL_LOCK_LOST")
        if op == "live-baseline":
            evidence = self._gate_live_baseline()
        elif op == "configuration-contract":
            evidence = self._gate_configuration()
        elif op == "private-backup":
            evidence = self._gate_private_backup()
        elif op == "exact-install":
            evidence = self._gate_exact_install()
        elif op == "state-transition":
            evidence = self._gate_state_transition()
        elif op == "live-identity":
            evidence = self._gate_live_identity()
        elif op == "serving-topology":
            evidence = self._gate_serving_topology()
        elif op == "health-readiness":
            evidence = self._gate_health()
        elif op == "auth-contract":
            evidence = self._gate_auth()
        elif op == "bounded-logs":
            evidence = self._gate_logs()
        elif op == "out-of-scope-unchanged":
            evidence = self._gate_out_of_scope()
        elif op == "prd-studio-crud-smoke":
            evidence = self._gate_crud()
        elif op == "rollback":
            evidence = self._rollback()
        elif op == "rollback-verification":
            evidence = self._gate_rollback_verification()
            self.state["terminal"] = True
            self.state["mutation_armed"] = False
        elif op == "arm-mutation":
            _require(not self.state.get("mutation_armed"), "MUTATION_ALREADY_ARMED")
            self.state["mutation_armed"] = True
            _require(not PRIVATE_STATE_ROOT.exists() and not APP_ROOT.exists(),
                     "FIRST_INSTALL_ROOT_NOT_ABSENT")
            group_rc, _out, _err = _run(["/usr/sbin/groupadd", "--system", "prd-studio"], 8,
                                        max_output=64 * 1024)
            _require(group_rc == 0, "SERVICE_GROUP_CREATE_FAILED")
            import grp
            try:
                grp.getgrnam("prd-studio")
                self.state["service_group_created"] = True
            except KeyError:
                raise WorkerError("SERVICE_GROUP_CREATE_NOT_OBSERVED") from None
            socket_group_rc, _out, _err = _run(
                ["/usr/sbin/groupadd", "--system", "prd-studio-socket"], 8,
                max_output=64 * 1024)
            _require(socket_group_rc == 0, "SOCKET_GROUP_CREATE_FAILED")
            try:
                grp.getgrnam("prd-studio-socket")
                self.state["socket_group_created"] = True
            except KeyError:
                raise WorkerError("SOCKET_GROUP_CREATE_NOT_OBSERVED") from None
            user_rc, _out, _err = _run([
                "/usr/sbin/useradd", "--system", "--gid", "prd-studio",
                "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "prd-studio",
            ], 8, max_output=64 * 1024)
            _require(user_rc == 0, "SERVICE_USER_CREATE_FAILED")
            import pwd
            try:
                pwd.getpwnam("prd-studio")
                self.state["service_user_created"] = True
            except KeyError:
                raise WorkerError("SERVICE_USER_CREATE_NOT_OBSERVED") from None
            nginx_group = grp.getgrnam(self.state["profile"]["remote"]["nginx_worker_group"])
            service_group = grp.getgrnam("prd-studio")
            _require(nginx_group.gr_gid != service_group.gr_gid,
                     "NGINX_AND_SECRET_GROUP_NOT_SEPARATE")
            member_rc, _out, _err = _run([
                "/usr/sbin/usermod", "-a", "-G", "prd-studio-socket",
                self.state["profile"]["remote"]["nginx_worker_user"],
            ], 8, max_output=64 * 1024)
            _require(member_rc == 0, "NGINX_SOCKET_GROUP_MEMBERSHIP_FAILED")
            socket_group = grp.getgrnam("prd-studio-socket")
            if self.state["profile"]["remote"]["nginx_worker_user"] in socket_group.gr_mem:
                self.state["nginx_socket_membership_added"] = True
            _require(self.state["nginx_socket_membership_added"],
                     "NGINX_SOCKET_GROUP_MEMBERSHIP_FAILED")
            APPLICATION_CONFIG_DIR.mkdir(mode=0o750)
            self.state["application_config_created"] = True
            os.chown(APPLICATION_CONFIG_DIR, 0, service_group.gr_gid)
            PRIVATE_STATE_ROOT.mkdir(mode=0o710)
            os.chown(PRIVATE_STATE_ROOT, 0, service_group.gr_gid)
            self.state["private_state_root_created"] = True
            STAGING_ROOT.mkdir(mode=0o700)
            BACKUP_ROOT.mkdir(mode=0o700)
            (STAGING_ROOT / self.state["release_id"]).mkdir(mode=0o700)
            APP_ROOT.mkdir(mode=0o755)
            self.state["app_root_created"] = True
            RELEASES_ROOT.mkdir(mode=0o755)
            recovery = BACKUP_ROOT / f"{self.state['release_id']}.recovery-armed.json"
            digest = _write_exclusive(recovery, {
                "schema_version": "1.0", "release_id": self.state["release_id"],
                "status": "RECOVERY_ARMED", "candidate_sha256": self.state["candidate"]["artifact_sha256"],
                "armed_epoch": int(time.time()),
            })
            self.state["recovery_armed_path"] = str(recovery)
            self.state["recovery_armed_sha256"] = digest
            evidence = {"recovery_state_sha256": digest, "armed": True}
        elif op == "prepare-success":
            _require(self.state.get("mutation_armed") and not self.state.get("terminal"),
                     "SUCCESS_COMMIT_NOT_ARMED")
            _establish_write_fence()
            recovery = pathlib.Path(self.state.get("recovery_armed_path", ""))
            _require(recovery.is_absolute() and recovery.is_file()
                     and _sha256_file(recovery, 8192)
                     == self.state.get("recovery_armed_sha256"),
                     "RECOVERY_ARMED_RECORD_INVALID")
            enable_rc, _ = _systemctl("enable", SERVICE, timeout=10)
            _require(enable_rc == 0, "SERVICE_ENABLE_FAILED")
            prepared = BACKUP_ROOT / f"{self.state['release_id']}.success-prepared.json"
            prepared_sha256 = _write_exclusive(prepared, {
                "schema_version": "1.0", "release_id": self.state["release_id"],
                "status": "SUCCESS_PREPARED", "candidate_sha256":
                    self.state["candidate"]["artifact_sha256"],
                "write_fenced": True, "prepared_epoch": int(time.time()),
            })
            self.state["success_prepared_path"] = str(prepared)
            self.state["success_prepared_sha256"] = prepared_sha256
            self.state["commit_prepared"] = True
            evidence = {"recovery_armed": True, "service_enabled": True,
                        "writes_open": False, "success_prepared_sha256": prepared_sha256}
        elif op == "enter-commit-in-doubt":
            _require(self.state.get("mutation_armed") and self.state.get("commit_prepared")
                     and not self.state.get("terminal"), "SUCCESS_NOT_PREPARED")
            _establish_write_fence()
            prepared = pathlib.Path(self.state.get("success_prepared_path", ""))
            _require(prepared.is_file() and _sha256_file(prepared, 8192)
                     == self.state.get("success_prepared_sha256"),
                     "SUCCESS_PREPARED_RECORD_INVALID")
            marker = BACKUP_ROOT / f"{self.state['release_id']}.global-commit-in-doubt.json"
            marker_sha256 = _write_exclusive(marker, {
                "schema_version": "1.0", "release_id": self.state["release_id"],
                "status": "GLOBAL_COMMIT_IN_DOUBT", "write_fenced": True,
                "recorded_epoch": int(time.time()),
            })
            self.state["commit_in_doubt_path"] = str(marker)
            self.state["commit_in_doubt_sha256"] = marker_sha256
            self.state["commit_in_doubt"] = True
            evidence = {"write_fenced": True, "global_commit_in_doubt_sha256": marker_sha256}
        elif op == "finalize-success":
            if self.state.get("already_finalized"):
                enabled_rc, _ = _systemctl("is-enabled", "--quiet", SERVICE, timeout=5)
                _require(enabled_rc == 0 and CURRENT_LINK.is_symlink()
                         and os.readlink(CURRENT_LINK) == str(
                             RELEASES_ROOT / self.state["candidate"]["artifact_sha256"]),
                         "FINALIZED_SUCCESS_IDENTITY_INVALID")
                evidence = {"recovery_disarmed": True, "service_enabled": True,
                            "writes_open": True, "idempotent_reconcile": True}
                self.state["terminal"] = True
                self.state["mutation_armed"] = False
                return {"status": "PASS", "reason_code": "FINALIZE_SUCCESS_PASSED",
                        "evidence": evidence}
            _require(self.state.get("mutation_armed") and self.state.get("commit_prepared")
                     and self.state.get("commit_in_doubt") and not self.state.get("terminal"),
                     "GLOBAL_SUCCESS_NOT_COMMITTABLE")
            _establish_write_fence()
            recovery = pathlib.Path(self.state.get("recovery_armed_path", ""))
            in_doubt = pathlib.Path(self.state.get("commit_in_doubt_path", ""))
            _require(recovery.is_file() and _sha256_file(recovery, 8192)
                     == self.state.get("recovery_armed_sha256")
                     and in_doubt.is_file() and _sha256_file(in_doubt, 8192)
                     == self.state.get("commit_in_doubt_sha256"),
                     "GLOBAL_COMMIT_RECORD_INVALID")
            _write_exclusive(BACKUP_ROOT / f"{self.state['release_id']}.recovery-disarmed.json", {
                "schema_version": "1.0", "release_id": self.state["release_id"],
                "status": "RECOVERY_DISARMED", "finished_epoch": int(time.time()),
            })
            recovery.unlink()
            in_doubt.unlink()
            pathlib.Path(self.state["success_prepared_path"]).unlink()
            backup_fd = os.open(BACKUP_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(backup_fd)
            finally:
                os.close(backup_fd)
            _require(not recovery.exists(), "RECOVERY_ARMED_RECORD_REMAINS")
            WRITE_FENCE.unlink()
            fence_fd = os.open(WRITE_FENCE_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fence_fd)
            finally:
                os.close(fence_fd)
            _require(not WRITE_FENCE.exists() and not WRITE_FENCE.is_symlink(),
                     "WRITE_FENCE_RELEASE_FAILED")
            self.state["terminal"] = True
            self.state["mutation_armed"] = False
            self.state["commit_in_doubt"] = False
            evidence = {"recovery_disarmed": True, "service_enabled": True,
                        "writes_open": True}
        elif op == "cleanup-stage":
            stage = pathlib.Path(self.state["staged_path"])
            stage.unlink(missing_ok=True)
            try:
                stage.parent.rmdir()
            except OSError:
                pass
            evidence = {"stage_removed": True}
        elif op == "close":
            if self.state.get("mutation_armed") and not self.state.get("terminal"):
                self.emergency_rollback()
            _require(not self.state.get("mutation_armed"), "CLOSE_RECOVERY_INCOMPLETE")
            return {"status": "PASS", "reason_code": "WORKER_CLOSED",
                    "evidence": {"recovery_complete": True}}
        else:
            raise WorkerError("WORKER_OPERATION_INVALID")
        return {"status": "PASS", "reason_code": op.upper().replace("-", "_") + "_PASSED",
                "evidence": evidence}


def main() -> int:
    # The worker source is review material, not a separately callable live
    # entry point. Fail before signal handlers, stdin, locks, or filesystem use.
    sys.stdout.write(json.dumps(
        {"status": "ERROR", "reason_code": "RUNNER_EXECUTION_NOT_CERTIFIED"},
        sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
