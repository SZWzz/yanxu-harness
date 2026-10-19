"""Independent review regressions (MCP-R1--R5), kept as default acceptance tests.

These are the failures recorded in
``docs/mcp-client-independent-review.zh-CN.md``: a ``$dynamicRef`` that fetched a
remote document, an observed catalog drift that still resumed, credentials echoed
by a remote error that reached the ledger/model, a bound-but-missing tool that
produced a ``complete`` empty catalog, and a refused refresh that left the old
catalog dispatchable. Every assertion is made against a real loopback HTTP peer, a
real stdio server, the real harness/gateway/approval/SQLite stack, or an
independent HTTP request counter.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.core import AssistantMessage, StopReason, TextContent, ToolCall  # noqa: E402
from yanxu_harness.errors import ResumeRefused  # noqa: E402
from yanxu_harness.integrations.mcp import (  # noqa: E402
    McpClientManager,
    McpConfigurationError,
    McpDriftRefusal,
    McpSchemaError,
    McpToolAdapter,
    McpToolBinding,
    RemoteToolSchema,
    build_snapshot,
    discover_catalog,
)
from yanxu_harness.integrations.mcp.types import McpToolDescriptor  # noqa: E402
from yanxu_harness.runtime import (  # noqa: E402
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
    RunStatus,
)
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence  # noqa: E402
from yanxu_harness.tools import (  # noqa: E402
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)
from support.harness import facts_of, mcp_http_config, mcp_stdio_config, raw_http_server  # noqa: E402
from package_paths import source_root  # noqa: E402

READ = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))


def _write_binding(name: str = "bump") -> McpToolBinding:
    return McpToolBinding(
        name,
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
    )


def _context(*, run_id: str = "r1", attempt: str = "a1") -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id=run_id, attempt_id=attempt
        ),
        principal=Principal(subject="author", permissions=frozenset({"mcp.write"})),
    )


# -- MCP-R1: every retrieval keyword is refused, with no request made ----------


class _CountingPeer(BaseHTTPRequestHandler):
    hits: list[str]

    def log_message(self, *_args) -> None:  # pragma: no cover - quiet fixture
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        _CountingPeer.hits.append(self.path)
        body = json.dumps({"type": "object"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_dynamic_and_recursive_refs_are_refused_without_a_fetch() -> None:
    _CountingPeer.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for keyword in ("$dynamicRef", "$recursiveRef"):
            schema = {"type": "object", keyword: f"{base}/schema-{keyword.strip('$')}"}
            with pytest.raises(McpSchemaError) as caught:
                RemoteToolSchema.register("remote_dynamic", schema, max_bytes=64 * 1024)
            assert keyword in str(caught.value)
        # A legitimate same-document dynamic anchor still compiles and validates.
        local = RemoteToolSchema.register(
            "local_dynamic",
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"child": {"$dynamicRef": "#node"}},
                "additionalProperties": False,
            },
            max_bytes=64 * 1024,
        )
        local.validate({"child": {"child": {}}})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert _CountingPeer.hits == [], "registration and validation must fetch nothing"


def test_an_output_schema_with_a_remote_reference_is_refused(tmp_path) -> None:
    config = mcp_stdio_config(tmp_path / "facts.jsonl", tmp_path / "state")
    descriptor = McpToolDescriptor(
        name="echo",
        description="A local observable probe.",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "$dynamicRef": "http://127.0.0.1:1/unwanted-output-schema-fetch",
        },
    )
    with pytest.raises(McpSchemaError) as caught:
        build_snapshot([descriptor], config=config, bindings={"echo": READ})
    assert "output schema" in str(caught.value)


# -- MCP-R4: a bound tool missing from the directory refuses the assembly ------


async def test_an_intent_submission_failure_sends_no_tool_request(tmp_path) -> None:
    """AC19, on the MCP path: a ledger that refuses the intent never reaches the server."""
    from support.deployment import build_deployment

    deploy = await build_deployment(
        tmp_path, {"echo": McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))}
    )
    try:
        async def refuse_admission(intent):
            raise RuntimeError("simulated ledger submission failure")

        # The authority is the injected submission path, not a monkeypatched network:
        # the gateway cannot admit the call, so the handler must never run.
        deploy["backend"].admit = refuse_admission
        with pytest.raises(RuntimeError, match="simulated ledger submission failure"):
            await core_tool(
                deploy["definitions"][0],
                GatewayBindings(gateway=deploy["gateway"], context=_context()),
            ).execute("call-1", {"text": "must not be sent"})
    finally:
        await deploy["manager"].__aexit__(None, None, None)
    assert facts_of(deploy["facts"], "server.call_tool.start") == []


# -- AC21: an intent left by a dead client is open, not absent ------------------


def test_a_process_that_dies_after_intent_leaves_an_open_row(tmp_path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(source_root())
    child = subprocess.run(
        [
            sys.executable,
            str(harness.SUPPORT_DIR / "open_intent_child.py"),
            str(directory),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )
    assert child.returncode == 9, (child.returncode, child.stderr[-2000:])
    assert facts_of(directory / "facts.jsonl", "server.call_tool.start") == [], (
        "the intent commits before any request; the handler must never run"
    )

    reader = (
        "import json, sys;"
        "from yanxu_harness.storage import SqliteExecutionBackend;"
        "backend = SqliteExecutionBackend(sys.argv[1], read_only=True);"
        "rows = [{'operation_id': row.operation_id, 'state': row.state, "
        "'tool_name': row.tool_name} for row in backend.unsettled()];"
        "backend.close();"
        "print(json.dumps(rows))"
    )
    second = subprocess.run(
        [sys.executable, "-c", reader, str(directory / "operations.db")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )
    assert second.returncode == 0, second.stderr[-2000:]
    rows = json.loads(second.stdout.strip().splitlines()[-1])
    assert len(rows) == 1
    assert rows[0]["state"] == "running"
    assert rows[0]["operation_id"] == "project-a:s1:r1:call-1"
    assert rows[0]["tool_name"].startswith("mcp.fixture.echo.")


# -- MCP-R2: the frozen definition's own guard, beyond the harness ------------


async def test_a_new_configuration_revision_invalidates_the_old_adapter(tmp_path) -> None:
    """The host's own config change is also a drift fact, not only a schema change."""
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        first = mcp_stdio_config(facts, state)
        connection = await manager.connect(first)
        snapshot = await discover_catalog(
            manager, connection, config=first, bindings={"echo": READ}
        )
        adapter = McpToolAdapter(
            manager=manager, connection=connection, config=first, snapshot=snapshot
        )
        # The deployment moves to a new configuration revision (a real second connection).
        second = mcp_stdio_config(facts, state, config_revision="2")
        renewed = await manager.connect(second)
        assert renewed.scope_key != connection.scope_key
        with pytest.raises(McpDriftRefusal) as caught:
            await adapter.invoke(snapshot.entry("echo"), _context(), {"text": "stale"})
        assert "configuration revision" in str(caught.value)

        # The new revision's own discovery works and is dispatchable.
        fresh = await discover_catalog(manager, renewed, config=second, bindings={"echo": READ})
        renewed_adapter = McpToolAdapter(
            manager=manager, connection=renewed, config=second, snapshot=fresh
        )
        result = await renewed_adapter.invoke(fresh.entry("echo"), _context(), {"text": "ok"})
        assert result.state.value == "succeeded"

    assert [row.get("arguments") for row in facts_of(facts, "server.call_tool.start")] == [
        {"text": "ok"}
    ]


