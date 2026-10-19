"""MCP06 的诊断面：只读、白名单、无秘密、不连接。

诊断的价值在于「事后可读」；所以这里证明三件事：

* 白名单外的字段（错误正文、参数、结果、原始帧）不会被投影出去；
* 配置侧连 command/url/argv 都不给，env/header 只给**名字和计数**；
* 导入这个模块不会把 SDK（以及它的 Web 依赖）拉进基础进程。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.integrations.mcp import McpLimits, McpServerConfig, McpTransport  # noqa: E402
from yanxu_harness.integrations.mcp.diagnostics import safe_call_facts, safe_config_facts  # noqa: E402

SECRET = "diag-secret-sentinel"


def test_only_allowlisted_call_facts_survive_and_they_are_bounded() -> None:
    details = {
        "alias": "mcp.fixture.echo.0123456789ab",
        "remote_tool": "echo\n" + "x" * 500,
        "tool_version": "sha256:" + "a" * 64,
        "connection_id": "b" * 32,
        "connection_generation": 2,
        "transport": "stdio",
        "protocol": "2025-11-25",
        "dispatched": True,
        "duration_seconds": 0.0125,
        "result_source": "remote",
        "policy_revision": "1",
        "not_sent": False,
        # None of the following may be projected:
        "error_text": f"a remote failure mentioning {SECRET}",
        "arguments": {"token": SECRET},
        "struct": {"content": [{"text": SECRET}]},
    }
    projected = safe_call_facts(details)
    assert set(projected) <= {
        "alias",
        "remote_tool",
        "tool_version",
        "connection_id",
        "connection_generation",
        "transport",
        "protocol",
        "dispatched",
        "duration_seconds",
        "result_source",
        "policy_revision",
        "not_sent",
    }
    assert SECRET not in json.dumps(projected)
    assert "\n" not in projected["remote_tool"]
    assert len(projected["remote_tool"]) <= 201
    assert projected["dispatched"] is True


def test_config_facts_never_carry_launch_details_or_credentials() -> None:
    real = McpServerConfig(
        server_id="fixture",
        transport=McpTransport.STDIO,
        protocol="2025-11-25",
        authorization_scope="scope-a",
        command="/usr/local/bin/mcp-secret-server",
        args=("--token", SECRET),
        env={"API_TOKEN": SECRET},
        limits=McpLimits(),
    )
    projected = safe_config_facts(real)
    rendered = json.dumps(projected)
    assert SECRET not in rendered
    assert "/usr/local/bin/mcp-secret-server" not in rendered
    assert "--token" not in rendered
    assert projected["env_key_count"] == 1
    assert projected["environment_keys"] == ["API_TOKEN"]


def test_importing_diagnostics_does_not_load_the_sdk() -> None:
    script = (
        "import json, sys;"
        "sys.path.insert(0, %r);"
        "import yanxu_harness.integrations.mcp.diagnostics as d;"
        "print(json.dumps(sorted({n.split('.')[0] for n in sys.modules "
        "if n.split('.')[0] in {'mcp', 'mcp_types', 'anyio', 'httpx2', 'jsonschema'}})))"
    ) % (str(harness.SUPPORT_DIR.parents[1] / "src"),)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-400:]
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_the_diagnostics_module_offers_no_way_to_connect() -> None:
    from yanxu_harness.integrations.mcp import diagnostics

    public = {name for name in dir(diagnostics) if not name.startswith("_")}
    assert "McpClientManager" not in public
    assert not any(
        name.startswith(("connect", "invoke", "session")) or "connect" in name for name in public
    ), public
    assert {"safe_call_facts", "safe_config_facts"} <= public
    assert safe_call_facts(None) == {}
