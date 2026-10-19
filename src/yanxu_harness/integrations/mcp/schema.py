"""远端 JSON Schema 的注册与真实验证。

规则来自计划 §5.2，逐条落地：

* 用真的 JSON Schema 校验器（``jsonschema``），不是「看一眼有 type 就当合法」；
* 默认 2020-12，显式声明 draft-07 也接受；**未知 dialect 直接拒绝**，而不是猜；
* 支持嵌套对象/数组、``required``、``enum``、``additionalProperties``、
  ``oneOf``/``anyOf``、同文档 ``$ref``；
* **远程 ``$ref`` 一律拒绝**（注册前扫描，不允许解析器去联网）；
* schema 与参数的深度/节点/字节都设上限；非法或过复杂在注册前拒绝；
* 验证只做验证：不补默认值、不改类型、不删参数、不改 canonical args hash；
* 远端 schema 里出现身份/凭据/端点字段也不会取得权威——那由宿主绑定决定。

适配器返回的校验结果就是原样 mapping；调用方（Gateway）用它算 args hash。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import McpConfigurationError, McpInvalidArguments, McpSchemaError

__all__ = [
    "ALLOWED_DIALECTS",
    "DEFAULT_DIALECT",
    "DRAFT7_DIALECT",
    "DRAFT2020_DIALECT",
    "RemoteToolSchema",
    "check_schema_document",
    "schema_complexity",
]

DRAFT2020_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DRAFT7_DIALECT = "http://json-schema.org/draft-07/schema#"

#: The dialects this adapter will validate against. Anything else — including a
#: missing/unknown `$schema` value we cannot place — is refused rather than
#: validated under a guessed meta-schema.
ALLOWED_DIALECTS: tuple[str, ...] = (DRAFT2020_DIALECT, DRAFT7_DIALECT)
DEFAULT_DIALECT = DRAFT2020_DIALECT

#: Structural bounds. A schema that needs more than this is refused at registration.
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_NODES = 4000
MAX_ARGUMENT_DEPTH = 32
MAX_ARGUMENT_NODES = 4000


def schema_complexity(value: Any) -> tuple[int, int]:
    """Return ``(nodes, depth)`` for a JSON value, without recursing forever."""
    nodes = 0
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if nodes > MAX_SCHEMA_NODES * 4:
            # Stop early; the caller compares against its own limit anyway.
            break
        if isinstance(item, Mapping):
            for child in item.values():
                stack.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            for child in item:
                stack.append((child, depth + 1))
    return nodes, max_depth


#: Every keyword that can make a validator resolve another document. `$dynamicRef`
#: (2020-12) and `$recursiveRef` (2019-09) are the ones a plain `$ref` scan misses; a
#: supported dialect is allowed to use them, so they are checked here as well.
_EXTERNAL_REF_KEYS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


def _remote_refs(value: Any, path: str = "") -> list[str]:
    """Every reference that is not a same-document fragment, with where it was found."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            here = f"{path}/{key}"
            if key in _EXTERNAL_REF_KEYS and isinstance(child, str) and not child.startswith("#"):
                found.append(f"{here}={child}")
            found.extend(_remote_refs(child, here))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_remote_refs(child, f"{path}/{index}"))
    return found


def _no_retrieval_registry() -> Any:
    """A `referencing` registry whose retrieve callback always refuses.

    The keyword scan above is the first line of defence; this is the second. Even a
    reference shape the scan does not know about can then only fail validation — it
    can never make the host fetch a document the server named. Both lines are
    independent on purpose: a string check alone has already been shown to miss a
    supported keyword.
    """
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    def refuse(uri: str) -> Any:
        raise NoSuchResource(ref=uri)

    return Registry(retrieve=refuse)


