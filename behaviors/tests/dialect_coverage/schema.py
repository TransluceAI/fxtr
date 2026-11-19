"""Provider request schemas as JSON Schema, from the pinned provider SDKs and API documents.

The Anthropic and OpenAI SDKs ship their request bodies as TypedDicts generated from each
provider's API definition; `typeddict_schema` converts one into a JSON Schema document (draft
2020-12, every TypedDict a `$defs` entry named by its qualified class name), so the coverage audit
and the property-based tests read every provider through one schema format. The SDK versions in
the lockfile are the pin: `schema_source` names them, so a change in a generated table can be
attributed to an SDK bump rather than a codec change.

Google's SDK builds the Gemini API's wire body from types of its own, so the Gemini schema comes
from the API's Discovery document instead (`discovery_schema`), read from a checked-in snapshot
of the schemas the request reaches (`GEMINI_SNAPSHOT`, pinned by the document's revision;
`refresh_gemini_snapshot` replaces it).
"""

import collections.abc
import importlib.metadata
import json
import re
import tomllib
import types
import typing
import urllib.request
from pathlib import Path
from typing import Any, cast

import typing_extensions

JSONSchema = dict[str, Any]

# Iterable spellings the SDKs use for array parameters.
_ARRAY_ORIGINS = (
    list,
    tuple,
    collections.abc.Iterable,
    collections.abc.Sequence,
    collections.abc.Collection,
)
_MAPPING_ORIGINS = (dict, collections.abc.Mapping)
_QUALIFIERS = (
    typing.Required,
    typing.NotRequired,
    typing_extensions.Required,
    typing_extensions.NotRequired,
    typing_extensions.ReadOnly,
)


_LOCKFILE = Path(__file__).parents[3] / "uv.lock"


def schema_source(distribution: str) -> str:
    """`<distribution> <version>`, for a table header."""
    return f"{distribution} {importlib.metadata.version(distribution)}"


def locked_version(distribution: str) -> str | None:
    """The version of `distribution` the workspace lockfile pins, or None without a lockfile."""
    if not _LOCKFILE.exists():
        return None
    lock = tomllib.loads(_LOCKFILE.read_text())
    for package in cast("list[dict[str, Any]]", lock.get("package", [])):
        if package.get("name") == distribution:
            return cast("str", package["version"])
    return None


def typeddict_schema(root: type) -> JSONSchema:
    """The JSON Schema of a TypedDict class and everything it references.

    Keys the SDK renames in the body (`PropertyInfo(alias=...)`) appear under their wire name.
    Which top-level keys the SDK sends as headers instead the TypedDict does not say:
    `wire.mark_header_parameters` finds them. A discriminated union
    (`PropertyInfo(discriminator=...)`) is a plain `anyOf`: each branch carries its own `const`
    discriminator.
    """
    converter = _Converter()
    ref = converter.convert(root)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **ref,
        "$defs": dict(sorted(converter.defs.items())),
    }


class _Converter:
    def __init__(self) -> None:
        self.defs: dict[str, JSONSchema] = {}

    def convert(self, hint: Any) -> JSONSchema:
        hint, _ = _unwrap(hint)
        if hint is Any or hint is object:
            return {}
        if hint is None or hint is type(None):
            return {"type": "null"}
        if hint is str:
            return {"type": "string"}
        if hint is bool:
            return {"type": "boolean"}
        if hint is int:
            return {"type": "integer"}
        if hint is float:
            return {"type": "number"}
        if hint is bytes:
            return {"type": "string", "contentEncoding": "base64"}
        if typing_extensions.is_typeddict(hint):
            return self._typeddict(cast("type", hint))
        origin = typing.get_origin(hint)
        args = typing.get_args(hint)
        if origin in (typing.Literal, typing_extensions.Literal):
            return _literal(args)
        if origin in (typing.Union, types.UnionType):
            # The SDKs also accept their response models and file handles in places; those are
            # Python-side conveniences with no JSON form of their own.
            branches = [self.convert(arg) for arg in args]
            supported = [b for b in branches if "x-unsupported" not in b]
            if not supported:
                return {"x-unsupported": repr(hint)}
            return supported[0] if len(supported) == 1 else {"anyOf": supported}
        if origin in _MAPPING_ORIGINS:
            value = self.convert(args[1]) if len(args) == 2 else {}
            return {"type": "object", "additionalProperties": value}
        if origin in _ARRAY_ORIGINS or _is_sequence_not_str(origin):
            item = self.convert(args[0]) if args else {}
            return {"type": "array", "items": item}
        if hint in (list, tuple):
            return {"type": "array"}
        if hint in (dict,):
            return {"type": "object"}
        # File uploads and the like: no JSON body form the codecs could be handed.
        return {"x-unsupported": repr(hint)}

    def _typeddict(self, cls: type) -> JSONSchema:
        name = f"{cls.__module__}.{cls.__qualname__}"
        ref = {"$ref": f"#/$defs/{name}"}
        if name in self.defs:
            return ref
        self.defs[name] = {}  # reserved, so a recursive reference resolves to the ref
        hints = typing_extensions.get_type_hints(cls, include_extras=True)
        total = cast("bool", getattr(cls, "__total__", True))
        properties: dict[str, JSONSchema] = {}
        required: list[str] = []
        for key, hint in hints.items():
            _, alias = _unwrap(hint)
            # An alias that is no body key names a header (`betas` as `anthropic-beta`); the
            # parameter keeps its own name, which is what the SDK call takes.
            wire = alias if alias is not None and alias.isidentifier() else key
            properties[wire] = self.convert(hint)
            if _is_required(hint, total):
                required.append(wire)
        definition: JSONSchema = {"type": "object", "properties": properties}
        if required:
            definition["required"] = required
        self.defs[name] = definition
        return ref


