#!/usr/bin/env python3
"""Build the versioned runner as a byte-for-byte deterministic zipapp."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()


def build(output: pathlib.Path) -> str:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(prefix="prd-studio-runner-") as raw:
        stage = pathlib.Path(raw)
        package_source = ROOT / "runner/prd_studio_deploy"
        package_target = stage / "prd_studio_deploy"
        package_target.mkdir()
        for source in sorted(package_source.glob("*.py")):
            shutil.copyfile(source, package_target / source.name)
        assets = stage / "prd_studio_deploy/assets"
        assets.mkdir()
        shutil.copyfile(package_source / "assets/__init__.py", assets / "__init__.py")
        shutil.copyfile(ROOT / "templates/prd-studio.service", assets / "prd-studio.service")
        shutil.copyfile(ROOT / "templates/prd-studio.nginx.conf", assets / "prd-studio.nginx.conf")
        shutil.copyfile(ROOT / "templates/prd-studio.nginx-http.conf", assets / "prd-studio.nginx-http.conf")
        shutil.copyfile(ROOT / "fixtures/acceptance-project-v1.json", assets / "acceptance-project-v1.json")
        with (stage / "__main__.py").open("x", encoding="utf-8", newline="\n") as launcher:
            launcher.write(
                "from prd_studio_deploy.__main__ import main\nraise SystemExit(main())\n")
        temporary = output.with_name("." + output.name + ".new")
        with temporary.open("xb") as raw_output:
            raw_output.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(raw_output, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=9) as archive:
                for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                    name = path.relative_to(stage).as_posix()
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    mode = 0o755 if name == "__main__.py" else 0o644
                    info.external_attr = (0o100000 | mode) << 16
                    archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED,
                                     compresslevel=9)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
        parent_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path,
                        default=ROOT / f"dist/prd-studio-deploy-{VERSION}.pyz")
    args = parser.parse_args()
    digest = build(args.output)
    print(json.dumps({"status": "PASS", "version": VERSION,
                      "runner_sha256": digest, "output": str(args.output.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
