"""Read runner-bound templates and the synthetic fixture."""

from __future__ import annotations

import importlib.resources
import json
import pathlib
from typing import Any

from .errors import RunnerError


def _read(name: str, fallback: pathlib.Path) -> bytes:
    resource = importlib.resources.files("prd_studio_deploy.assets").joinpath(name)
    if resource.is_file():
        return resource.read_bytes()
    if fallback.is_file():
        return fallback.read_bytes()
    raise RunnerError("RUNNER_ASSET_MISSING")


def load_assets() -> tuple[dict[str, str], dict[str, Any]]:
    root = pathlib.Path(__file__).resolve().parents[2]
    unit = _read("prd-studio.service", root / "templates/prd-studio.service")
    nginx = _read("prd-studio.nginx.conf", root / "templates/prd-studio.nginx.conf")
    nginx_http = _read("prd-studio.nginx-http.conf", root / "templates/prd-studio.nginx-http.conf")
    fixture_raw = _read("acceptance-project-v1.json", root / "fixtures/acceptance-project-v1.json")
    try:
        fixture = json.loads(fixture_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError("RUNNER_FIXTURE_INVALID") from error
    return {
        "systemd_unit": unit.decode("utf-8"),
        "nginx_include": nginx.decode("utf-8"),
        "nginx_http_include": nginx_http.decode("utf-8"),
    }, fixture
