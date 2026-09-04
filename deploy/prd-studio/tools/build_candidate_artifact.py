#!/usr/bin/env python3
"""Build an exact deterministic application tar from one committed studio tree."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
from prd_studio_deploy.records import canonical_json_bytes  # noqa: E402

EXPECTED_DEPENDENCIES = {"dotenv": "17.4.2", "express": "5.2.1", "mysql2": "3.24.3"}
SAFE_DEPENDENCY_SPEC = re.compile(r"^[0-9A-Za-z .<>=~^*|+_-]{1,120}$")
INTEGRITY = re.compile(r"^sha512-[A-Za-z0-9+/]+={0,2}$")


def _validate_dependency_contract(studio: pathlib.Path) -> str:
    package = json.loads((studio / "package.json").read_bytes())
    lock = json.loads((studio / "package-lock.json").read_bytes())
    if (not isinstance(package, dict) or package.get("name") != "bankkaro-prd-studio"
            or package.get("version") != "1.0.0" or package.get("private") is not True
            or package.get("packageManager") != "npm@10.9.7"
            or package.get("engines") != {"node": "22.22.2"}
            or package.get("dependencies") != EXPECTED_DEPENDENCIES):
        raise ValueError("PACKAGE_CONTRACT_INVALID")
    if (not isinstance(lock, dict) or lock.get("lockfileVersion") != 3
            or lock.get("requires") is not True or not isinstance(lock.get("packages"), dict)):
        raise ValueError("PACKAGE_LOCK_CONTRACT_INVALID")
    root = lock["packages"].get("")
    if (not isinstance(root, dict) or root.get("name") != "bankkaro-prd-studio"
            or root.get("version") != "1.0.0"
            or root.get("dependencies") != EXPECTED_DEPENDENCIES):
        raise ValueError("PACKAGE_LOCK_ROOT_INVALID")
    for location, entry in lock["packages"].items():
        if not isinstance(location, str) or not isinstance(entry, dict):
            raise ValueError("PACKAGE_LOCK_ENTRY_INVALID")
        if location == "":
            continue
        if (not location.startswith("node_modules/") or ".." in pathlib.PurePosixPath(location).parts
                or entry.get("link") is not None):
            raise ValueError("PACKAGE_LOCK_LOCATION_INVALID")
        resolved = entry.get("resolved")
        integrity = entry.get("integrity")
        if not isinstance(resolved, str) or not isinstance(integrity, str):
            raise ValueError("PACKAGE_LOCK_MATERIAL_MISSING")
        parsed = urllib.parse.urlsplit(resolved)
        if (parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org"
                or parsed.port not in {None, 443} or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.startswith("/")
                or INTEGRITY.fullmatch(integrity) is None):
            raise ValueError("PACKAGE_LOCK_MATERIAL_UNAPPROVED")
        for dependency_field in ("dependencies", "optionalDependencies", "peerDependencies"):
            dependencies = entry.get(dependency_field, {})
            if not isinstance(dependencies, dict):
                raise ValueError("PACKAGE_LOCK_DEPENDENCY_FIELDS_INVALID")
            for dependency_name, specification in dependencies.items():
                if (not isinstance(dependency_name, str) or not isinstance(specification, str)
                        or SAFE_DEPENDENCY_SPEC.fullmatch(specification) is None
                        or not any(character.isdigit() for character in specification)):
                    raise ValueError("PACKAGE_LOCK_DEPENDENCY_SOURCE_UNAPPROVED")
    return hashlib.sha256((studio / "package-lock.json").read_bytes()).hexdigest()


def git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
    return result.stdout.strip()


def deterministic_tar(source: pathlib.Path, output: pathlib.Path) -> str:
    if output.exists():
        raise FileExistsError(output)
    with output.open("xb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        paths = [source] + sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix())
        for path in paths:
            relative = pathlib.PurePosixPath("app") if path == source else pathlib.PurePosixPath("app") / path.relative_to(source).as_posix()
            details = path.lstat()
            info = tarfile.TarInfo(relative.as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.pax_headers = {}
            if path.is_symlink():
                target = os.readlink(path)
                pure_target = pathlib.PurePosixPath(target)
                if pure_target.is_absolute() or ".." in pure_target.parts:
                    raise ValueError("ARTIFACT_SYMLINK_INVALID")
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                bundle.addfile(info)
            elif path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                bundle.addfile(info)
            elif path.is_file():
                payload = path.read_bytes()
                info.size = len(payload)
                info.mode = 0o755 if details.st_mode & 0o111 else 0o644
                bundle.addfile(info, io.BytesIO(payload))
            else:
                raise ValueError("ARTIFACT_MEMBER_TYPE_INVALID")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(output, 0o444)
    parent_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def _tool_version(node: pathlib.Path, npm_cli: pathlib.Path,
                  expected_node: str, expected_npm: str) -> None:
    if expected_node != "v22.22.2" or expected_npm != "10.9.7":
        raise ValueError("BUILD_TOOL_EXPECTATION_INVALID")
    if (not node.is_absolute() or not npm_cli.is_absolute()
            or not node.resolve(strict=True).is_file()
            or not npm_cli.resolve(strict=True).is_file()):
        raise ValueError("BUILD_TOOL_PATH_INVALID")
    clean_env = {"PATH": str(node.parent) + ":/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    node_result = subprocess.run([str(node), "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=10, env=clean_env)
    npm_result = subprocess.run([str(node), str(npm_cli), "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10, env=clean_env)
    if (node_result.returncode != 0 or node_result.stdout.decode("ascii", "strict").strip()
            != expected_node or npm_result.returncode != 0
            or npm_result.stdout.decode("ascii", "strict").strip() != expected_npm):
        raise ValueError("BUILD_TOOL_VERSION_MISMATCH")


def _write_identity(path: pathlib.Path, value: dict[str, str]) -> None:
    payload = canonical_json_bytes(value)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o444)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def build(repo: pathlib.Path, commit: str, output: pathlib.Path, identity_output: pathlib.Path,
          node: pathlib.Path, npm_cli: pathlib.Path,
          expected_node: str, expected_npm: str) -> dict[str, str]:
    repo = repo.resolve(strict=True)
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("CERTIFIED_BUILD_PLATFORM_REQUIRED")
    resolved_commit = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved_commit != commit or len(commit) != 40:
        raise ValueError("CANDIDATE_COMMIT_NOT_EXACT")
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}")
    _tool_version(node, npm_cli, expected_node, expected_npm)
    with tempfile.TemporaryDirectory(prefix="prd-studio-candidate-") as raw:
        work = pathlib.Path(raw)
        archive = work / "source.tar"
        subprocess.run(["/usr/bin/git", "-C", str(repo), "archive", "--format=tar",
                        "--prefix=source/", "-o", str(archive), commit, "studio"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        with tarfile.open(archive, "r:") as bundle:
            members = bundle.getmembers()
            if not 1 <= len(members) <= 100000:
                raise ValueError("SOURCE_ARCHIVE_MEMBER_COUNT_INVALID")
            seen: set[pathlib.PurePosixPath] = set()
            total = 0
            for member in members:
                pure = pathlib.PurePosixPath(member.name)
                if (pure.is_absolute() or ".." in pure.parts or not pure.parts
                        or pure.parts[0] != "source" or pure in seen
                        or not (member.isdir() or member.isfile())):
                    raise ValueError("SOURCE_ARCHIVE_MEMBER_INVALID")
                seen.add(pure)
                target = work.joinpath(*pure.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.size
                if member.size > 64 * 1024 * 1024 or total > 256 * 1024 * 1024:
                    raise ValueError("SOURCE_ARCHIVE_SIZE_LIMIT")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("SOURCE_ARCHIVE_FILE_MISSING")
                with target.open("xb") as destination:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise ValueError("SOURCE_ARCHIVE_FILE_TRUNCATED")
                        destination.write(block)
                        remaining -= len(block)
        studio = work / "source/studio"
        for required in ("server.js", "schema.sql", "package.json", "package-lock.json",
                         "scripts/apply-schema.js"):
            path = studio / required
            if not path.is_file() or path.is_symlink():
                raise ValueError("REQUIRED_SOURCE_FILE_INVALID")
        if (not (studio / "public").is_dir() or (studio / "public").is_symlink()
                or any(path.is_symlink() for path in (studio / "public").rglob("*"))):
            raise ValueError("PUBLIC_SOURCE_TREE_INVALID")
        package_lock_sha256 = _validate_dependency_contract(studio)
        clean_env = {"PATH": str(node.parent) + ":/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        result = subprocess.run([str(node), str(npm_cli), "ci", "--omit=dev", "--ignore-scripts",
                                 "--no-audit", "--no-fund"], cwd=studio,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                                env=clean_env)
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            raise ValueError("NPM_CI_FAILED")
        metadata = {"schema_version": "1.0", "commit": commit, "tree": tree,
                    "node_version": expected_node, "npm_version": expected_npm,
                    "build_os": "linux", "build_arch": "x86_64",
                    "package_lock_sha256": package_lock_sha256}
        bundle = work / "bundle"
        bundle.mkdir()
        for relative in ("server.js", "schema.sql", "package.json", "package-lock.json"):
            shutil.copyfile(studio / relative, bundle / relative)
        shutil.copytree(studio / "public", bundle / "public")
        (bundle / "scripts").mkdir()
        shutil.copyfile(studio / "scripts/apply-schema.js", bundle / "scripts/apply-schema.js")
        shutil.copytree(studio / "node_modules", bundle / "node_modules", symlinks=True)
        (bundle / ".release-source.json").write_bytes(canonical_json_bytes(metadata))
        digest = deterministic_tar(bundle, output)
    identity = {**metadata, "artifact_sha256": digest}
    _write_identity(identity_output, identity)
    return identity


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=pathlib.Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--identity-output", required=True, type=pathlib.Path)
    parser.add_argument("--node", required=True, type=pathlib.Path)
    parser.add_argument("--npm-cli", required=True, type=pathlib.Path)
    parser.add_argument("--expected-node-version", required=True)
    parser.add_argument("--expected-npm-version", required=True)
    args = parser.parse_args()
    identity = build(args.repository, args.commit, args.output, args.identity_output,
                     args.node, args.npm_cli,
                     args.expected_node_version, args.expected_npm_version)
    print(json.dumps({"status": "PASS", **identity}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
