"""What each provider enforces of a JSON Schema (`dialects.schema_support`), case by case from
the providers' documentation as read on 2026-11-09:

- Anthropic: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Gemini: https://aistudio.google.com/docs/structured-output
- OpenAI: https://developers.openai.com/api/docs/guides/structured-outputs

When a provider widens or narrows its subset, the case changes with the table it tests.
"""

from typing import Any

import pytest

from behaviors.implementations.dialects.schema_support import (
    anthropic_rejections,
    anthropic_transform,
    gemini_rejects,
    gemini_unvalidated_keywords,
    move_keywords,
    openai_rejects,
    openai_strict_movable,
    openai_strict_violations,
    unsupported_keywords,
)
from behaviors.types.json import JSONObject


def _closed(**properties: Any) -> JSONObject:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }
    return schema


@pytest.mark.parametrize(
    ("schema", "unsupported"),
    [
        # Supported: basic types, enum, const, minItems of 0 or 1, the listed formats.
        (_closed(s={"type": "string", "enum": ["a", "b"]}), ()),
        (_closed(a={"type": "array", "items": {"type": "string"}, "minItems": 1}), ()),
        (_closed(t={"type": "string", "format": "date-time"}), ()),
        # Numeric and string-length constraints are not supported.
        (_closed(n={"type": "number", "minimum": 0}), ("minimum",)),
        (_closed(s={"type": "string", "maxLength": 3}), ("maxLength",)),
        # minItems only of 0 or 1.
        (_closed(a={"type": "array", "items": {}, "minItems": 2}), ("minItems",)),
        # Simple regexes are supported; backreferences, lookaround and word boundaries are not.
        (_closed(s={"type": "string", "pattern": "^[a-z]+\\d{2,4}$"}), ()),
        (_closed(s={"type": "string", "pattern": "(a)\\1"}), ("pattern",)),
        (_closed(s={"type": "string", "pattern": "^(?!x)"}), ("pattern",)),
        (_closed(s={"type": "string", "pattern": "\\bword\\b"}), ("pattern",)),
        # enum of strings, numbers, booleans and nulls; complex values are a 400.
        (_closed(e={"enum": ["a", 1, 2.5, True, None]}), ()),
        (_closed(e={"enum": [{"a": 1}]}), ("enum",)),
        (_closed(e={"enum": [[1, 2]]}), ("enum",)),
        # allOf is supported, but not with $ref.
        (_closed(x={"allOf": [{"type": "string"}]}), ()),
        (_closed(x={"allOf": [{"$ref": "#/$defs/d"}]}), ("allOf",)),
        # additionalProperties must be false on every object.
        ({"type": "object", "properties": {}}, ("additionalProperties",)),
        # ... including a nullable object's.
        (_closed(o={"type": ["object", "null"]}), ("additionalProperties",)),
        # $ref within the schema only.
        ({**_closed(x={"$ref": "#/$defs/d"}), "$defs": {"d": {"type": "string"}}}, ()),
        (_closed(x={"$ref": "https://example.com/s.json"}), ("$ref",)),
    ],
)
def test_anthropic_subset(schema: JSONObject, unsupported: tuple[str, ...]) -> None:
    assert unsupported_keywords(schema, "anthropic") == unsupported


@pytest.mark.parametrize(
    "member",
    [
        {"$ref": "https://example.com/s.json"},
        # A local definition with a sibling constraint: both apply, so neither may replace the
        # other.
        {"$ref": "#/$defs/small", "enum": [2, 3]},
    ],
)
def test_anthropic_transform_moves_an_allof_with_a_ref(member: JSONObject) -> None:
    schema: JSONObject = {
        **_closed(x={"allOf": [member]}),
        "$defs": {"small": {"enum": [1, 2]}},
    }
    fitted = anthropic_transform(schema)
    assert fitted.moved == ("allOf",)
    properties: Any = fitted.schema["properties"]
    assert "allOf" not in properties["x"] and "allOf" in properties["x"]["description"]


def test_anthropic_transform_moves_a_complex_enum() -> None:
    fitted = anthropic_transform(_closed(e={"enum": [{"a": 1}]}))
    assert fitted.moved == ("enum",)


@pytest.mark.parametrize(
    ("schema", "unsupported"),
    [
        (_closed(a={"type": "array", "items": {}, "minItems": 1, "maxItems": 3}), ()),
        (_closed(n={"type": "integer", "minimum": 0, "maximum": 9}), ()),
        # Stripped and ignored rather than rejected.
        (_closed(s={"type": "string", "minLength": 1}), ("minLength",)),
        (_closed(a={"type": "array", "items": {}, "uniqueItems": True}), ("uniqueItems",)),
        # Not documented, so not counted as enforced.
        (_closed(s={"type": "string", "pattern": "^a"}), ("pattern",)),
    ],
)
def test_gemini_subset(schema: JSONObject, unsupported: tuple[str, ...]) -> None:
    assert unsupported_keywords(schema, "gemini") == unsupported


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        (_closed(s={"type": "string", "pattern": "^a", "format": "email"}), None),
        (_closed(n={"type": "number", "minimum": 0, "multipleOf": 2}), None),
        # Every property required, every object closed.
        ({**_closed(a={"type": "string"}), "required": []}, "an object with optional properties"),
        (
            {"type": "object", "properties": {}, "required": []},
            "an object without additionalProperties: false",
        ),
        # A nullable object is an object.
        (_closed(o={"type": ["object", "null"]}), "an object without additionalProperties: false"),
        # The root is an object, and not an anyOf.
        ({"type": "string"}, "the root is not an object"),
        ({**_closed(), "anyOf": [_closed()]}, "the root is an anyOf"),
        # allOf, not and if/then/else are not supported.
        ({**_closed(x={"allOf": [{"type": "string"}]})}, "unsupported keywords: allOf"),
    ],
)
def test_openai_strict_rules(schema: JSONObject, problem: str | None) -> None:
    problems = openai_strict_violations(schema)
    assert problems == () if problem is None else problem in problems