def _is_required(hint: Any, total: bool) -> bool:
    """Whether a TypedDict key is required. Read from the annotation itself: the SDK modules use
    postponed annotations, so the class's `__required_keys__` never saw `Required[...]`."""
    while typing.get_origin(hint) in (typing.Annotated, typing_extensions.Annotated):
        hint = typing.get_args(hint)[0]
    origin = typing.get_origin(hint)
    if origin in (typing.Required, typing_extensions.Required):
        return True
    if origin in (typing.NotRequired, typing_extensions.NotRequired):
        return False
    return total


def _unwrap(hint: Any) -> tuple[Any, str | None]:
    """`hint` without `Required`/`NotRequired`/`ReadOnly`/`Annotated`, and the wire alias an
    `Annotated` carried."""
    alias: str | None = None
    while True:
        origin = typing.get_origin(hint)
        if origin in (typing.Annotated, typing_extensions.Annotated):
            for meta in hint.__metadata__:
                alias = getattr(meta, "alias", None) or alias
            hint = typing.get_args(hint)[0]
            continue
        if origin is not None and origin in _QUALIFIERS:
            hint = typing.get_args(hint)[0]
            continue
        if isinstance(hint, (typing.TypeAliasType, typing_extensions.TypeAliasType)):
            hint = hint.__value__
            continue
        return hint, alias


def _literal(values: tuple[Any, ...]) -> JSONSchema:
    if len(values) == 1:
        return {"const": values[0]}
    return {"enum": list(values)}


def _is_sequence_not_str(origin: Any) -> bool:
    return getattr(origin, "__name__", None) == "SequenceNotStr"


# --- the Gemini API's Discovery document ---------------------------------------------------------

GEMINI_DISCOVERY_URL = "https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta"
GEMINI_SNAPSHOT = Path(__file__).parent / "gemini_v1beta_discovery.json"
GEMINI_REQUEST_ROOT = "GenerateContentRequest"

# Protobuf oneofs, which a Discovery document describes only in prose: a `Part` is one of these
# kinds of data, alongside the fields any part may carry (`thought`, `thoughtSignature`, ...).
_ONEOFS: dict[str, tuple[str, ...]] = {
    "Part": (
        "text",
        "inlineData",
        "fileData",
        "functionCall",
        "functionResponse",
        "executableCode",
        "codeExecutionResult",
        "toolCall",
        "toolResponse",
    ),
}
# Enumerations a Discovery document also gives only in prose ("Must be either 'user' or
# 'model'"): a content's role, which places its parts on one side of the conversation.
_PROSE_ENUMS: dict[tuple[str, str], tuple[str, ...]] = {("Content", "role"): ("user", "model")}

_REQUIRED = re.compile(r"^\s*Required\.")


def gemini_schema_source(snapshot: Path = GEMINI_SNAPSHOT) -> str:
    """`<document> <version> revision <revision>`, for a table header."""
    doc = json.loads(snapshot.read_text())
    return f"gemini discovery {doc['version']} revision {doc['revision']} {GEMINI_REQUEST_ROOT}"


def gemini_request_schema(snapshot: Path = GEMINI_SNAPSHOT) -> JSONSchema:
    """The generateContent request body's schema. `model` is the method's path parameter
    (`/v1beta/{model=models/*}:generateContent`), bound from the URL, not sent in the body."""
    schema = discovery_schema(json.loads(snapshot.read_text()), GEMINI_REQUEST_ROOT)
    root = cast("JSONSchema", schema["$defs"][GEMINI_REQUEST_ROOT])
    cast("dict[str, Any]", root["properties"]).pop("model", None)
    root["required"] = [
        key for key in cast("list[str]", root.get("required", [])) if key != "model"
    ]
    return schema


