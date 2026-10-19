"""MCP04 的 Schema 层：真校验、拒绝未知方言与远程 $ref、参数不被改写。"""

from __future__ import annotations

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.integrations.mcp import McpInvalidArguments, McpSchemaError, RemoteToolSchema  # noqa: E402

NESTED = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "target": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "mode": {"enum": ["replace", "append"]}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "note": {"oneOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["target"],
    "additionalProperties": False,
}

DRAFT7 = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {"point": {"$ref": "#/definitions/point"}},
    "required": ["point"],
    "definitions": {
        "point": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        }
    },
}


def register(schema, name="tool", **kwargs) -> RemoteToolSchema:
    return RemoteToolSchema.register(name, schema, max_bytes=kwargs.pop("max_bytes", 64 * 1024), **kwargs)


def test_2020_12_and_draft_07_schemas_are_compiled() -> None:
    modern = register(NESTED)
    assert modern.dialect_name == "2020-12"
    draft7 = register(DRAFT7)
    assert draft7.dialect_name == "draft-07"
    draft7.validate({"point": {"x": 1, "y": 2}})


def test_validation_returns_the_arguments_unchanged() -> None:
    schema = register(
        {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "planted"}},
            "required": ["path"],
            "additionalProperties": False,
        }
    )
    original = {"path": "a.md"}
    validated = schema.validate(original)
    assert validated is original, "no copy, no default insertion, no coercion"
    assert "planted" not in validated


def test_nested_rules_are_enforced() -> None:
    schema = register(NESTED)
    schema.validate({"target": {"path": "a.md", "mode": "replace"}, "tags": ["x"], "note": None})
    with pytest.raises(McpInvalidArguments):
        schema.validate({"target": {"mode": "replace"}})  # required path
    with pytest.raises(McpInvalidArguments):
        schema.validate({"target": {"path": "a.md", "mode": "merge"}})  # enum
    with pytest.raises(McpInvalidArguments):
        schema.validate({"target": {"path": "a.md"}, "extra": 1})  # additionalProperties
    with pytest.raises(McpInvalidArguments):
        schema.validate({"target": {"path": "a.md"}, "note": 5})  # oneOf


def test_declared_types_are_not_coerced() -> None:
    schema = register({"type": "object", "properties": {"count": {"type": "integer"}}})
    with pytest.raises(McpInvalidArguments):
        schema.validate({"count": "3"})
    schema.validate({"count": 3})


def test_a_remote_ref_is_refused_without_being_resolved() -> None:
    with pytest.raises(McpSchemaError) as caught:
        register(
            {
                "type": "object",
                "properties": {"value": {"$ref": "https://example.invalid/schema.json#/value"}},
            }
        )
    assert "remote $ref" in str(caught.value)


def test_an_unresolvable_local_ref_is_refused() -> None:
    with pytest.raises(McpSchemaError) as caught:
        register({"type": "object", "properties": {"value": {"$ref": "#/definitions/missing"}}})
    assert "unresolvable" in str(caught.value)


def test_an_unknown_dialect_is_refused() -> None:
    with pytest.raises(McpSchemaError) as caught:
        register({"$schema": "https://example.invalid/dialect", "type": "object"})
    assert "dialect" in str(caught.value)


def test_a_malformed_schema_is_refused() -> None:
    with pytest.raises(McpSchemaError):
        register({"type": "object", "properties": 5})


def test_over_complex_schemas_are_refused() -> None:
    wide = {"type": "object", "properties": {f"f{index}": {"type": "string"} for index in range(3000)}}
    with pytest.raises(McpSchemaError) as caught:
        register(wide, name="wide", max_bytes=1_000_000, max_nodes=50)
    assert "nodes" in str(caught.value)

    with pytest.raises(McpSchemaError) as caught_bytes:
        register({"type": "object", "properties": {"a": {"type": "string"}}}, max_bytes=16)
    assert "bytes" in str(caught_bytes.value)

    deep: dict = {"type": "object"}
    cursor = deep
    for _ in range(40):
        child: dict = {"type": "object", "properties": {}}
        cursor["properties"] = {"child": child}
        cursor = child
    with pytest.raises(McpSchemaError) as caught_depth:
        register(deep, name="deep")
    assert "nests" in str(caught_depth.value)


def test_argument_bounds_are_enforced() -> None:
    schema = register({"type": "object", "properties": {"value": {"type": "string"}}}).with_limits(
        max_argument_bytes=64
    )
    with pytest.raises(McpInvalidArguments) as caught:
        schema.validate({"value": "x" * 200})
    assert "bytes" in str(caught.value)
    with pytest.raises(McpInvalidArguments):
        schema.validate(["not", "an", "object"])
