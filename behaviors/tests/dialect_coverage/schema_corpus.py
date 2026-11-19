"""A corpus of JSON Schemas covering the features providers limit, and what the providers did
with each when it was sent live (`test_schema_acceptance_llm_integration`, 2026-11-10).

The live test sends the corpus and records the wire form of each case a provider took
(`ACCEPTED_PATH`); `test_schema_corpus` holds adaptation to that record without a network: a change
to what is sent for a case fails until a live run has seen the provider take the new form.
"""

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.types.content import ContentText
from behaviors.types.json import JSONObject
from behaviors.types.message import UserMessage
from behaviors.types.response import ModelRequest, ResponseFormatJsonSchema, SamplingParams
from behaviors.types.tool import ToolChoiceAuto, ToolChoiceRequired, ToolParam


def _closed(required: list[str] | None = None, /, **properties: Any) -> dict[str, Any]:
    """An object every provider's strict mode can take as is: every property required, no
    others allowed."""
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties) if required is None else required,
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_PERSON = _closed(name=_STRING, age={"type": "integer"})


@dataclass(frozen=True)
class Case:
    name: str
    schema: dict[str, Any]


# Each schema is valid JSON Schema 2020-12 unless it declares another draft, and satisfiable.
CORPUS: list[Case] = [
    # --- objects: closed, open, optional, empty, nullable --------------------------------------
    Case("closed object", _PERSON),
    Case("open object", {"type": "object", "properties": {"name": _STRING}, "required": ["name"]}),
    Case("additionalProperties true", {**_PERSON, "additionalProperties": True}),
    Case(
        "additionalProperties schema (a map)",
        _closed(tags={"type": "object", "additionalProperties": _STRING}),
    ),
    Case("optional property", _closed(["name"], name=_STRING, nickname=_STRING)),
    Case(
        "no required list",
        {"type": "object", "properties": {"name": _STRING}, "additionalProperties": False},
    ),
    Case(
        "nested closed objects",
        _closed(author=_closed(name=_STRING, born=_closed(year={"type": "integer"}))),
    ),
    Case(
        "nested open object",
        _closed(author={"type": "object", "properties": {"name": _STRING}, "required": ["name"]}),
    ),
    Case("object without properties", _closed(extra={"type": "object"})),
    Case(
        "closed object without properties",
        _closed(extra={"type": "object", "additionalProperties": False}),
    ),
    Case("nullable object, open", _closed(o={"type": ["object", "null"]})),
    Case("nullable object, closed", _closed(o={**_PERSON, "type": ["object", "null"]})),
    Case("object under anyOf with null", _closed(o={"anyOf": [_PERSON, {"type": "null"}]})),
    Case(
        "open object under anyOf",
        _closed(o={"anyOf": [{"type": "object", "properties": {"a": _STRING}}, {"type": "null"}]}),
    ),
    Case(
        "properties without type",
        _closed(o={"properties": {"a": _STRING}, "required": ["a"], "additionalProperties": False}),
    ),
    Case("unusual property names", _closed(**{"a b": _STRING, "$x": _STRING, "ü": _STRING})),
    Case(
        "schema keywords as property names",
        _closed(
            **{
                name: _STRING
                for name in ("type", "enum", "pattern", "items", "properties", "format")
            }
        ),
    ),
    Case("a property named $ref", _closed(**{"$ref": _STRING, "name": _STRING})),
    Case(
        "keyword-like annotation values",
        _closed(
            x={
                **_STRING,
                "examples": [{"enum": [{"a": 1}], "$ref": "https://example.com/s.json"}],
            }
        ),
    ),
    Case("many properties", _closed(**{f"p{i}": _STRING for i in range(60)})),
    Case("deep nesting", _closed(a=_closed(b=_closed(c=_closed(d=_closed(e=_closed(f=_STRING))))))),
    Case(
        "minProperties / maxProperties",
        _closed(
            m={
                "type": "object",
                "additionalProperties": _STRING,
                "minProperties": 1,
                "maxProperties": 3,
            }
        ),
    ),
    Case(
        "patternProperties",
        _closed(
            m={
                "type": "object",
                "patternProperties": {"^x": _STRING},
                "additionalProperties": False,
            }
        ),
    ),
    Case(
        "propertyNames",
        _closed(
            m={
                "type": "object",
                "propertyNames": {"pattern": "^[a-z]+$"},
                "additionalProperties": _STRING,
            }
        ),
    ),
    Case(
        "dependentRequired",
        {**_closed(["a"], a=_STRING, b=_STRING), "dependentRequired": {"b": ["a"]}},
    ),
    Case(
        "unevaluatedProperties",
        {
            "type": "object",
            "properties": {"a": _STRING},
            "required": ["a"],
            "unevaluatedProperties": False,
        },
    ),
    # --- types ---------------------------------------------------------------------------------
    Case(
        "scalar types",
        _closed(
            s=_STRING,
            i={"type": "integer"},
            n={"type": "number"},
            b={"type": "boolean"},
            z={"type": "null"},
        ),
    ),
    Case("type array of scalars", _closed(v={"type": ["string", "null"]})),
    Case("type array of three", _closed(v={"type": ["string", "integer", "null"]})),
    Case("no type (any value)", _closed(v={})),
    Case("boolean subschema true", _closed(v=True)),
    # --- enum and const ------------------------------------------------------------------------
    Case("string enum", _closed(c={"type": "string", "enum": ["red", "green"]})),
    Case("mixed scalar enum", _closed(c={"enum": ["a", 1, 2.5, True, None]})),
    Case("enum without type", _closed(c={"enum": ["a", "b"]})),
    Case("object enum", _closed(c={"enum": [{"a": 1}, {"a": 2}]})),
    Case("array enum", _closed(c={"enum": [[1, 2], [3]]})),
    Case("large enum", _closed(c={"type": "string", "enum": [f"v{i}" for i in range(600)]})),
    Case("const string", _closed(c={"const": "fixed"})),
    Case("const object", _closed(c={"const": {"a": 1}})),
    # --- numbers -------------------------------------------------------------------------------
    Case("minimum / maximum", _closed(n={"type": "integer", "minimum": 1, "maximum": 5})),
    Case(
        "exclusive bounds",
        _closed(n={"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}),
    ),
    Case("multipleOf", _closed(n={"type": "integer", "multipleOf": 5})),
    # --- strings -------------------------------------------------------------------------------
    Case("minLength / maxLength", _closed(s={"type": "string", "minLength": 2, "maxLength": 5})),
    Case("simple pattern", _closed(s={"type": "string", "pattern": "^[A-Z]{3}-\\d{2,4}$"})),
    Case("pattern with backreference", _closed(s={"type": "string", "pattern": "^(a+)-\\1$"})),
    Case(
        "pattern with lookahead", _closed(s={"type": "string", "pattern": "^(?=.*\\d)[a-z0-9]+$"})
    ),
    Case("pattern with lookbehind", _closed(s={"type": "string", "pattern": "^[a-z]+(?<!x)$"})),
    Case("pattern with word boundary", _closed(s={"type": "string", "pattern": "\\bcat\\b"})),
    Case("pattern with unicode class", _closed(s={"type": "string", "pattern": "^\\p{L}+$"})),
    Case("pattern without anchors", _closed(s={"type": "string", "pattern": "[0-9]"})),
    Case(
        "supported formats",
        _closed(
            t={"type": "string", "format": "date-time"},
            e={"type": "string", "format": "email"},
            u={"type": "string", "format": "uuid"},
            d={"type": "string", "format": "date"},
        ),
    ),
    Case(
        "other formats",
        _closed(
            r={"type": "string", "format": "uri-reference"},
            h={"type": "string", "format": "idn-hostname"},
        ),
    ),
    Case("unknown format", _closed(p={"type": "string", "format": "phone-number"})),
    Case("format on a non-string", _closed(n={"type": "integer", "format": "int32"})),
    Case(
        "contentEncoding / contentMediaType",
        _closed(b={"type": "string", "contentEncoding": "base64", "contentMediaType": "image/png"}),
    ),
    # --- arrays --------------------------------------------------------------------------------
    Case("array of strings", _closed(a={"type": "array", "items": _STRING})),
    Case("array of closed objects", _closed(a={"type": "array", "items": _PERSON})),
    Case(
        "array of open objects",
        _closed(a={"type": "array", "items": {"type": "object", "properties": {"a": _STRING}}}),
    ),
    Case("array without items", _closed(a={"type": "array"})),
    Case("minItems 1", _closed(a={"type": "array", "items": _STRING, "minItems": 1})),
    Case(
        "minItems 2 / maxItems",
        _closed(a={"type": "array", "items": _STRING, "minItems": 2, "maxItems": 4}),
    ),
    Case("uniqueItems", _closed(a={"type": "array", "items": _STRING, "uniqueItems": True})),
    Case(
        "prefixItems",
        _closed(a={"type": "array", "prefixItems": [_STRING, {"type": "integer"}], "items": False}),
    ),
    Case("contains", _closed(a={"type": "array", "items": _STRING, "contains": {"const": "x"}})),
    Case(
        "nested arrays",
        _closed(a={"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}),
    ),
    # --- combinators ---------------------------------------------------------------------------
    Case("anyOf of scalars", _closed(v={"anyOf": [_STRING, {"type": "integer"}]})),
    Case("anyOf of closed objects", _closed(v={"anyOf": [_PERSON, _closed(title=_STRING)]})),
    Case("oneOf", _closed(v={"oneOf": [_STRING, {"type": "integer"}]})),
    Case("allOf of inline schemas", _closed(v={"allOf": [_STRING, {"enum": ["a", "b"]}]})),
    Case("allOf of closed objects", _closed(v={"allOf": [_PERSON]})),
    Case(
        "allOf with a local $ref",
        {**_closed(v={"allOf": [{"$ref": "#/$defs/name"}]}), "$defs": {"name": _STRING}},
    ),
    Case("not", _closed(v={"type": "string", "not": {"const": "x"}})),
    Case(
        "if / then / else",
        _closed(
            v={
                "type": "integer",
                "if": {"minimum": 10},
                "then": {"multipleOf": 2},
                "else": {"maximum": 5},
            }
        ),
    ),
    # --- references ----------------------------------------------------------------------------
    Case(
        "local $ref in $defs",
        {**_closed(p={"$ref": "#/$defs/person"}), "$defs": {"person": _PERSON}},
    ),
    Case(
        "local $ref in definitions",
        {**_closed(p={"$ref": "#/definitions/person"}), "definitions": {"person": _PERSON}},
    ),
    Case(
        "$ref to a nested definition",
        {
            **_closed(p={"$ref": "#/$defs/group/$defs/person"}),
            "$defs": {"group": {"$defs": {"person": _PERSON}}},
        },
    ),
    Case("$ref into properties", _closed(a=_PERSON, b={"$ref": "#/properties/a"})),
    Case(
        "$ref with sibling description",
        {
            **_closed(p={"$ref": "#/$defs/person", "description": "who"}),
            "$defs": {"person": _PERSON},
        },
    ),
    Case(
        "$ref in array items",
        {
            **_closed(a={"type": "array", "items": {"$ref": "#/$defs/person"}}),
            "$defs": {"person": _PERSON},
        },
    ),
    Case(
        "$ref under anyOf",
        {
            **_closed(v={"anyOf": [{"$ref": "#/$defs/person"}, {"type": "null"}]}),
            "$defs": {"person": _PERSON},
        },
    ),
    Case("root $ref", {"$ref": "#/$defs/person", "$defs": {"person": _PERSON}}),
    Case(
        "recursive $ref",
        {
            **_closed(root={"$ref": "#/$defs/node"}),
            "$defs": {
                "node": _closed(
                    label=_STRING, children={"type": "array", "items": {"$ref": "#/$defs/node"}}
                )
            },
        },
    ),
    Case(
        "recursive root $ref",
        {**_closed(label=_STRING, children={"type": "array", "items": {"$ref": "#"}})},
    ),
    Case(
        "open object in $defs",
        {
            **_closed(p={"$ref": "#/$defs/loose"}),
            "$defs": {"loose": {"type": "object", "properties": {"a": _STRING}}},
        },
    ),
    Case("unused $defs", {**_PERSON, "$defs": {"unused": {"type": "object"}}}),
    Case("external $ref", _closed(p={"$ref": "https://json-schema.org/draft/2020-12/schema"})),
    Case(
        "$id and relative $ref",
        {
            **_closed(p={"$ref": "person"}),
            "$id": "https://example.com/root",
            "$defs": {"person": {**_PERSON, "$id": "person"}},
        },
    ),
    Case(
        "$anchor",
        {**_closed(p={"$ref": "#person"}), "$defs": {"person": {**_PERSON, "$anchor": "person"}}},
    ),
    # --- declarations and annotations ----------------------------------------------------------
    Case("$schema 2020-12", {"$schema": "https://json-schema.org/draft/2020-12/schema", **_PERSON}),
    Case("$schema draft-07", {"$schema": "http://json-schema.org/draft-07/schema#", **_PERSON}),
    Case(
        "annotations",
        {
            **_closed(
                name={
                    **_STRING,
                    "title": "Name",
                    "description": "a name",
                    "examples": ["Ada"],
                    "default": "Ada",
                    "deprecated": False,
                    "readOnly": True,
                    "$comment": "c",
                }
            ),
            "title": "Person",
            "description": "someone",
        },
    ),
    Case("default on an object", _closed(o={**_PERSON, "default": {"name": "a", "age": 1}})),
    # --- roots other than a closed object (response formats only) -------------------------------
    Case("string root", {"type": "string"}),
    Case("array root", {"type": "array", "items": _STRING}),
    Case("anyOf root", {"anyOf": [_PERSON, _closed(title=_STRING)]}),
    Case("nullable root", {**_PERSON, "type": ["object", "null"]}),
]

# Known limits adaptation does not handle, by (provider, case, use): each is a 400 the caller
# sees, and the case is an expected failure when it occurs (and passes once a provider lifts it).


# Known limits adaptation does not handle, by (provider, case, use): each is a 400 the caller
# sees, and the case is an expected failure when it occurs (and passes once a provider lifts it).
@dataclass(frozen=True)
class KnownLimit:
    """A failure adaptation does not prevent, and how to recognize it: a 400 whose description
    contains `signature`. Any other failure of the case is still a failure."""

    reason: str
    signature: str


_PROPERTY_NAMES = KnownLimit(
    "Anthropic tool property names must match ^[a-zA-Z0-9_.-]{1,64}$; not renamed",
    "Property keys should match pattern",
)
KNOWN_LIMITS: dict[tuple[str, str, str], KnownLimit] = {
    ("anthropic", "many properties", "tool"): KnownLimit(
        "Anthropic's grammar size limit is not modeled", "compiled grammar is too large"
    ),
    ("anthropic", "unusual property names", "tool"): _PROPERTY_NAMES,
    ("anthropic", "a property named $ref", "tool"): _PROPERTY_NAMES,
    **{
        ("openai_chat_completions", f"pattern with {feature}", "tool"): KnownLimit(
            "gpt-4o-mini runs a strict Chat Completions tool call with this pattern to the token "
            "limit (a generation problem, not a rejection; Responses does not)",
            "max_tokens or model output limit was reached",
        )
        for feature in ("backreference", "lookahead", "lookbehind", "word boundary")
    },
}


def is_object_root(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "object"


USES = ("response_format", "tool", "auto_tool")
"""How a case's schema is asked for: as the response format; as the input schema of a strict tool
the model must call; or of one it may call (`auto_tool`, Gemini only: the one way Gemini enforces
a tool's schema, `VALIDATED`, applies only when the call is not required)."""


def runs(provider: str, case: Case, use: str) -> bool:
    """Whether the live test sends `case` to `provider` as `use`."""
    if use != "response_format" and not is_object_root(case.schema):
        return False  # a tool's input schema is an object
    return use != "auto_tool" or provider == "gemini"


PROMPT = (
    "Give one example value of the requested shape, about a fictional book or its author. Keep "
    "it short."
)


def request_for(case: Case, use: str) -> ModelRequest:
    """A request asking for `case`'s schema strictly, as `use` (`USES`), the same live and
    offline so that `wire_digest` matches."""
    schema: JSONObject = case.schema
    base = ModelRequest(
        context=(UserMessage(content=(ContentText(text=PROMPT),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=2048),
    )
    if use == "response_format":
        return replace(
            base,
            response_format=ResponseFormatJsonSchema(schema=schema, strict=True, name="example"),
        )
    tool = ToolParam(
        name="record_example",
        description="Record the example value.",
        input_schema=schema,
        strict=True,
        input_examples=None,
    )
    choice = ToolChoiceRequired() if use == "tool" else ToolChoiceAuto()
    return replace(base, tools=(tool,), tool_choice=choice)


def wire_digest(codec: ModelSideCodec, request: ModelRequest) -> str:
    """A digest of the body `codec` sends for `request` after adapting it: what a provider was
    shown, so `accepted()` can say whether it has seen and taken this exact form."""
    adapted = codec.adapt_request("m", request).request
    body = codec.encode_request("m", adapted, streaming=False).body
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


ACCEPTED_PATH = Path(__file__).with_name("schema_corpus_accepted.jsonl")
"""The wire forms the providers took in the last live run, one JSON line per sent case
(`{"key": "provider|case|use", "digest": ...}`). To refresh after changing adaptation: delete
it, run the live test with `BEHAVIORS_RECORD_ACCEPTED_SCHEMAS` set to its path, then
`sort -o` it."""


def accepted() -> dict[str, str]:
    if not ACCEPTED_PATH.exists():
        return {}
    lines = ACCEPTED_PATH.read_text().splitlines()
    return {entry["key"]: entry["digest"] for entry in map(json.loads, filter(None, lines))}


def key(provider: str, case: Case, use: str) -> str:
    return f"{provider}|{case.name}|{use}"


# What the providers did with the corpus (2026-11-10), by (case, use): `rejected` with a 400 when
# sent strict and unadapted; `rejected unenforced`, also when sent without strict mode (so only
# changing the schema avoids it); `answer broke the schema`, for Gemini, which ignored it.
_OPENAI_REJECTED = {
    **{
        (name, use): "rejected"
        for name in (
            "$ref into properties",
            "$ref to a nested definition",
            "$ref with sibling description",
            "array without items",
            "closed object without properties",
            "const object",
            "const string",
            "external $ref",
            "no type (any value)",
            "other formats",
            "properties without type",
            "unknown format",
        )
        for use in ("response_format", "tool")
    },
    **{
        (name, use): "rejected unenforced"
        for name in ("array enum", "object enum", "pattern with unicode class")
        for use in ("response_format", "tool")
    },
    **{
        (name, "response_format"): "rejected unenforced"
        for name in ("anyOf root", "array root", "nullable root", "string root")
    },
}
SWEEP: dict[str, dict[tuple[str, str], str]] = {
    "anthropic": {
        **{
            (name, use): "rejected"
            for name in (
                "$ref into properties",
                "$ref to a nested definition",
                "boolean subschema true",
                "const object",
                "no type (any value)",
                "pattern with unicode class",
                "properties without type",
                "recursive $ref",
            )
            for use in ("response_format", "tool")
        },
        **{
            (name, "response_format"): "rejected"
            for name in (
                "$anchor",
                "$id and relative $ref",
                "allOf with a local $ref",
                "array enum",
                "external $ref",
                "object enum",
            )
        },
    },
    "openai_responses": _OPENAI_REJECTED,
    "openai_chat_completions": _OPENAI_REJECTED,
    "gemini": {
        **{
            (name, use): "rejected unenforced"
            for name in ("$id and relative $ref", "external $ref")
            for use in ("response_format", "tool")
        },
        **{
            (name, "response_format"): "answer broke the schema"
            for name in ("array enum", "mixed scalar enum", "object enum")
        },
        ("closed object without properties", "auto_tool"): "answer broke the schema",
    },
}