def refresh_gemini_snapshot(snapshot: Path = GEMINI_SNAPSHOT) -> str:
    """Fetch the Discovery document and save the schemas the request reaches; the revision."""
    with urllib.request.urlopen(GEMINI_DISCOVERY_URL, timeout=60) as response:
        doc = json.load(response)
    schemas = cast("dict[str, JSONSchema]", doc["schemas"])
    reachable: dict[str, JSONSchema] = {}
    pending = [GEMINI_REQUEST_ROOT]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable[name] = schemas[name]
        pending.extend(re.findall(r'"\$ref": "([^"]+)"', json.dumps(schemas[name])))
    trimmed = {
        "revision": doc["revision"],
        "version": doc["version"],
        "rootUrl": doc["rootUrl"],
        "schemas": dict(sorted(reachable.items())),
    }
    snapshot.write_text(json.dumps(trimmed, indent=1, sort_keys=True) + "\n")
    return cast("str", doc["revision"])


def discovery_schema(doc: JSONSchema, root: str) -> JSONSchema:
    """The JSON Schema of a Discovery document's `root` schema and everything it references.

    Each Discovery schema is a `$defs` entry under its id. Output-only fields (`readOnly`) are
    not request fields and are left out; a field whose description begins "Required." is
    required. Types follow the API's JSON mapping: `any` is any value, a 64-bit integer (a string
    in the document, as protobuf's JSON mapping writes one) is a JSON integer, which it reads
    too, and an enumeration leaves out its unset value (`UNSPECIFIED`, `*_UNSPECIFIED`: the same
    as not setting it). A oneof the document gives only in prose (`_ONEOFS`) is an `anyOf` of one
    schema per kind.
    """
    schemas = cast("dict[str, JSONSchema]", doc["schemas"])
    defs: dict[str, JSONSchema] = {}

    def convert(prop: JSONSchema) -> JSONSchema:
        if "$ref" in prop:
            return {"$ref": f"#/$defs/{prop['$ref']}"}
        kind = prop.get("type")
        if kind == "any":
            return {}
        if kind == "array":
            return {"type": "array", "items": convert(cast("JSONSchema", prop.get("items") or {}))}
        if kind == "object":
            extra = prop.get("additionalProperties")
            if isinstance(extra, dict):
                return {
                    "type": "object",
                    "additionalProperties": convert(cast("JSONSchema", extra)),
                }
            return {"type": "object"}
        if "enum" in prop:
            values = [
                value
                for value in cast("list[Any]", prop["enum"])
                if not (value == "UNSPECIFIED" or str(value).endswith("_UNSPECIFIED"))
            ]
            return {"enum": values} if len(values) > 1 else {"const": values[0]}
        fmt = prop.get("format")
        if kind == "string" and fmt in ("int64", "uint64"):
            return {"type": "integer"}
        if kind == "string" and fmt == "byte":
            return {"type": "string", "contentEncoding": "base64"}
        if kind in ("string", "integer", "number", "boolean"):
            return {"type": kind}
        return {}

    def definition(name: str, keys: collections.abc.Iterable[str] | None = None) -> JSONSchema:
        source = schemas[name]
        properties: dict[str, JSONSchema] = {}
        required: list[str] = []
        for key, prop in cast("dict[str, JSONSchema]", source.get("properties") or {}).items():
            if keys is not None and key not in keys:
                continue
            if prop.get("readOnly"):
                continue
            enum = _PROSE_ENUMS.get((name, key))
            properties[key] = {"enum": list(enum)} if enum is not None else convert(prop)
            if _REQUIRED.match(str(prop.get("description", ""))):
                required.append(key)
        out: JSONSchema = {"type": "object", "properties": properties}
        if required:
            out["required"] = required
        return out

    for name in schemas:
        kinds = _ONEOFS.get(name)
        if kinds is None:
            defs[name] = definition(name)
            continue
        all_keys = list(cast("dict[str, JSONSchema]", schemas[name].get("properties") or {}))
        common = [key for key in all_keys if key not in kinds]
        branches: list[JSONSchema] = []
        for kind in kinds:
            branch = definition(name, [kind, *common])
            branch["required"] = [kind, *cast("list[str]", branch.get("required", []))]
            defs[f"{name}.{kind}"] = branch
            branches.append({"$ref": f"#/$defs/{name}.{kind}"})
        defs[name] = {"anyOf": branches}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{root}",
        "$defs": dict(sorted(defs.items())),
    }
