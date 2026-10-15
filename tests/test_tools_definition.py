"""A tool definition is a contract; these are the clauses it cannot skip."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.tools.outcomes import ToolArgsInvalid
from yanxu_harness.tools import ExecutionMode, Idempotency, ToolDefinition, ToolEffect


class _SchemaModel:
    """Stands in for a Pydantic model without importing the extra."""

    @staticmethod
    def model_json_schema() -> dict:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


class _ValidatorHolder:
    """A model-like owner whose bound method is the validator."""

    @staticmethod
    def model_json_schema() -> dict:
        return _SchemaModel.model_json_schema()

    def validate(self, arguments: dict) -> dict:
        return validator(arguments)


def validator(arguments: dict) -> dict:
    if not isinstance(arguments.get("path"), str) or not arguments["path"]:
        raise ValueError("path must be a non-empty string")
    return {"path": arguments["path"]}


def handler(_context, arguments):
    return {"read": arguments["path"]}


def _write_preview(_context, arguments):
    return {"path": arguments["path"], "action": "replace"}


def test_an_executable_tool_must_bind_a_validator() -> None:
    with pytest.raises(ContractError, match="argument validator"):
        ToolDefinition("notes.read", "Read a note", handler=handler)


def test_a_validator_without_a_handler_is_refused() -> None:
    with pytest.raises(ContractError, match="dead weight"):
        ToolDefinition("notes.read", "Read a note", args_validator=validator)


def test_a_write_tool_must_declare_a_preview() -> None:
    with pytest.raises(ContractError, match="preview"):
        ToolDefinition(
            "notes.write",
            "Write a note",
            effects=frozenset({ToolEffect.WRITE}),
            args_validator=validator,
            handler=handler,
        )
    definition = ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validator,
        handler=handler,
        preview=_write_preview,
    )
    assert definition.writes


@pytest.mark.parametrize("name", ["Notes.Read", "1bad name", "", "-leading", "x" * 121])
def test_a_tool_name_must_be_a_stable_id(name: str) -> None:
    with pytest.raises(ContractError):
        ToolDefinition(name, "…", args_validator=validator, handler=handler)


def test_effects_and_version_cannot_be_empty() -> None:
    with pytest.raises(ContractError, match="effects"):
        ToolDefinition("a.b", "…", effects=frozenset(), args_validator=validator, handler=handler)
    with pytest.raises(ContractError, match="version"):
        ToolDefinition("a.b", "…", version="", args_validator=validator, handler=handler)


def test_idempotency_key_lifecycle_must_be_consistent() -> None:
    with pytest.raises(ContractError, match="idempotency_key"):
        ToolDefinition(
            "a.b", "…", args_validator=validator, handler=handler, idempotency=Idempotency.KEYED
        )
    with pytest.raises(ContractError, match="would not be used"):
        ToolDefinition(
            "a.b",
            "…",
            args_validator=validator,
            handler=handler,
            idempotency_key=lambda _c, _a: "k",
        )
    definition = ToolDefinition(
        "a.b",
        "…",
        args_validator=validator,
        handler=handler,
        idempotency=Idempotency.KEYED,
        idempotency_key=lambda _c, a: a["path"],
    )
    assert definition.idempotency is Idempotency.KEYED


def test_validation_failures_are_normalised() -> None:
    definition = ToolDefinition("a.b", "…", args_validator=validator, handler=handler)
    assert definition.validate({"path": "x"}) == {"path": "x"}
    with pytest.raises(ToolArgsInvalid, match="path must be a non-empty string"):
        definition.validate({})
    with pytest.raises(ToolArgsInvalid):
        definition.validate("not-a-mapping")  # type: ignore[arg-type]


def test_a_tool_cannot_require_itself() -> None:
    with pytest.raises(ContractError, match="require itself"):
        ToolDefinition("a.b", "…", args_validator=validator, handler=handler, requires=frozenset({"a.b"}))


def test_schema_comes_from_the_declared_source_or_is_absent() -> None:
    from_declared = ToolDefinition(
        "a.b", "…", args_validator=validator, handler=handler, args_schema=_SchemaModel.model_json_schema
    )
    assert from_declared.schema()["required"] == ["path"]
    from_callable = ToolDefinition(
        "a.c", "…", args_validator=validator, handler=handler, args_schema=lambda: {"type": "object"}
    )
    assert from_callable.schema() == {"type": "object"}
    assert ToolDefinition("a.d", "…", args_validator=validator, handler=handler).schema() is None


def test_a_schema_can_be_derived_from_the_validator_owner() -> None:
    """A bound method validator on a model-like object yields its own schema."""
    holder = _ValidatorHolder()
    definition = ToolDefinition("a.e", "…", args_validator=holder.validate, handler=handler)
    assert definition.schema()["required"] == ["path"]


def test_call_awaits_an_async_handler() -> None:
    async def async_handler(_context, arguments):
        return f"read {arguments['path']}"

    definition = ToolDefinition("a.b", "…", args_validator=validator, handler=async_handler)
    assert asyncio.run(definition.call(None, {"path": "n.md"})) == "read n.md"


def test_preview_cannot_be_async() -> None:
    async def async_preview(_context, arguments):
        return {"path": arguments["path"]}

    definition = ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validator,
        handler=handler,
        preview=async_preview,
    )
    with pytest.raises(ContractError, match="synchronous"):
        definition.preview_for(None, {"path": "n.md"})


def test_execution_mode_defaults_to_parallel_and_can_be_sequential() -> None:
    parallel = ToolDefinition("a.b", "…", args_validator=validator, handler=handler)
    sequential = ToolDefinition(
        "a.c", "…", args_validator=validator, handler=handler, execution_mode=ExecutionMode.SEQUENTIAL
    )
    assert parallel.runs_in_parallel
    assert not sequential.runs_in_parallel


def test_read_only_helper_declares_the_read_effect() -> None:
    from yanxu_harness.tools import read_only

    definition = read_only("notes.read", "Read a note", validator, handler)
    assert definition.effects == frozenset({ToolEffect.READ})
    assert definition.stable_id() == "notes.read@1"
