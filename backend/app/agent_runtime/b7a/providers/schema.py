"""Strict schema helpers for Codex structured outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple


FORBIDDEN_KEYWORDS = {
    "allOf",
    "anyOf",
    "not",
    "if",
    "then",
    "else",
    "dependentRequired",
    "dependentSchemas",
    "patternProperties",
}

ALLOWED_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "$defs",
    "enum",
    "const",
    "minItems",
    "maxItems",
}


@dataclass(slots=True)
class SchemaValidationError(ValueError):
    pointer: str
    reason: str

    def __str__(self) -> str:
        return f"{self.pointer}: {self.reason}"


def build_codex_output_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "proposal_id",
            "task_id",
            "explanation",
            "files",
            "expected_tests",
            "risks",
            "provider_metadata",
        ],
        "properties": {
            "proposal_id": {"type": "string"},
            "task_id": {"type": "integer"},
            "explanation": {"type": "string"},
            "files": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "relative_path",
                        "operation",
                        "expected_sha256",
                        "new_content",
                        "encoding",
                    ],
                    "properties": {
                        "relative_path": {"type": "string"},
                        "operation": {"type": "string", "const": "modify"},
                        "expected_sha256": {"type": "string"},
                        "new_content": {"type": "string"},
                        "encoding": {"type": "string", "const": "utf-8"},
                    },
                },
            },
            "expected_tests": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "provider_metadata": {
                "type": "object",
                "additionalProperties": False,
                "required": ["provider", "model", "request_id", "finish_reason"],
                "properties": {
                    "provider": {"type": "string", "const": "codex"},
                    "model": {"type": "string"},
                    "request_id": {"type": ["string", "null"]},
                    "finish_reason": {"type": ["string", "null"]},
                },
            },
        },
    }


def validate_strict_schema(schema: Dict[str, Any]) -> None:
    _validate_node(schema, pointer="")


def _validate_node(node: Any, *, pointer: str) -> None:
    if not isinstance(node, dict):
        raise SchemaValidationError(pointer or "/", "schema node must be an object")

    _check_forbidden_keywords(node, pointer=pointer)
    _check_allowed_keys(node, pointer=pointer)

    schema_type = node.get("type")
    if pointer == "" and isinstance(schema_type, list):
        raise SchemaValidationError(pointer or "/", "root schema must not use union type")

    if pointer == "" and schema_type != "object":
        raise SchemaValidationError(pointer or "/", "root schema must be an object")

    if schema_type == "object":
        _validate_object_node(node, pointer=pointer)
        return

    if schema_type == "array":
        _validate_array_node(node, pointer=pointer)
        return

    if isinstance(schema_type, list):
        _validate_union_type(node, pointer=pointer)
        return

    _validate_primitive_node(node, pointer=pointer)


def _validate_object_node(node: Dict[str, Any], *, pointer: str) -> None:
    if node.get("type") != "object":
        raise SchemaValidationError(pointer or "/", 'object node must declare "type": "object"')
    if "properties" not in node:
        raise SchemaValidationError(pointer or "/", 'object node must include "properties"')
    if "additionalProperties" not in node or node["additionalProperties"] is not False:
        raise SchemaValidationError(pointer or "/", 'object node must include "additionalProperties": false')
    if not isinstance(node["properties"], dict):
        raise SchemaValidationError(_child(pointer, "properties"), "properties must be an object")

    properties = node["properties"]
    required = node.get("required")
    if not isinstance(required, list):
        raise SchemaValidationError(_child(pointer, "required"), "required must be an array")

    property_keys = list(properties.keys())
    required_keys = [item for item in required if isinstance(item, str)]
    if set(required_keys) != set(property_keys) or len(required_keys) != len(property_keys):
        raise SchemaValidationError(_child(pointer, "required"), "required must include every property exactly once")

    for key, value in properties.items():
        _validate_node(value, pointer=_child(_child(pointer, "properties"), key))

    if "$defs" in node:
        defs = node["$defs"]
        if not isinstance(defs, dict):
            raise SchemaValidationError(_child(pointer, "$defs"), "$defs must be an object")
        for key, value in defs.items():
            _validate_node(value, pointer=_child(_child(pointer, "$defs"), key))


def _validate_array_node(node: Dict[str, Any], *, pointer: str) -> None:
    if node.get("type") != "array":
        raise SchemaValidationError(pointer or "/", 'array node must declare "type": "array"')
    if "items" not in node:
        raise SchemaValidationError(pointer or "/", 'array node must include "items"')
    _validate_node(node["items"], pointer=_child(pointer, "items"))


def _validate_union_type(node: Dict[str, Any], *, pointer: str) -> None:
    type_value = node.get("type")
    if not isinstance(type_value, list) or not type_value:
        raise SchemaValidationError(pointer or "/", "union type must be a non-empty list")
    if "object" in type_value or "array" in type_value:
        raise SchemaValidationError(pointer or "/", "object and array nodes must not use union types")
    if "null" not in type_value:
        raise SchemaValidationError(pointer or "/", "optional fields must include null in type union")
    for key in ("properties", "required", "additionalProperties", "items", "$defs"):
        if key in node:
            raise SchemaValidationError(_child(pointer, key), f"keyword {key!r} is not supported on union nodes")


def _validate_primitive_node(node: Dict[str, Any], *, pointer: str) -> None:
    if "items" in node:
        raise SchemaValidationError(pointer or "/", 'non-array schema must not include "items"')
    if "properties" in node:
        raise SchemaValidationError(pointer or "/", 'non-object schema must not include "properties"')
    if "required" in node:
        raise SchemaValidationError(pointer or "/", 'non-object schema must not include "required"')
    if "additionalProperties" in node:
        raise SchemaValidationError(pointer or "/", 'non-object schema must not include "additionalProperties"')
    if "$defs" in node:
        defs = node["$defs"]
        if not isinstance(defs, dict):
            raise SchemaValidationError(_child(pointer, "$defs"), "$defs must be an object")
        for key, value in defs.items():
            _validate_node(value, pointer=_child(_child(pointer, "$defs"), key))


def _check_forbidden_keywords(node: Dict[str, Any], *, pointer: str) -> None:
    for key in node.keys():
        if key in FORBIDDEN_KEYWORDS:
            raise SchemaValidationError(_child(pointer, key), f"keyword {key!r} is not supported")


def _check_allowed_keys(node: Dict[str, Any], *, pointer: str) -> None:
    for key in node.keys():
        if key not in ALLOWED_SCHEMA_KEYS:
            raise SchemaValidationError(_child(pointer, key), f"keyword {key!r} is not supported")


def _child(pointer: str, segment: str) -> str:
    escaped = segment.replace("~", "~0").replace("/", "~1")
    if not pointer:
        return f"/{escaped}"
    return f"{pointer}/{escaped}"
