"""Which JSON Schema keywords each provider enforces when it constrains generation to a schema.

A provider that constrains decoding (structured outputs, strict tools) does so over a subset of
JSON Schema. A keyword outside the subset is a gap in enforcement whether the provider rejects
it or ignores it: Gemini, for one, drops what it does not support without an error. The tables
here say what each target enforces, so that `adaptation.adapt_request` can record what a
translated contract loses; the codecs themselves send schemas as given.

Support depends on values and combinations as well as on keywords, so each target's rules are
code with a keyword table inside. Each is read from the provider's documentation, on the date
given, and `tests/test_schema_support.py` holds cases taken from the same pages:

- Anthropic structured outputs and strict tools, which share one grammar
  (https://platform.claude.com/docs/en/build-with-claude/structured-outputs, 2026-11-09): no
  numeric or string-length constraints, `minItems` of 0 or 1 only, a fixed set of string
  `format`s, `additionalProperties: false` on every object, `enum` of strings, numbers, booleans
  and nulls only, `$ref` within the schema only, `allOf` but not with `$ref`, no recursive
  schemas, and `pattern` without backreferences, lookaround or word boundaries. An unsupported
  feature is a 400.
  `anthropic_transform` fits a schema to that subset the way the Anthropic SDK's
  `transform_schema` does.
- Gemini structured output (https://aistudio.google.com/docs/structured-output, 2026-11-09):
  the listed keywords are enforced; `minLength`, `maxLength`, `minProperties`, `maxProperties`,
  `uniqueItems` and `contains` are stripped and ignored. `pattern` is not documented, so it is
  treated as not enforced.
- OpenAI strict mode (https://developers.openai.com/api/docs/guides/structured-outputs,
  2026-11-09): the listed keywords; an object root that is not an `anyOf`; on every object
  `additionalProperties: false` with every property listed in `required`
  (`openai_strict_violations`). A schema that breaks those rules is rejected.

The documentation leaves rules out, so these also hold what the providers did with the corpus in
`tests/test_schema_acceptance_llm_integration.py` (run live 2026-11-10):

- Every provider resolves a `$ref` only within the schema: Anthropic and OpenAI only to the root
  (`#`) or a top-level `$defs`/`definitions` entry, with no sibling keywords on OpenAI; Gemini
  rejects one that does not start with `#`. Anthropic and OpenAI reject a Unicode property class
  (`\\p{L}`) in a `pattern`.
- Anthropic rejects a node that says nothing of its value's type (no `type`, `enum`, `const`,
  `anyOf`, `allOf` or `$ref`: `{}`, a node with only `properties`), a boolean subschema under
  `properties`, a recursive reference, and a `const` object or array (`anthropic_rejections`).
- OpenAI strict mode also needs a `type` on every node (an `enum`, `anyOf` or `$ref` node
  excepted), `items` on every array, properties on every object below the root, and a string
  `format` from its documented list; it rejects an `enum` of objects or arrays.
- OpenAI strict mode takes, and enforces, `minLength`, `maxLength` and a `$ref` into
  `definitions`, which its documentation leaves out of the subset (it lists the lengths as
  unsupported only for fine-tuned models, which are not modelled here). It takes the
  annotations `contentEncoding`, `contentMediaType`, `default`, `deprecated`, `readOnly` and
  `writeOnly`. It rejects `uniqueItems`, `minProperties`, `maxProperties`, `dependentRequired`,
  `propertyNames`, `contains`, `unevaluatedProperties`, `oneOf`, `allOf` and `if` ("not
  permitted"). Checked on gpt-4o-mini and gpt-6-astra (2026-11-16), each sent strict and
  unadapted, and with prompts pushing against the lengths and the reference.
- Gemini enforces `enum` only of strings. Validating tool calls (`VALIDATED`), it let a call
  through with keys on a closed object that lists no `properties`
  (`gemini_unvalidated_keywords`).

Not checked: the providers' size limits (nesting depth, property and enum counts, Anthropic's
"compiled grammar is too large"), which a schema rarely reaches.

Annotations (`description`, `title`, `$comment`, `examples`) carry no constraint and are
accepted everywhere.
"""

import copy
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, cast

from behaviors.types.json import JSONObject

SchemaTarget = Literal["anthropic", "gemini", "openai_strict"]

_ANNOTATIONS = frozenset({"description", "title", "$comment", "examples"})

