#!/usr/bin/env python3
"""EXT-R7: real MCP borrowed-adapter binding checks and a matching control.

Use .venv-mcp/bin/python scripts/probe_extensions_borrowed_adapter.py.
Exit 1 means a foreign/undeclared borrowed binding was admitted, or its matching
control failed. The shared manager is owned and closed by this probe's host task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/mcp/support"))

from harness import counter, mcp_stdio_config
from yanxu_harness.extensions import (
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionManifest,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.extensions.errors import ExtensionError
from yanxu_harness.extensions.mcp import McpExtensionFactory
from yanxu_harness.integrations.mcp import (
    McpClientManager,
    McpToolAdapter,
    McpToolBinding,
    discover_catalog,
    local_alias,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    InMemoryApprovalStore,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
)


async def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="extensions-borrowed-review-") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        config = mcp_stdio_config(
            root / "facts.jsonl", state,
            authorization_scope="scope-a", config_revision="cfg-a",
        )
        tool_bindings = {
            "bump": McpToolBinding(
                remote_name="bump", effects=frozenset({ToolEffect.WRITE}),
                preview=lambda ctx, args: {"key": args["key"]},
            )
        }
        alias = local_alias(config.server_id, "bump")
        extension_id = "review.mcp"
        manifest = ExtensionManifest(
            id=extension_id, version="1.0.0",
            requires_capabilities=("mcp.tools.v1", "tools.v1"),
            contributes=ContributionDeclarations(tools=(alias,), skills=()),
        )
        results = {}
        async with McpClientManager() as manager:
            connection = await manager.connect(config)
            snapshot = await discover_catalog(
                manager, connection, config=config, bindings=tool_bindings,
            )
            adapter = McpToolAdapter(
                manager=manager, connection=connection, config=config, snapshot=snapshot,
            )
            cases = (
                ("wrong_namespace", "project-b", "scope-a", "cfg-a", True),
                ("wrong_authorization", "project-a", "scope-b", "cfg-a", True),
                ("wrong_revision", "project-a", "scope-a", "cfg-b", True),
                ("undeclared_borrow", "project-a", "scope-a", "cfg-a", False),
                ("matching_control", "project-a", "scope-a", "cfg-a", True),
            )
            for name, namespace, scope, revision, explicit in cases:
                deployment = None
                backend = None
                status = "not_opened"
                # The omitted manager/namespace variant is accepted by from_adapter
                # today; it must not become a way to avoid declaring the borrow.
                binding = ExtensionBinding(
                    extension_id=extension_id, namespace=namespace,
                    authorization_scope=scope, config_revision=revision,
                    policy_revision="p1", capabilities=frozenset({"mcp.tools.v1", "tools.v1"}),
                    tools=(alias,), skills=(),
                )
                try:
                    try:
                        factory = McpExtensionFactory.from_adapter(
                            adapter, manager=manager if explicit else None,
                            borrowed_namespace="project-a" if explicit else "",
                        )
                        plan = plan_extensions(
                            manifests=[manifest], factories={extension_id: factory}, bindings=[binding],
                        )
                        deployment = open_extensions(plan)
                        await deployment.open()
                    except ExtensionError:
                        status = "refused_before_dispatch"
                    else:
                        backend = SqliteExecutionBackend(root / f"{name}.db")
                        gateway = ToolGateway(
                            registry=deployment.registry,
                            policy=ConservativePolicy(auto_write=True),
                            approvals=InMemoryApprovalStore(), backend=backend,
                        )
                        context = RunContext(
                            identity=RunIdentity(
                                namespace=namespace, session_id="s", run_id=name, attempt_id="a",
                            ),
                            principal=Principal(subject="independent-review"),
                            labels={"authorization_scope": scope},
                        )
                        result = await gateway.invoke(
                            ToolInvocation(
                                namespace=namespace, tool_name=alias, call_id=name,
                                run_id=name, arguments={"key": name},
                            ), context,
                        )
                        status = result.status.value
                finally:
                    if deployment is not None:
                        await deployment.aclose()
                    if backend is not None:
                        backend.close()
                effects = counter(state, name)
                manager_live = manager.connection_state(connection) == "live"
                expected_refusal = name != "matching_control"
                passed = (
                    status == "refused_before_dispatch" and effects == 0 if expected_refusal
                    else status == "executed" and effects == 1
                ) and manager_live
                results[name] = {
                    "passed": passed, "status": status, "remote_effect_count": effects,
                    "borrowed_manager_still_live": manager_live,
                }
        return {
            "ok": all(item["passed"] for item in results.values()),
            "python": sys.version.split()[0], "checks": results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(run())
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
