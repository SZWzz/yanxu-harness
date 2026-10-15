"""Example 2 — an approvable data modification.

    python examples/02_approved_data_edit.py

A custom tool edits a tiny JSON "record" and the harness refuses to run it until
the author approves the exact change. Four things are worth reading closely:

1. the tool declares ``effects={WRITE}`` **and** a ``preview`` — a write tool
   without a preview cannot even be constructed, because an approval has to bind
   to a concrete description of the change;
2. the first call returns ``AWAITING_APPROVAL`` and nothing was written and no
   ledger entry exists;
3. approving is bound to the exact arguments, the target version and the policy
   version — changing the arguments makes the approval stop applying;
4. the second call executes once, and repeating it replays the receipt rather
   than writing twice.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend, SqliteApprovalStore
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)

RECORD = {"title": "draft", "words": 1200}


def validate(arguments: dict) -> dict:
    if not isinstance(arguments.get("title"), str) or not arguments["title"]:
        raise ValueError("title must be a non-empty string")
    return {"title": arguments["title"], "expected_version": arguments.get("expected_version")}


def make_handler(path: Path):
    def handler(_context, arguments):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["title"] = arguments["title"]
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return {"title": record["title"], "version": arguments["expected_version"] + 1}

    return handler


def make_preview(_context, arguments):
    return {"field": "title", "from": RECORD["title"], "to": arguments["title"]}


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="yanxu-example-02-"))
    record_path = workspace / "record.json"
    record_path.write_text(json.dumps(RECORD, ensure_ascii=False), encoding="utf-8")

    definition = ToolDefinition(
        "record.rename",
        "Rename the record's title.",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validate,
        handler=make_handler(record_path),
        preview=make_preview,
    )
    approvals = SqliteApprovalStore(workspace / "approvals.sqlite")
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),  # writes need an approval
        approvals=approvals,
        backend=SqliteExecutionBackend(workspace / "operations.sqlite"),
    )
    context = RunContext(
        identity=RunIdentity(namespace="example", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
    )
    call = ToolInvocation(
        call_id="call-1",
        namespace="example",
        tool_name="record.rename",
        arguments={"title": "final", "expected_version": 1},
        target_version="1",
        run_id="r1",
    )

    waiting = await gateway.invoke(call, context)
    print(f"first call: {waiting.status.value} — {waiting.reason}")
    print(f"preview:    {json.dumps(waiting.preview, ensure_ascii=False)}")
    assert waiting.status is InvocationStatus.AWAITING_APPROVAL
    assert json.loads(record_path.read_text(encoding="utf-8"))["title"] == "draft"

    approved = await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
    print(f"author decided: {approved.status.value}")

    executed = await gateway.invoke(call, context)
    print(f"approved call: {executed.status.value} → {executed.receipt.result}")
    assert executed.status is InvocationStatus.EXECUTED
    assert json.loads(record_path.read_text(encoding="utf-8"))["title"] == "final"

    replayed = await gateway.invoke(call, context)
    print(f"repeated call: {replayed.status.value} (the handler did not run again)")
    assert replayed.status is InvocationStatus.REPLAYED

    changed = await gateway.invoke(
        ToolInvocation(
            call_id="call-1",
            namespace="example",
            tool_name="record.rename",
            arguments={"title": "something else", "expected_version": 1},
            target_version="1",
            run_id="r1",
        ),
        context,
    )
    print(f"changed args:  {changed.status.value} — {changed.reason}")
    assert changed.status is InvocationStatus.AWAITING_APPROVAL
    assert "arguments changed" in changed.reason

    print("OK: version-bound preview, one approval, one write, replay instead of a second write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