_SUPPORTED: dict[SchemaTarget, frozenset[str]] = {
    "anthropic": frozenset(
        {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "const",
            "anyOf",
            "allOf",
            "$ref",
            "$defs",
            "definitions",
            "default",
            "format",
            "minItems",
            "pattern",
        }
    ),
    "gemini": frozenset(
        {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "enum",
            "format",
            "minimum",
            "maximum",
            "items",
            "prefixItems",
            "minItems",
            "maxItems",
            "anyOf",
            "$ref",
            "$defs",
            "propertyOrdering",
        }
    ),
    "openai_strict": frozenset(
        {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "const",
            "anyOf",
            "$ref",
            "$defs",
            "pattern",
            "format",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "definitions",
            "contentEncoding",
            "contentMediaType",
            "default",
            "deprecated",
            "readOnly",
            "writeOnly",
        }
    ),
}

_ANTHROPIC_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)

# Keywords whose value is one subschema, a list of them, or a map of them.
_ONE_SUBSCHEMA = ("items", "additionalProperties", "not", "contains", "if", "then", "else")
_SUBSCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")


def _subschemas(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for key in _ONE_SUBSCHEMA:
        value = node.get(key)
        if isinstance(value, dict):
            yield cast("dict[str, Any]", value)
        elif key == "items" and isinstance(value, list):
            yield from (v for v in cast("list[Any]", value) if isinstance(v, dict))
    for key in _SUBSCHEMA_LISTS:
        value = node.get(key)
        if isinstance(value, list):
            yield from (v for v in cast("list[Any]", value) if isinstance(v, dict))
    for key in _SUBSCHEMA_MAPS:
        value = node.get(key)
        if isinstance(value, dict):
            yield from (v for v in cast("dict[str, Any]", value).values() if isinstance(v, dict))


def _nodes(schema: JSONObject) -> Iterator[dict[str, Any]]:
    stack: list[dict[str, Any]] = [cast("dict[str, Any]", schema)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(_subschemas(node))


def _types(node: dict[str, Any]) -> list[Any]:
    kind = node.get("type")
    return cast("list[Any]", kind) if isinstance(kind, list) else [kind]


def _is_array(node: dict[str, Any]) -> bool:
    return "array" in _types(node)


def _is_string(node: dict[str, Any]) -> bool:
    return "string" in _types(node)


def is_plain_object_root(schema: JSONObject) -> bool:
    """Whether the root of `schema` is an object and only an object (`type: "object"`), the one
    root OpenAI takes for a response format, strict or not."""
    return schema.get("type") == "object"


def _is_object(node: dict[str, Any]) -> bool:
    """Whether `node` admits an object: `type` is or includes `object` (a nullable object,
    `["object", "null"]`, counts), or it lists properties."""
    kind = node.get("type")
    return (
        kind == "object"
        or (isinstance(kind, list) and "object" in cast("list[Any]", kind))
        or "properties" in node
    )


# Regex features Anthropic's grammar does not support in `pattern`: backreferences, lookahead
# and lookbehind, word boundaries, Unicode property classes.
_ANTHROPIC_UNSUPPORTED_REGEX = re.compile(r"\\[1-9]|\(\?<?[=!]|\\[bB]|\\[pP]\{")
# Unicode property classes, which OpenAI's strict mode does not take as a regex either.
_UNICODE_PROPERTY_CLASS = re.compile(r"\\[pP]\{")
# A reference Anthropic and OpenAI resolve: the root, or a definition at the top level.
_TOP_LEVEL_REF = re.compile(r"^#(/(\$defs|definitions)/[^/]+)?$")
# Strings formats OpenAI's strict mode takes.
_OPENAI_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
)
# Keywords OpenAI's strict mode rejects that constrain only the value of their own node and hold
# no subschema.
_OPENAI_STRICT_MOVABLE = frozenset(
    {"uniqueItems", "minProperties", "maxProperties", "dependentRequired"}
)


def _is_complex(value: Any) -> bool:
    return isinstance(value, dict | list)


def _complex_enum(value: Any) -> bool:
    return not isinstance(value, list) or any(
        _is_complex(member) for member in cast("list[Any]", value)
    )


def _top_level_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOP_LEVEL_REF.match(value))


def _has_type(node: dict[str, Any], *, by: frozenset[str]) -> bool:
    return bool(by & node.keys())


def _refs(schema: JSONObject) -> Iterator[str]:
    for node in _nodes(schema):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref


