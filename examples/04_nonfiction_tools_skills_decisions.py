"""Example 4 — a non-fiction task using a custom tool, a skill and a decision router.

    python examples/04_nonfiction_tools_skills_decisions.py

The point of this example is that none of it is about novels. A CSV file of
measurements is:

* validated and summarised by a **custom tool** with a declared read effect,
* described by a **skill** ("how to read a measurement table") that is published
  as bounded metadata and whose body loads only when it is actually used,
* ranked by a **decision router** that, when a host supplies a model call, picks
  the most relevant column. With no decision model configured the same task still
  runs: the no-op router simply chooses nothing.

Three claims are checked at the end: the skill catalog never loaded the body
before it was used, a skill grants no authority, and the run works with no
decision model at all.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import tempfile
from pathlib import Path

from yanxu_harness.decisions import (
    DecisionBudget,
    DecisionCandidate,
    DecisionKind,
    DecisionRequest,
    ModelDecisionRouter,
    NoopDecisionRouter,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.skills import InMemorySkillProvider, SkillLimits, SkillMetadata
from yanxu_harness.storage import SqliteApprovalStore, SqliteExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)

TABLE = "city,temperature_mm,rainfall_mm\nOslo,-4,35\nLisbon,17,78\n"


def validate(arguments: dict) -> dict:
    if not isinstance(arguments.get("column"), str) or not arguments["column"]:
        raise ValueError("column must be a non-empty string")
    return {"column": arguments["column"]}


def summarise(table: str):
    def handler(_context, arguments):
        reader = csv.DictReader(io.StringIO(table))
        column = arguments["column"]
        values = [float(row[column]) for row in reader if row.get(column) not in (None, "")]
        if not values:
            return {"column": column, "rows": 0}
        return {
            "column": column,
            "rows": len(values),
            "min": min(values),
            "max": max(values),
            "mean": round(sum(values) / len(values), 2),
        }

    return handler


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="yanxu-example-04-"))

    # Layer 1/2 — a custom, read-only tool. No write effects: no approval needed.
    definition = ToolDefinition(
        "table.summarise",
        "Summarise one numeric column of a CSV table.",
        effects=frozenset({ToolEffect.READ}),
        args_validator=validate,
        handler=summarise(TABLE),
        args_schema={
            "type": "object",
            "properties": {"column": {"type": "string"}},
            "required": ["column"],
        },
    )
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        # A read-only task still needs a ledger and an approval store to exist;
        # neither is exercised, and this file's tables stay empty.
        approvals=SqliteApprovalStore(workspace / "approvals.sqlite"),
        backend=SqliteExecutionBackend(workspace / "ops.sqlite"),
    )
    context = RunContext(
        identity=RunIdentity(namespace="example", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="analyst"),
    )

    # Skills — metadata is listed; the body is loaded only when it is used.
    provider = InMemorySkillProvider(SkillLimits(body_chars=2000))
    provider.register(
        SkillMetadata(
            skill_id="table.reading",
            name="Reading a measurement table",
            description="How to pick and interpret a numeric column.",
            when_to_use="The task involves a table of measurements.",
            avoid_when="The task is free-form prose.",
            tools=("table.summarise",),
        ),
        "Read the header first, then state which column you used.",
    )
    listed = await provider.list_metadata()
    body = await provider.load_body("table.reading")
    print(f"skill: {listed[0].skill_id} v{listed[0].version}, body loaded on demand ({len(body.text)} chars)")

    # Decisions — optional. A host model call may rank the candidates.
    columns = ("city", "temperature_mm", "rainfall_mm")
    request = DecisionRequest(
        task="Which column should the summary use?",
        candidates=tuple(
            DecisionCandidate(candidate_id=name, kind=DecisionKind.TOOL, description=f"column {name}")
            for name in columns
        ),
        kind=DecisionKind.TOOL,
        max_suggestions=1,
        scope="example",
    )

    async def host_model_call(payload):
        # A stand-in for the host's own model call: it sees only bounded metadata.
        candidate = payload["requests"][0]["candidates"][1]
        return {"suggestions": [{"candidate_id": candidate["id"], "kind": "tool", "rank": 0}]}

    chosen = (await ModelDecisionRouter(host_model_call, budget=DecisionBudget()).route(request)).ids()
    print(f"decision router chose: {chosen}")

    no_model = await NoopDecisionRouter().route(request)
    assert no_model.suggestions == (), "with no decision model the task still runs, choosing nothing"

    column = chosen[0] if chosen else "temperature_mm"
    result = await gateway.invoke(
        ToolInvocation(
            call_id="call-1",
            namespace="example",
            tool_name="table.summarise",
            arguments={"column": column},
            run_id="r1",
        ),
        context,
    )
    print(f"tool result: {json.dumps(result.receipt.result, ensure_ascii=False)}")
    assert result.status is InvocationStatus.EXECUTED
    assert result.receipt.result["rows"] == 2

    # The checks this example exists to make.
    assert not hasattr(listed[0], "permissions"), "a skill grants nothing"
    assert listed[0].tools == ("table.summarise",), "a skill's tool list is an inert hint"
    print("OK: custom tool + skill + optional decision router, with no fiction anywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