def check_schema_document(
    tool_name: str,
    schema: Mapping[str, Any] | None,
    *,
    max_bytes: int,
    max_depth: int = MAX_SCHEMA_DEPTH,
    max_nodes: int = MAX_SCHEMA_NODES,
    label: str = "input schema",
) -> None:
    """Bounds + reference checks shared by the input *and* output schema paths.

    The output schema is not compiled into a validator here, but it participates in
    the frozen definition version and in what the host may later act on. Auditing only
    the input path would leave a second, unchecked document in the catalog.
    """
    if schema is None:
        raise McpSchemaError(f"{tool_name}: the tool declares no {label}")
    if not isinstance(schema, Mapping):
        raise McpSchemaError(f"{tool_name}: the {label} must be an object")

    encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True, default=_refuse_default)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise McpSchemaError(
            f"{tool_name}: the {label} is {len(encoded.encode('utf-8'))} bytes, "
            f"above the {max_bytes}-byte limit"
        )
    nodes, depth = schema_complexity(schema)
    if nodes > max_nodes:
        raise McpSchemaError(
            f"{tool_name}: the {label} has {nodes} nodes, above {max_nodes}"
        )
    if depth > max_depth:
        raise McpSchemaError(
            f"{tool_name}: the {label} nests {depth} levels, above {max_depth}"
        )

    remote = _remote_refs(schema)
    if remote:
        raise McpSchemaError(
            f"{tool_name}: remote $ref/$dynamicRef/$recursiveRef values are refused "
            f"(no network resolution) in the {label}: " + ", ".join(sorted(remote)[:4])
        )
    for pointer in _local_refs(schema):
        _resolve_pointer(schema, pointer)  # raises when it does not resolve


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve one same-document JSON pointer, returning a sentinel on failure."""
    if pointer in {"", "#"}:
        return document
    fragment = pointer[1:] if pointer.startswith("#") else pointer
    if not fragment.startswith("/"):
        raise McpSchemaError(f"unsupported local reference {pointer!r}")
    target = document
    for raw in fragment[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and token in target:
            target = target[token]
        elif isinstance(target, list) and token.isdigit() and int(token) < len(target):
            target = target[int(token)]
        else:
            raise McpSchemaError(f"unresolvable local reference {pointer!r}")
    return target


@dataclass(frozen=True)
class RemoteToolSchema:
    """One remote tool's input schema, checked and ready to validate arguments."""

    tool_name: str
    document: Mapping[str, Any]
    dialect: str
    _validator: Any
    _validator_cls: Any

    # -- registration ---------------------------------------------------

    @classmethod
    def register(
        cls,
        tool_name: str,
        schema: Mapping[str, Any] | None,
        *,
        max_bytes: int,
        max_depth: int = MAX_SCHEMA_DEPTH,
        max_nodes: int = MAX_SCHEMA_NODES,
    ) -> "RemoteToolSchema":
        """Check and compile one remote schema, or refuse it with a reason."""
        check_schema_document(
            tool_name,
            schema,
            max_bytes=max_bytes,
            max_depth=max_depth,
            max_nodes=max_nodes,
            label="input schema",
        )
        assert schema is not None  # narrowed by check_schema_document

        dialect = schema.get("$schema", DEFAULT_DIALECT)
        if dialect not in ALLOWED_DIALECTS:
            raise McpSchemaError(
                f"{tool_name}: the schema declares dialect {dialect!r}, which this release does not "
                f"validate against; allowed: {', '.join(ALLOWED_DIALECTS)}"
            )

        import jsonschema

        validator_cls = (
            jsonschema.Draft202012Validator if dialect == DRAFT2020_DIALECT else jsonschema.Draft7Validator
        )
        try:
            # `check_schema` is the difference between "we store it" and "it means
            # something": a schema whose keywords are malformed is refused here.
            validator_cls.check_schema(schema)
        except Exception as exc:  # noqa: BLE001 - jsonschema raises its own hierarchy
            raise McpSchemaError(f"{tool_name}: the schema is not a valid {dialect} schema: {exc}") from exc
        # The registry is bound to the *validator that will actually run*: the scan
        # above is not the only guarantee, and a future dialect keyword cannot turn a
        # parameter check into an outbound request.
        registry = _no_retrieval_registry()
        return cls(
            tool_name=tool_name,
            document=dict(schema),
            dialect=dialect,
            _validator=validator_cls(dict(schema), registry=registry),
            _validator_cls=validator_cls,
        )

    # -- validation -----------------------------------------------------

    @property
    def dialect_name(self) -> str:
        return "2020-12" if self.dialect == DRAFT2020_DIALECT else "draft-07"

    def validate(self, arguments: Any) -> Mapping[str, Any]:
        """Validate one call's arguments, returning them *unchanged*.

        No defaults are inserted, no types are coerced, no unknown keys are dropped:
        the value that reaches the handler (and the args hash) is exactly what the
        model produced, or the call does not happen at all.
        """
        if not isinstance(arguments, Mapping):
            raise McpInvalidArguments(f"{self.tool_name}: arguments must be an object")
        nodes, depth = schema_complexity(arguments)
        if nodes > MAX_ARGUMENT_NODES:
            raise McpInvalidArguments(f"{self.tool_name}: the arguments have {nodes} nodes")
        if depth > MAX_ARGUMENT_DEPTH:
            raise McpInvalidArguments(f"{self.tool_name}: the arguments nest {depth} levels")
        try:
            encoded = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=_refuse_default)
        except TypeError as exc:
            raise McpInvalidArguments(f"{self.tool_name}: the arguments are not JSON: {exc}") from exc
        if len(encoded.encode("utf-8")) > self._max_argument_bytes:
            raise McpInvalidArguments(
                f"{self.tool_name}: the arguments exceed {self._max_argument_bytes} bytes"
            )

        errors = sorted(self._validator.iter_errors(dict(arguments)), key=lambda error: list(error.path))
        if errors:
            raise McpInvalidArguments(
                f"{self.tool_name}: invalid arguments — {_describe(errors[0])}"
            )
        return arguments

    # Set by `with_limits`, since a frozen dataclass cannot be mutated in place.
    _max_argument_bytes: int = 1024 * 1024

    def with_limits(self, *, max_argument_bytes: int) -> "RemoteToolSchema":
        return RemoteToolSchema(
            tool_name=self.tool_name,
            document=self.document,
            dialect=self.dialect,
            _validator=self._validator,
            _validator_cls=self._validator_cls,
            _max_argument_bytes=max_argument_bytes,
        )


def _local_refs(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "$ref" and isinstance(child, str) and child.startswith("#"):
                found.append(child)
            found.extend(_local_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_local_refs(child))
    return found


def _refuse_default(value: Any) -> Any:
    raise McpConfigurationError(f"the schema contains a non-JSON value of type {type(value).__name__}")


def _describe(error: Any) -> str:
    location = "/".join(str(part) for part in error.path) or "<root>"
    return f"{location}: {error.message}"[:500]


def iter_refs(schema: Mapping[str, Any]) -> Iterable[str]:
    return _remote_refs(schema)