def _recursive(schema: JSONObject) -> bool:
    """Whether a reference in `schema` leads back to itself: to the root (`#`), or around a
    cycle of top-level definitions."""
    root = cast("dict[str, Any]", schema)
    definitions: dict[str, JSONObject] = {}
    for container in ("$defs", "definitions"):
        found = root.get(container)
        if isinstance(found, dict):
            for name, definition in cast("dict[str, Any]", found).items():
                if isinstance(definition, dict):
                    definitions[f"#/{container}/{name}"] = cast("JSONObject", definition)
    if "#" in _refs(schema):
        return True
    edges = {ref: [r for r in _refs(d) if r in definitions] for ref, d in definitions.items()}
    visiting: set[str] = set()
    done: set[str] = set()

    def cycles(ref: str) -> bool:
        if ref in done:
            return False
        if ref in visiting:
            return True
        visiting.add(ref)
        if any(cycles(target) for target in edges[ref]):
            return True
        visiting.discard(ref)
        done.add(ref)
        return False

    return any(cycles(ref) for ref in definitions)


def _anthropic_unsupported(node: dict[str, Any], key: str) -> bool:
    """Whether a keyword the Anthropic table lists is still outside the subset for its value."""
    value = node.get(key)
    if key == "additionalProperties":
        return value is not False
    if key == "minItems":
        return value not in (0, 1)
    if key == "format":
        return node.get("type") == "string" and value not in _ANTHROPIC_STRING_FORMATS
    if key == "pattern":
        return not isinstance(value, str) or bool(_ANTHROPIC_UNSUPPORTED_REGEX.search(value))
    if key == "allOf":
        return isinstance(value, list) and any(
            isinstance(sub, dict) and "$ref" in sub for sub in cast("list[Any]", value)
        )
    if key == "$ref":
        # To the root or a top-level definition only; anything else is a 400.
        return not _top_level_ref(value)
    if key == "enum":
        return _complex_enum(value)
    if key == "const":
        return _is_complex(value)
    return False


# What says enough of a node's value for Anthropic's grammar, and for OpenAI's strict mode.
_ANTHROPIC_TYPED_BY = frozenset({"type", "enum", "const", "anyOf", "allOf", "$ref"})
_OPENAI_TYPED_BY = frozenset({"type", "enum", "anyOf", "$ref"})


def anthropic_rejections(schema: JSONObject) -> tuple[str, ...]:
    """Why Anthropic would reject `schema` outright, whatever is moved out of it: a node that
    says nothing of its value's type (`{}`, or one emptied by moving its keywords), a boolean
    subschema under `properties`, or a recursive reference. () when it would take it."""
    problems: list[str] = []
    nodes = list(_nodes(schema))
    if any(not _has_type(node, by=_ANTHROPIC_TYPED_BY) for node in nodes):
        problems.append("a node without a type")
    if any(
        not isinstance(sub, dict)
        for node in nodes
        if isinstance(node.get("properties"), dict)
        for sub in cast("dict[str, Any]", node["properties"]).values()
    ):
        problems.append("a boolean subschema")
    if _recursive(schema):
        problems.append("a recursive reference")
    return tuple(problems)


_DEFAULT_DRAFT = "json-schema.org/draft/2020-12/schema"


def declares_default_draft(node: dict[str, Any], key: str) -> bool:
    """Whether `key` is a `$schema` naming draft 2020-12, the vocabulary's default: a
    declaration of what the schema already is, not a constraint."""
    value = node.get(key)
    return (
        key == "$schema"
        and isinstance(value, str)
        and value.split("://", 1)[-1].rstrip("#") == _DEFAULT_DRAFT
    )


def _gemini_unenforced_enum(value: Any) -> bool:
    """Gemini enforces an `enum` of strings only."""
    return not isinstance(value, list) or not all(
        isinstance(member, str) for member in cast("list[Any]", value)
    )


def unsupported_keywords(schema: JSONObject, target: SchemaTarget) -> tuple[str, ...]:
    """The keywords in `schema` that `target` does not enforce, sorted, each once. A `$schema`
    naming the default draft is not one."""
    supported = _SUPPORTED[target] | _ANNOTATIONS
    found: set[str] = set()
    for node in _nodes(schema):
        for key in node:
            if declares_default_draft(node, key):
                continue
            if key not in supported or (
                target == "anthropic" and _anthropic_unsupported(node, key)
            ):
                found.add(key)
            elif target == "gemini" and key == "enum" and _gemini_unenforced_enum(node[key]):
                found.add(key)
    if target == "anthropic":
        # An object without `additionalProperties` allows extra keys, which Anthropic cannot
        # express; the keyword's absence is the gap.
        if any(_is_object(node) and "additionalProperties" not in node for node in _nodes(schema)):
            found.add("additionalProperties")
    return tuple(sorted(found))


