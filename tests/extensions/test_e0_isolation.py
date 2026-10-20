"""EXT-AC06/08: the extension layer is standard-library-only and inert until opened."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from yanxu_harness.extensions import describe_plan, parse_manifest, plan_extensions

from ext_support import RecordingFactory, binding, manifest_payload


FORBIDDEN = (
    "mcp",
    "mcp_types",
    "anyio",
    "httpx",
    "httpx2",
    "jsonschema",
    "pydantic",
    "yaml",
    "requests",
    "urllib3",
)

_COLD_IMPORT = """
import importlib, json, pkgutil, sys
import yanxu_harness.extensions as extensions
for module in pkgutil.walk_packages(extensions.__path__, "yanxu_harness.extensions."):
    importlib.import_module(module.name)
banned = set(json.loads(sys.argv[1]))
leaked = sorted({name.split(".")[0] for name in sys.modules if name.split(".")[0] in banned})
print(json.dumps({"leaked": leaked, "modules": len(sys.modules)}))
"""


def _package_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_a_cold_import_of_the_extensions_package_pulls_no_optional_dependency(tmp_path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_package_root() / "src")
    env.pop("PYTHONHOME", None)
    completed = subprocess.run(
        [sys.executable, "-c", _COLD_IMPORT, json.dumps(FORBIDDEN)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["leaked"] == []


def test_parsing_planning_and_describing_perform_no_process_or_network_io() -> None:
    factory = RecordingFactory(
        contribution=lambda: (_ for _ in ()).throw(AssertionError("factory opened during diagnosis"))
    )
    plan = plan_extensions(
        manifests=[parse_manifest(manifest_payload())],
        factories={"example.reference": factory},
        bindings=[binding()],
    )

    def refuse(*args, **kwargs):
        raise AssertionError("the extension layer performed I/O before it was opened")

    original_connect = socket.socket.connect
    original_create = socket.create_connection
    original_popen = subprocess.Popen
    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    subprocess.Popen = refuse  # type: ignore[assignment]
    try:
        described = describe_plan(plan)
        parse_manifest(manifest_payload(version="1.0.1"))
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create  # type: ignore[assignment]
        subprocess.Popen = original_popen  # type: ignore[assignment]

    assert described["state"] == "planned"
    assert factory.opened == 0
