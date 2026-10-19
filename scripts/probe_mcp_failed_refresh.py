#!/usr/bin/env python3
"""A failed catalog refresh must invalidate the old MCP binding (MCP-R5).

Reuse the independent review's real stdio peer, SQLite, approval and scripted
provider. Only temporary local data is used. Exit 1 means an old binding stayed
dispatchable after discovery observed that its remote tool disappeared.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import probe_mcp_independent_review as p


async def probe() -> dict:
    # Disappearance from discovery does not imply the server refuses tools/call.
    # The host must enforce its own catalog guard even against such a server.
    p.PEER.write_text(
        p.PEER_SOURCE.replace(
            "'tools':[{", "'tools':[] if cfg.get('missing') else [{"
        ),
        encoding="utf-8",
    )
    control, calls = p.ROOT / "control.json", p.ROOT / "calls.jsonl"
    control.write_text("{}", encoding="utf-8")
    config = p.McpServerConfig(
        server_id="review",
        transport=p.McpTransport.STDIO,
        protocol="2025-11-25",
        authorization_scope="review-scope",
        command=sys.executable,
        args=(str(p.PEER),),
        env={"REVIEW_CONTROL": str(control), "REVIEW_CALLS": str(calls)},
    )
    bindings = {
        "write": p.McpToolBinding(
            "write",
            effects=frozenset({p.ToolEffect.WRITE}),
            preview=lambda _ctx, args: {"target": args["key"]},
        )
    }
    ledger = p.SqliteExecutionBackend(p.ROOT / "operations.db")
    store = p.SqlitePersistence(p.ROOT / "runs.db")
    try:
        async with p.McpClientManager() as manager:
            connection = await manager.connect(config)
            snapshot = await p.discover_catalog(
                manager, connection, config=config, bindings=bindings
            )
            adapter = p.McpToolAdapter(
                manager=manager, connection=connection, config=config, snapshot=snapshot
            )
            definitions = adapter.definitions()
            approvals = p.InMemoryApprovalStore()
            gateway = p.ToolGateway(
                registry=p.ToolRegistry(definitions), policy=p.ConservativePolicy(),
                approvals=approvals, backend=ledger,
            )
            holder = {"ctx": p.context("a1")}
            tool = p.core_tool(
                definitions[0],
                p.GatewayBindings(gateway=gateway, context=lambda: holder["ctx"]),
            )
            provider = p.Script(tool.name)
            runtime = p.DefaultHarness(stream_fn=provider, tools=[tool], persistence=store)
            first = await runtime.start(p.RunRequest(prompt="write"), holder["ctx"])
            assert first.status.value == "suspended", "setup must await real approval"

            control.write_text(json.dumps({"missing": True}), encoding="utf-8")
            refresh_error = None
            try:
                await p.discover_catalog(manager, connection, config=config, bindings=bindings)
            except p.McpConfigurationError as exc:
                refresh_error = str(exc)
            assert refresh_error, "discovery must actually observe and refuse the missing tool"
            check = adapter.catalog_check()
            await approvals.decide(
                approvals.open_approvals()[0].key, p.ApprovalStatus.APPROVED, "review-author"
            )
            holder["ctx"] = p.context("a2")
            refusal = None
            try:
                status = (await runtime.resume("r", {}, holder["ctx"])).status.value
            except (p.ResumeRefused, p.McpDriftRefusal) as exc:
                status, refusal = "refused", type(exc).__name__
            durable = await runtime.state("r", context=holder["ctx"])
            remote_calls = len(calls.read_text().splitlines()) if calls.exists() else 0
            model_calls = provider.calls - 1
            passed = (
                not check.ok and status in {"refused", "suspended"}
                and durable.status.value == "suspended"
                and remote_calls == 0 and model_calls == 0
                and durable.pending_interaction is not None
            )
            return {
                "source_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=p.PACKAGE, text=True
                ).strip(),
                "python": sys.version.split()[0],
                "evidence_root": str(p.ROOT),
                "refresh_refused": True,
                "refresh_error": refresh_error,
                "old_adapter_catalog_check_ok": check.ok,
                "resumed_status": status,
                "refusal": refusal,
                "durable_status": durable.status.value,
                "remote_calls": remote_calls,
                "provider_calls_after_resume": model_calls,
                "passed": passed,
            }
    finally:
        ledger.close()
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(probe(), timeout=90))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    (p.ROOT / "results.json").write_text(rendered, encoding="utf-8")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