def openai_strict_violations(schema: JSONObject) -> tuple[str, ...]:
    """Why OpenAI's strict mode would reject `schema`, or () when it would accept it."""
    problems: list[str] = []
    if not is_plain_object_root(schema):
        problems.append("the root is not an object")
    elif "anyOf" in schema:
        problems.append("the root is an anyOf")
    nodes = list(_nodes(schema))
    if any(not _has_type(node, by=_OPENAI_TYPED_BY) for node in nodes):
        problems.append("a node without a type")
    if any(_is_array(node) and "items" not in node for node in nodes):
        problems.append("an array without items")
    if any(
        node is not schema and _is_object(node) and not node.get("properties") for node in nodes
    ):
        problems.append("an object without properties below the root")
    if any("$ref" in node and not _top_level_ref(node["$ref"]) for node in nodes):
        problems.append("a reference to something other than a top-level definition")
    if any("$ref" in node and len(node) > 1 for node in nodes):
        problems.append("a reference with sibling keywords")
    if any(
        _is_string(node) and "format" in node and node["format"] not in _OPENAI_STRING_FORMATS
        for node in nodes
    ):
        problems.append("a string format outside the supported list")
    if any("enum" in node and _complex_enum(node["enum"]) for node in nodes):
        problems.append("an enum of objects or arrays")
    if any(
        isinstance(node.get("pattern"), str)
        and _UNICODE_PROPERTY_CLASS.search(node["pattern"]) is not None
        for node in nodes
    ):
        problems.append("a pattern with a Unicode property class")
    unsupported = unsupported_keywords(schema, "openai_strict")
    if unsupported:
        problems.append(f"unsupported keywords: {', '.join(unsupported)}")
    for node in _nodes(schema):
        if not _is_object(node):
            continue
        if node.get("additionalProperties") is not False:
            problems.append("an object without additionalProperties: false")
            break
    for node in _nodes(schema):
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = node.get("required")
            listed = set(cast("list[Any]", required)) if isinstance(required, list) else set()
            if set(cast("dict[str, Any]", properties)) - listed:
                problems.append("an object with optional properties")
                break
    return tuple(problems)


@dataclass(frozen=True)
class TransformedSchema:
    """A schema fitted to a provider's subset, and what fitting it changed.

    Attributes:
        schema: The fitted schema.
        moved: Keywords taken out of the schema and written into the description of the node
            that had them, where the model reads them but the provider does not enforce them.
        strengthened: Whether an object that allowed keys beyond its properties now forbids
            them.
    """

    schema: JSONObject
    moved: tuple[str, ...]
    strengthened: bool


def _fit_subschemas(key: str, value: Any, fit: Callable[[dict[str, Any]], dict[str, Any]]) -> Any:
    """`value` under `key` with `fit` applied to each subschema it holds, and only to those: a
    `properties` map's keys are property names and an annotation's value is data, whatever
    they look like."""
    if key in _ONE_SUBSCHEMA and isinstance(value, dict):
        return fit(cast("dict[str, Any]", value))
    if key in _SUBSCHEMA_LISTS + ("items",) and isinstance(value, list):
        return [
            fit(cast("dict[str, Any]", sub)) if isinstance(sub, dict) else sub
            for sub in cast("list[Any]", value)
        ]
    if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
        return {
            name: fit(cast("dict[str, Any]", sub)) if isinstance(sub, dict) else sub
            for name, sub in cast("dict[str, Any]", value).items()
        }
    return value


def move_keywords(
    schema: JSONObject, rejects: Callable[[dict[str, Any], str], bool]
) -> TransformedSchema:
    """`schema` with each keyword `rejects` names (given its node and key) moved into its node's
    description, for a provider that rejects the request over it whether or not it enforces the
    schema."""
    moved: set[str] = set()

    def fit(node: dict[str, Any]) -> dict[str, Any]:
        rest = {key: value for key, value in node.items() if rejects(node, key)}
        out = {key: _fit_subschemas(key, value, fit) for key, value in node.items()}
        for key in rest:
            del out[key]
        if rest:
            moved.update(rest)
            note = "{" + ", ".join(f"{k}: {json.dumps(v)}" for k, v in rest.items()) + "}"
            description = out.get("description")
            out["description"] = f"{description}\n\n{note}" if description else note
        return out

    fitted = cast("JSONObject", fit(cast("dict[str, Any]", schema)))
    return TransformedSchema(schema=fitted, moved=tuple(sorted(moved)), strengthened=False)


