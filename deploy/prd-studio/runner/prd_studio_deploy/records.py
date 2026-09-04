"""Deterministic JSON and private-file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
from typing import Any

from .errors import RunnerError

HEX64 = re.compile(r"^[0-9a-f]{64}$")
REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
PLACEHOLDER = re.compile(r"(?:__[A-Z0-9_]+__|\$\{[^}]+\}|\b(?:TBD|TODO|CHANGEME)\b)", re.I)
SENSITIVE_KEY = re.compile(r"(?:password|token|secret|authorization|cookie|provider.body)", re.I)
SENSITIVE_VALUE = re.compile(r"(?:-----BEGIN [A-Z ]+PRIVATE KEY-----|://[^/@\s]+@)", re.I)


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RunnerError("DIGEST_INPUT_NOT_REGULAR")
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after):
            raise RunnerError("DIGEST_INPUT_CHANGED_DURING_READ")
    finally:
        os.close(fd)
    return digest.hexdigest()


def load_json_with_digest(
    path: pathlib.Path, *, maximum: int = 2 * 1024 * 1024
) -> tuple[dict[str, Any], str]:
    parent = os.lstat(path.parent)
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022):
        raise RunnerError("JSON_INPUT_PARENT_NOT_TRUSTED")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022):
            raise RunnerError("JSON_INPUT_NOT_PRIVATE_REGULAR")
        if before.st_size <= 0 or before.st_size > maximum:
            raise RunnerError("JSON_INPUT_SIZE_INVALID")
        chunks = []
        remaining = before.st_size
        while remaining:
            block = os.read(fd, min(remaining, 1024 * 1024))
            if not block:
                raise RunnerError("JSON_INPUT_READ_INCOMPLETE")
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(before) != identity(after) or len(payload) != before.st_size:
            raise RunnerError("JSON_INPUT_CHANGED_DURING_READ")
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError("JSON_INPUT_INVALID") from error
    if not isinstance(value, dict):
        raise RunnerError("JSON_INPUT_NOT_OBJECT")
    return value, sha256_bytes(payload)


def load_json(path: pathlib.Path, *, maximum: int = 2 * 1024 * 1024) -> dict[str, Any]:
    return load_json_with_digest(path, maximum=maximum)[0]


def write_exclusive(path: pathlib.Path, value: Any, *, mode: int = 0o400) -> str:
    parent = path.parent
    details = os.lstat(parent)
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RunnerError("OUTPUT_PARENT_INVALID")
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o022:
        raise RunnerError("OUTPUT_PARENT_NOT_PRIVATE")
    payload = canonical_json_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RunnerError("OUTPUT_WRITE_INCOMPLETE")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return sha256_bytes(payload)


def _walk(value: Any, parent_key: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child, str(key))
    elif isinstance(value, list):
        for child in value:
            yield parent_key, child
            yield from _walk(child, parent_key)
    elif isinstance(value, str):
        yield parent_key, value


def assert_secret_free(value: Any) -> None:
    """Reject protected values in an evidence/result object before serialization."""
    for key, child in _walk(value):
        if SENSITIVE_KEY.search(key) and not key.endswith(("_sha256", "_file", "_reference")):
            raise RunnerError("PROTECTED_FIELD_IN_OUTPUT")
        if isinstance(child, str):
            if PLACEHOLDER.search(child) or SENSITIVE_VALUE.search(child):
                raise RunnerError("PROTECTED_OR_PLACEHOLDER_VALUE_IN_OUTPUT")


def sanitized_error(reason_code: str) -> dict[str, str]:
    code = reason_code if REASON.fullmatch(reason_code) else "UNSAFE_ERROR"
    return {"status": "ERROR", "reason_code": code}