def test_anthropic_transform_closes_a_nullable_object_and_moves_an_external_ref() -> None:
    fitted = anthropic_transform(
        _closed(o={"type": ["object", "null"]}, x={"$ref": "https://example.com/s.json"})
    )
    properties: Any = fitted.schema["properties"]
    assert properties["o"]["additionalProperties"] is False and fitted.strengthened
    assert "$ref" not in properties["x"] and fitted.moved == ("$ref",)


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        (_closed(x={"type": "string"}), None),
        (_closed(x={"enum": ["a"]}), None),
        (_closed(x={"anyOf": [{"type": "string"}]}), None),
        (_closed(x={}), "a node without a type"),
        (_closed(x={"description": "moved out"}), "a node without a type"),
        (_closed(x=True), "a boolean subschema"),
        ({**_closed(x={"$ref": "#"})}, "a recursive reference"),
        (
            {
                **_closed(x={"$ref": "#/$defs/a"}),
                "$defs": {
                    "a": _closed(b={"$ref": "#/$defs/b"}),
                    "b": _closed(a={"$ref": "#/$defs/a"}),
                },
            },
            "a recursive reference",
        ),
        (
            {**_closed(x={"$ref": "#/$defs/a"}), "$defs": {"a": _closed(s={"type": "string"})}},
            None,
        ),
    ],
)
def test_anthropic_rejections(schema: JSONObject, problem: str | None) -> None:
    problems = anthropic_rejections(schema)
    assert problems == () if problem is None else problem in problems


@pytest.mark.parametrize(
    ("enum", "unsupported"),
    [(["a", "b"], ()), (["a", 1], ("enum",)), ([{"a": 1}], ("enum",))],
)
def test_gemini_enforces_string_enums_only(
    enum: list[object], unsupported: tuple[str, ...]
) -> None:
    assert (
        unsupported_keywords(_closed(e={"type": "string", "enum": enum}), "gemini") == unsupported
    )


@pytest.mark.parametrize(
    ("extra", "unvalidated"),
    [
        ({"type": "object", "additionalProperties": False}, ("additionalProperties",)),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            ("additionalProperties",),
        ),
        (_closed(a={"type": "string"}), ()),
        ({"type": "object"}, ()),
    ],
)
def test_gemini_does_not_validate_closing_an_object_without_properties(
    extra: JSONObject, unvalidated: tuple[str, ...]
) -> None:
    assert gemini_unvalidated_keywords(_closed(extra=extra)) == unvalidated


def test_move_keywords_moves_what_the_provider_rejects_anywhere_in_the_schema() -> None:
    schema: JSONObject = {
        **_closed(e={"enum": [{"a": 1}]}, p={"type": "string", "pattern": "^\\p{L}$"}),
        "$defs": {"d": {"type": "string", "pattern": "^a$"}},
    }
    fitted = move_keywords(schema, openai_rejects)
    properties: Any = fitted.schema["properties"]
    assert fitted.moved == ("enum", "pattern")
    assert "enum" not in properties["e"] and "pattern" not in properties["p"]
    assert "enum" in properties["e"]["description"]
    defs: Any = fitted.schema["$defs"]
    assert defs["d"]["pattern"] == "^a$"  # a pattern OpenAI takes stays


def test_only_keywords_that_constrain_their_own_value_move_out_of_an_openai_strict_schema() -> None:
    schema = _closed(
        s={"type": "string", "format": "citations", "minLength": 1},
        a={"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        d={"type": "string", "format": "date"},
        o={"type": "integer", "format": "int32"},
        n={"type": "string", "not": {"const": "x"}},
    )
    fitted = move_keywords(schema, openai_strict_movable)
    properties: Any = fitted.schema["properties"]
    assert fitted.moved == ("format", "uniqueItems")
    assert set(properties["s"]) == {"type", "minLength", "description"}  # OpenAI enforces it
    assert "uniqueItems" not in properties["a"]
    assert properties["d"]["format"] == "date"  # a format OpenAI takes stays
    assert properties["o"]["format"] == "int32"  # not a string format
    assert "not" in properties["n"]  # holds a subschema
    assert "unsupported keywords: not" in openai_strict_violations(fitted.schema)


@pytest.mark.parametrize("rejects", [openai_rejects, gemini_rejects])
def test_move_keywords_leaves_property_names_and_annotation_values_alone(rejects: Any) -> None:
    # Property names and example data that look like the keywords a provider rejects.
    schema: JSONObject = {
        **_closed(
            enum={"type": "string"},
            pattern={"type": "string"},
            **{"$ref": {"type": "string"}},
        ),
        "examples": [{"enum": [{"a": 1}], "$ref": "https://example.com"}],
    }
    fitted = move_keywords(schema, rejects)
    assert fitted.moved == ()
    assert fitted.schema == schema