def gemini_unvalidated_keywords(schema: JSONObject) -> tuple[str, ...]:
    """Keywords in `schema` that Gemini does not hold a validated tool call to, beyond those it
    does not enforce at all: `additionalProperties: false` on an object that lists no
    `properties`."""
    closed_empty = any(
        _is_object(node)
        and node.get("additionalProperties") is False
        and not node.get("properties")
        for node in _nodes(schema)
    )
    return ("additionalProperties",) if closed_empty else ()


def gemini_rejects(node: dict[str, Any], key: str) -> bool:
    """A keyword Gemini rejects the request over: a `$ref` outside the schema, which it cannot
    resolve."""
    value = node.get(key)
    return key == "$ref" and not (isinstance(value, str) and value.startswith("#"))


def openai_rejects(node: dict[str, Any], key: str) -> bool:
    """A keyword OpenAI rejects the request over, strict or not: an `enum` of objects or arrays,
    a `pattern` with a Unicode property class."""
    value = node.get(key)
    if key == "enum":
        return _complex_enum(value)
    if key == "pattern":
        return isinstance(value, str) and _UNICODE_PROPERTY_CLASS.search(value) is not None
    return False


def openai_strict_movable(node: dict[str, Any], key: str) -> bool:
    """A keyword OpenAI's strict mode rejects that constrains only the value of its own node and
    holds no subschema: a string `format` outside the supported list, or a keyword such as
    `uniqueItems` outside the strict subset. Moved into the node's description, it leaves what the
    rest of the schema allows unchanged."""
    if key == "format":
        return _is_string(node) and node[key] not in _OPENAI_STRING_FORMATS
    return key in _OPENAI_STRICT_MOVABLE


def anthropic_transform(schema: JSONObject) -> TransformedSchema:
    """`schema` fitted to Anthropic's structured-output subset, by the rules of the Anthropic
    SDK's `transform_schema`: every object gets `additionalProperties: false`, `oneOf` becomes
    `anyOf`, and each keyword outside the subset moves into the description of its node."""
    moved: set[str] = set()
    strengthened = [False]

    def fit(node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        rest: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
                out[key] = {
                    name: fit(cast("dict[str, Any]", sub)) if isinstance(sub, dict) else sub
                    for name, sub in cast("dict[str, Any]", value).items()
                }
            elif key == "allOf" and _anthropic_unsupported(node, key):
                rest[key] = value
            elif key in ("anyOf", "allOf", "oneOf") and isinstance(value, list):
                if key == "oneOf":
                    moved.add("oneOf")  # exclusivity has no Anthropic form
                out["anyOf" if key == "oneOf" else key] = [
                    fit(cast("dict[str, Any]", sub)) if isinstance(sub, dict) else sub
                    for sub in cast("list[Any]", value)
                ]
            elif key == "items" and isinstance(value, dict):
                out[key] = fit(cast("dict[str, Any]", value))
            elif key == "additionalProperties":
                continue  # set below for an object
            elif declares_default_draft(node, key):
                continue  # what the schema already is; Anthropic takes no `$schema`
            elif (key in _SUPPORTED["anthropic"] or key in _ANNOTATIONS) and not (
                _anthropic_unsupported(node, key)
            ):
                out[key] = copy.deepcopy(value)
            else:
                rest[key] = value
        if _is_object(node):
            if node.get("additionalProperties") is not False:
                strengthened[0] = True
            out["additionalProperties"] = False
        elif "additionalProperties" in node:
            rest["additionalProperties"] = node["additionalProperties"]
        if rest:
            moved.update(rest)
            note = "{" + ", ".join(f"{k}: {json.dumps(v)}" for k, v in rest.items()) + "}"
            description = out.get("description")
            out["description"] = f"{description}\n\n{note}" if description else note
        return out

    fitted = fit(cast("dict[str, Any]", schema))
    return TransformedSchema(
        schema=cast("JSONObject", fitted), moved=tuple(sorted(moved)), strengthened=strengthened[0]
    )