async def test_two_authorization_scopes_at_one_revision_stay_dispatchable(tmp_path) -> None:
    """Scope isolation is not drift: connecting a second scope must not invalidate another."""
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        first_config = mcp_stdio_config(facts, state, authorization_scope="scope-a")
        first = await manager.connect(first_config)
        first_snapshot = await discover_catalog(
            manager, first, config=first_config, bindings={"echo": READ}
        )
        adapter = McpToolAdapter(
            manager=manager, connection=first, config=first_config, snapshot=first_snapshot
        )
        second_config = mcp_stdio_config(facts, state, authorization_scope="scope-b")
        second = await manager.connect(second_config)
        second_snapshot = await discover_catalog(
            manager, second, config=second_config, bindings={"echo": READ}
        )
        assert second_snapshot.fingerprint != first_snapshot.fingerprint
        result = await adapter.invoke(first_snapshot.entry("echo"), _context(), {"text": "live"})
        assert result.state.value == "succeeded"

    assert len(facts_of(facts, "server.call_tool.start")) == 1


async def test_a_revoked_binding_refuses_an_old_adapter(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(
            manager, connection, config=config, bindings={"echo": READ}
        )
        adapter = McpToolAdapter(
            manager=manager, connection=connection, config=config, snapshot=snapshot
        )
        # The same server, re-discovered with no bindings at all: the host revoked it.
        await discover_catalog(manager, connection, config=config, bindings={})
        with pytest.raises(McpDriftRefusal) as caught:
            await adapter.invoke(snapshot.entry("echo"), _context(), {"text": "revoked"})
        assert "revoked" in str(caught.value)

    assert facts_of(facts, "server.call_tool.start") == []


async def test_a_bound_tool_missing_from_a_complete_directory_is_refused(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        with pytest.raises(McpConfigurationError) as caught:
            await discover_catalog(
                manager,
                connection,
                config=config,
                bindings={"missing_tool": McpToolBinding("missing_tool", effects=frozenset({ToolEffect.READ}))},
            )
        assert "did not offer" in str(caught.value)
        assert "missing_tool" in str(caught.value)
        # A refused assembly is not published as a trusted catalog.
        assert manager.catalog_state(connection) is None

    assert facts_of(facts, "server.call_tool.start") == []


# -- MCP-R5: a refused refresh revokes the old catalog for its scope -----------


async def test_a_refused_refresh_invalidates_only_that_scope(tmp_path) -> None:
    """The peer hides a tool from ``tools/list`` but would still execute it.

    The host must not delegate this guard to the server: after discovery itself
    refuses the observed directory, the old adapter is no longer dispatchable, while
    another authorization scope keeps its own trusted catalog. Only a successful new
    publish restores dispatchability.
    """
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    hidden = tmp_path / "hidden-tools.txt"
    env = {"MCP_FIXTURE_HIDDEN_TOOLS_FILE": str(hidden)}
    async with McpClientManager() as manager:
        first_config = mcp_stdio_config(
            facts, state, authorization_scope="scope-a", extra_env=env
        )
        second_config = mcp_stdio_config(
            facts, state, authorization_scope="scope-b", extra_env=env
        )
        first = await manager.connect(first_config)
        second = await manager.connect(second_config)
        first_snapshot = await discover_catalog(
            manager, first, config=first_config, bindings={"echo": READ}
        )
        second_snapshot = await discover_catalog(
            manager, second, config=second_config, bindings={"echo": READ}
        )
        first_adapter = McpToolAdapter(
            manager=manager, connection=first, config=first_config, snapshot=first_snapshot
        )
        second_adapter = McpToolAdapter(
            manager=manager, connection=second, config=second_config, snapshot=second_snapshot
        )

        hidden.write_text("echo\n", encoding="utf-8")
        with pytest.raises(McpConfigurationError) as caught:
            await discover_catalog(
                manager, first, config=first_config, bindings={"echo": READ}
            )
        assert "did not offer" in str(caught.value)

        stale = first_adapter.catalog_check(first_snapshot.entry("echo"))
        assert not stale.ok and stale.code == "invalidated"
        with pytest.raises(McpDriftRefusal) as refused:
            await first_adapter.invoke(
                first_snapshot.entry("echo"), _context(), {"text": "stale"}
            )
        assert "invalidated" in str(refused.value)

        # A different authorization scope is a different connection scope: the failed
        # refresh above must not revoke its catalog or its dispatch.
        assert second_adapter.catalog_check(second_snapshot.entry("echo")).ok
        live = await second_adapter.invoke(
            second_snapshot.entry("echo"), _context(), {"text": "live"}
        )
        assert live.state.value == "succeeded"

        # A successful new publish is what restores dispatchability for the refused scope.
        hidden.unlink()
        refreshed = await discover_catalog(
            manager, first, config=first_config, bindings={"echo": READ}
        )
        assert first_adapter.catalog_check(first_snapshot.entry("echo")).ok
        restored = await first_adapter.invoke(
            refreshed.entry("echo"), _context(), {"text": "restored"}
        )
        assert restored.state.value == "succeeded"

    assert [row.get("arguments") for row in facts_of(facts, "server.call_tool.start")] == [
        {"text": "live"},
        {"text": "restored"},
    ], "the stale tool must never be dispatched, even though the peer would execute it"


async def test_a_refused_refresh_keeps_the_paused_run_and_its_interaction(tmp_path) -> None:
    """The real pause/resume path: a refused refresh is a resume refusal, not a tool failure."""
    hidden = tmp_path / "hidden-tools.txt"
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    approvals = InMemoryApprovalStore()
    manager = McpClientManager()
    await manager.__aenter__()
    store = None
    try:
        config = mcp_stdio_config(
            facts, state, extra_env={"MCP_FIXTURE_HIDDEN_TOOLS_FILE": str(hidden)}
        )
        connection = await manager.connect(config)
        snapshot = await discover_catalog(
            manager, connection, config=config, bindings={"bump": _write_binding()}
        )
        adapter = McpToolAdapter(
            manager=manager, connection=connection, config=config, snapshot=snapshot
        )
        definitions = adapter.definitions()
        ledger = SqliteExecutionBackend(tmp_path / "ledger.sqlite")
        gateway = ToolGateway(
            registry=ToolRegistry(list(definitions)),
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=ledger,
        )
        holder = {"ctx": _context(attempt="a1")}
        spec = core_tool(
            definitions[0], GatewayBindings(gateway=gateway, context=lambda: holder["ctx"])
        )
        provider = _Script(spec.name)
        store = SqlitePersistence(tmp_path / "runs.sqlite")
        runtime = DefaultHarness(stream_fn=provider, tools=[spec], persistence=store)

        first = await runtime.start(RunRequest(prompt="write the document"), holder["ctx"])
        assert first.status is RunStatus.SUSPENDED
        wait_before = await runtime.state("r1", context=holder["ctx"])

        hidden.write_text("bump\n", encoding="utf-8")
        with pytest.raises(McpConfigurationError):
            await discover_catalog(
                manager, connection, config=config, bindings={"bump": _write_binding()}
            )
        check = adapter.catalog_check(snapshot.entry("bump"))
        assert not check.ok and check.code == "invalidated"

        approval = approvals.open_approvals()[0]
        await approvals.decide(approval.key, ApprovalStatus.APPROVED, "author")
        holder["ctx"] = _context(attempt="a2")
        with pytest.raises(ResumeRefused) as caught:
            await runtime.resume("r1", {}, holder["ctx"])
        assert caught.value.reason_code == "binding_drift"

        # Nothing was dispatched and no model turn followed; the original wait is intact.
        assert provider.calls == 1
        assert facts_of(facts, "server.call_tool.start") == []
        wait_after = await runtime.state("r1", context=holder["ctx"])
        assert wait_after.status is RunStatus.SUSPENDED
        assert wait_after.pending_interaction == wait_before.pending_interaction
        assert wait_after.pending_interaction is not None

        # Once discovery succeeds again, the same approval and the same wait can finish.
        hidden.unlink()
        refreshed = await discover_catalog(
            manager, connection, config=config, bindings={"bump": _write_binding()}
        )
        assert refreshed.entry("bump").version == snapshot.entry("bump").version
        holder["ctx"] = _context(attempt="a3")
        final = await runtime.resume("r1", {}, holder["ctx"])
        assert final.status is RunStatus.COMPLETED
        assert provider.calls == 2
        assert len(facts_of(facts, "server.call_tool.start")) == 1
        ledger.close()
    finally:
        await manager.__aexit__(None, None, None)
        if store is not None:
            store.close()


# -- MCP-R3: a remote echo of the host credential never reaches ledger/model ---


async def test_remote_credential_echo_is_redacted_everywhere(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    canary = "SYNTHETIC_MCP_CREDENTIAL_CANARY_197"
    with raw_http_server(facts, mode="credential_echo") as (url, _port):
        async with McpClientManager() as manager:
            config = mcp_http_config(url, headers={"Authorization": f"Bearer {canary}"})
            connection = await manager.connect(config)
            bindings = {
                name: McpToolBinding(name, effects=frozenset({ToolEffect.READ}))
                for name in ("leak_success", "leak_error")
            }
            snapshot = await discover_catalog(
                manager, connection, config=config, bindings=bindings
            )
            adapter = McpToolAdapter(
                manager=manager, connection=connection, config=config, snapshot=snapshot
            )
            ledger = SqliteExecutionBackend(tmp_path / "ledger.sqlite")
            gateway = ToolGateway(
                registry=ToolRegistry(list(adapter.definitions())),
                policy=ConservativePolicy(),
                approvals=InMemoryApprovalStore(),
                backend=ledger,
            )
            projected: dict[str, dict] = {}
            for definition in adapter.definitions():
                projected[definition.name] = await core_tool(
                    definition, GatewayBindings(gateway=gateway, context=_context())
                ).execute(f"call-{definition.name}", {})
            rows = ledger.all_operations()
            ledger.close()

    assert set(projected) == {snapshot.entry("leak_success").alias, snapshot.entry("leak_error").alias}
    success = projected[snapshot.entry("leak_success").alias]
    failure = projected[snapshot.entry("leak_error").alias]
    assert success["is_error"] is False
    assert failure["is_error"] is True
    assert "accepted" in success["content"][0].text, "the non-secret part must survive"
    assert "upstream diagnostic" in failure["content"][0].text

    # The canary is absent from the model projection and from the durable receipt,
    # including nested structured content and the error text.
    assert canary not in repr(projected)
    assert canary not in repr(rows)
    assert any("[REDACTED]" in repr(row) for row in rows), "the redaction is visible where the echo was"


# -- MCP-R2: a real pause + real re-discovery refuses the resume ---------------


class _Script:
    """A scripted provider: one tool call, then a final answer."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0
        self.final = None

    async def __call__(self, model, context, options=None):
        self.calls += 1
        self.final = (
            AssistantMessage(
                content=[ToolCall(id="w1", name=self.tool_name, arguments={"key": "document"})],
                stop_reason=StopReason.TOOL_USE,
            )
            if self.calls == 1
            else AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)
        )
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self.final


async def test_observed_catalog_drift_refuses_resume_with_zero_dispatch(tmp_path) -> None:
    drift = tmp_path / "description.txt"
    drift.write_text("version-one", encoding="utf-8")
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    approvals = InMemoryApprovalStore()
    manager = McpClientManager()
    await manager.__aenter__()
    store = None
    try:
        config = mcp_stdio_config(
            facts, state, extra_env={"MCP_FIXTURE_DRIFT_DESCRIPTION": str(drift)}
        )
        connection = await manager.connect(config)
        snapshot = await discover_catalog(
            manager, connection, config=config, bindings={"bump": _write_binding()}
        )
        adapter = McpToolAdapter(
            manager=manager, connection=connection, config=config, snapshot=snapshot
        )
        definitions = adapter.definitions()
        ledger = SqliteExecutionBackend(tmp_path / "ledger.sqlite")
        gateway = ToolGateway(
            registry=ToolRegistry(list(definitions)),
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=ledger,
        )
        holder = {"ctx": _context(attempt="a1")}
        spec = core_tool(
            definitions[0], GatewayBindings(gateway=gateway, context=lambda: holder["ctx"])
        )
        provider = _Script(spec.name)
        store = SqlitePersistence(tmp_path / "runs.sqlite")
        runtime = DefaultHarness(stream_fn=provider, tools=[spec], persistence=store)

        first = await runtime.start(RunRequest(prompt="write the document"), holder["ctx"])
        assert first.status is RunStatus.SUSPENDED

        drift.write_text("version-two-changed-contract", encoding="utf-8")
        current = await discover_catalog(
            manager, connection, config=config, bindings={"bump": _write_binding()}
        )
        assert current.entry("bump").version != snapshot.entry("bump").version

        # The adapter's own promise is independent of the harness guard: a direct
        # dispatch through the old adapter refuses before any byte leaves the host.
        with pytest.raises(McpDriftRefusal):
            await adapter.invoke(snapshot.entry("bump"), _context(), {"key": "document"})
        assert facts_of(facts, "server.call_tool.start") == []

        await approvals.decide(
            approvals.open_approvals()[0].key, ApprovalStatus.APPROVED, "author"
        )
        holder["ctx"] = _context(attempt="a2")
        with pytest.raises(ResumeRefused) as caught:
            await runtime.resume("r1", {}, holder["ctx"])
        assert caught.value.reason_code == "binding_drift"

        # The original wait is intact: same suspended run, no remote call, no model turn.
        assert provider.calls == 1
        assert facts_of(facts, "server.call_tool.start") == []
        durable = await runtime.state("r1", context=holder["ctx"])
        assert durable.status is RunStatus.SUSPENDED
        assert durable.pending_interaction is not None
        ledger.close()
    finally:
        await manager.__aexit__(None, None, None)
        if store is not None:
            store.close()
