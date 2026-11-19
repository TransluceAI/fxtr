"""The codec coverage audit: every path a provider's request schema allows, probed one at a time
through the codec's same-dialect reconstruction check, and classified.

`schema_paths` walks a request schema (JSON Schema, `schema.py`) into one path per property at
each position the schema allows it, a union's branches and a message's roles being distinct
positions. For each path, `probe` builds a minimal body that sets it (the *witness*: the
schema's required fields elsewhere, a value of the path's type at the path) and runs it through
`decode_request` → `encode_request` → `compare_request`.
It records two things separately:

* **fate** — what the codec does with the field: `modeled` (the vocabulary carries it), `extra`
  (left over in the request's `DialectExtras`: a same-dialect subject gets it, any other does
  not), `dropped` (absent from the re-encoding) or `altered` (re-encoded differently).
* **treatment** — how the reconstruction check compares it: `exact`, `normalized` (rewritten to
  a canonical spelling on both sides, still compared) or `ignored` (the check cannot see it).

and from them a **verdict**: `ok` (carried and compared), `known_loss` (a `LossLog` entry the
path causes, or dropped or altered with an ignore the check names), `unhandled` (the check reports a
difference no loss record explains, or one outside the probed path that the witness of the path
above it does not get) or `unprobed` (no witness could be built, the SDK sends the parameter as a
header, or the codec raised on it), each with its reason.
"""

import copy
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from behaviors.implementations.dialects.reconstruction import (
    Difference,
    compare_request,
    normalized_request,
    reference_extras,
)
from behaviors.interfaces.dialect_codecs import DialectCodec
from behaviors.types.dialect import Dialect, Headers
from behaviors.types.json import JSONObject

from .schema import JSONSchema

Fate = Literal["modeled", "extra", "dropped", "altered"]
Treatment = Literal["exact", "normalized", "ignored"]
Verdict = Literal["ok", "known_loss", "unhandled", "unprobed"]

# A property whose enum values select distinct positions, like a union's branches do: a field on
# an assistant message is a different path from the same field on a user message.
_FAN_OUT = ("role",)

_STRING = "probe"
_STRING_ALT = "probe-alt"


# --- the walk ------------------------------------------------------------------------------------

Step = tuple[Literal["prop", "item", "branch", "fix"], Any]


@dataclass(frozen=True)
class SchemaPath:
    """One path the schema allows.

    Attributes:
        label: The path as the table names it: `.name` for a property, `[]` for an array item,
            `<type=text>` (or `<string>`, `<null>`, ...) for a union branch, `<role=user>` for a
            fanned-out role.
        steps: How to reach it from the root, for the witness builder.
        unwalkable: Why no witness is attempted (a header-only SDK parameter, a recursive
            reference), or None.
    """

    label: str
    steps: tuple[Step, ...]
    unwalkable: str | None = None


def schema_paths(doc: JSONSchema) -> list[SchemaPath]:
    """Every path of the schema's root object, in walk order."""
    return list(_walk_node(doc, doc, (), "", frozenset()))


def _resolve(doc: JSONSchema, node: JSONSchema) -> tuple[JSONSchema, str | None]:
    name: str | None = None
    while "$ref" in node:
        name = cast("str", node["$ref"]).rsplit("/", 1)[-1]
        node = cast("JSONSchema", doc["$defs"][name])
    return node, name


def _walk_node(
    doc: JSONSchema, node: JSONSchema, steps: tuple[Step, ...], label: str, stack: frozenset[str]
) -> Iterator[SchemaPath]:
    """The paths strictly below `node` (the path to `node` itself is the caller's)."""
    node, name = _resolve(doc, node)
    if name is not None:
        if name in stack:
            yield SchemaPath(
                label + "…",
                steps,
                unwalkable=f"recursive reference to {_short(name)}, walked where it first appears",
            )
            return
        stack = stack | {name}
    if "anyOf" in node:
        branches = cast("list[JSONSchema]", node["anyOf"])
        nullable = len(branches) == 2 and any(_is_null(b) for b in branches)
        labels = _branch_labels(doc, branches)
        for index, branch in enumerate(branches):
            if nullable and not _is_null(branch):
                # `X | null`: X keeps the plain path, null is its own branch.
                yield from _walk_node(doc, branch, (*steps, ("branch", index)), label, stack)
                continue
            here = f"{label}<{labels[index]}>"
            yield SchemaPath(here, (*steps, ("branch", index)))
            yield from _walk_node(doc, branch, (*steps, ("branch", index)), here, stack)
        return
    if node.get("type") == "array" and "items" in node:
        items = cast("JSONSchema", node["items"])
        yield from _walk_node(doc, items, (*steps, ("item", None)), f"{label}[]", stack)
        return
    properties = cast("dict[str, JSONSchema]", node.get("properties") or {})
    for key in _FAN_OUT:
        values = _enum_values(doc, properties.get(key))
        if len(values) > 1:
            for value in values:
                here = f"{label}<{key}={value}>"
                fixed: tuple[Step, ...] = (*steps, ("fix", (key, value)))
                yield SchemaPath(here, fixed)
                rest = {k: v for k, v in properties.items() if k != key}
                yield from _walk_properties(doc, rest, fixed, here, stack)
            return
    yield from _walk_properties(doc, properties, steps, label, stack)


def _walk_properties(
    doc: JSONSchema,
    properties: Mapping[str, JSONSchema],
    steps: tuple[Step, ...],
    label: str,
    stack: frozenset[str],
) -> Iterator[SchemaPath]:
    for key, child in properties.items():
        here = f"{label}.{key}" if label else key
        child_steps: tuple[Step, ...] = (*steps, ("prop", key))
        header = child.get("x-header")
        if header is not None:
            yield SchemaPath(
                here, child_steps, unwalkable=f"SDK client parameter, sent as the {header} header"
            )
            continue
        resolved, _ = _resolve(doc, child)
        branches = cast("list[JSONSchema]", resolved.get("anyOf") or [])
        nullable = len(branches) == 2 and any(_is_null(b) for b in branches)
        if not branches or nullable:
            yield SchemaPath(here, child_steps)
        yield from _walk_node(doc, child, child_steps, here, stack)


def _is_null(node: JSONSchema) -> bool:
    return node.get("type") == "null"


def _enum_values(doc: JSONSchema, node: JSONSchema | None) -> list[Any]:
    if node is None:
        return []
    node, _ = _resolve(doc, node)
    if "enum" in node:
        return list(cast("list[Any]", node["enum"]))
    return []


def _branch_labels(doc: JSONSchema, branches: list[JSONSchema]) -> list[str]:
    """Each branch's label; branches that would share one (two object types with the same
    `type` constant) are told apart by their type names, else by position."""
    labels = [_branch_label(doc, branch) for branch in branches]
    shared = {label for label in labels if labels.count(label) > 1}
    for index, label in enumerate(labels):
        if label in shared:
            _, name = _resolve(doc, branches[index])
            labels[index] = f"{label}:{_short(name) if name is not None else index}"
    return labels


def _branch_label(doc: JSONSchema, branch: JSONSchema) -> str:
    node, name = _resolve(doc, branch)
    properties = cast("dict[str, JSONSchema]", node.get("properties") or {})
    for key in ("type", *properties):
        prop = properties.get(key)
        if prop is not None and "const" in prop:
            return f"{key}={prop['const']}"
    if name is not None:
        return _short(name)
    if "const" in node:
        return f"={json.dumps(node['const'])}"
    if "enum" in node:
        return "enum"
    if "anyOf" in node:
        return "union"
    return cast("str", node.get("type", "any"))


def _short(def_name: str) -> str:
    return def_name.rsplit(".", 1)[-1]


# --- witnesses -----------------------------------------------------------------------------------


class Unconstructible(Exception):
    """The schema offers no JSON value for a part of the path."""


@dataclass(frozen=True)
class Witness:
    """A request body that sets one path.

    Attributes:
        body: The body.
        pointer: The JSON pointer of the probed value in `body`.
        value: The probed value.
        alternate: The body with a different value of the same type at `pointer`, or None when
            the type has only one (a constant).
    """

    body: dict[str, Any]
    pointer: str
    value: Any
    alternate: dict[str, Any] | None


def build_witness(doc: JSONSchema, path: SchemaPath) -> Witness:
    body, pointer, node = _instantiate(doc, doc, path.steps, {})
    if not isinstance(body, dict):
        raise Unconstructible("the schema root is not an object")
    body = cast("dict[str, Any]", body)
    alternate_value = _alternate(doc, node)
    alternate: dict[str, Any] | None = None
    if alternate_value is not _NONE:
        alternate = copy.deepcopy(body)
        _set_pointer(alternate, pointer, alternate_value)
    return Witness(
        body=body, pointer=pointer, value=get_pointer(body, pointer), alternate=alternate
    )


def _instantiate(
    doc: JSONSchema, node: JSONSchema, steps: tuple[Step, ...], fixed: dict[str, Any]
) -> tuple[Any, str, JSONSchema]:
    """The value at `node` that sets the path `steps`, the pointer to the probed value inside
    it, and the schema of that value."""
    node, _ = _resolve(doc, node)
    if not steps:
        if fixed:
            return _minimal_object(doc, node, fixed), "", node
        return _sample(doc, node), "", node
    (kind, arg), rest = steps[0], steps[1:]
    if kind == "branch":
        branches = cast("list[JSONSchema]", node["anyOf"])
        return _instantiate(doc, branches[cast("int", arg)], rest, {})
    if kind == "fix":
        key, value = arg
        return _instantiate(doc, node, rest, {**fixed, key: value})
    if kind == "item":
        value, pointer, leaf = _instantiate(doc, cast("JSONSchema", node["items"]), rest, {})
        return [value], f"/0{pointer}", leaf
    obj = _minimal_object(doc, node, fixed)
    properties = cast("dict[str, JSONSchema]", node.get("properties") or {})
    if not rest:
        # The probed value is `fixed`'s, when this step fixed it.
        leaf_node = _resolve(doc, properties[arg])[0]
        obj[arg] = fixed[arg] if arg in fixed else _sample(doc, leaf_node)
        return obj, f"/{_escape(arg)}", leaf_node
    value, pointer, leaf = _instantiate(doc, properties[arg], rest, {})
    obj[arg] = value
    return obj, f"/{_escape(arg)}{pointer}", leaf


def _minimal_object(doc: JSONSchema, node: JSONSchema, fixed: dict[str, Any]) -> dict[str, Any]:
    properties = cast("dict[str, JSONSchema]", node.get("properties") or {})
    obj: dict[str, Any] = {}
    for key in cast("list[str]", node.get("required") or []):
        obj[key] = fixed[key] if key in fixed else _required_value(doc, properties[key], 0)
    for key, value in fixed.items():
        obj[key] = value
    return obj


_MAX_DEPTH = 12


def _minimal(doc: JSONSchema, node: JSONSchema, depth: int) -> Any:
    """The smallest value the schema allows: required fields only, empty arrays, first
    branches."""
    if depth > _MAX_DEPTH:
        raise Unconstructible("required fields nest without end")
    node, _ = _resolve(doc, node)
    if "x-unsupported" in node:
        raise Unconstructible(f"no JSON form for {node['x-unsupported']}")
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return cast("list[Any]", node["enum"])[0]
    if "anyOf" in node:
        branches = [b for b in cast("list[JSONSchema]", node["anyOf"]) if not _is_null(b)]
        return _minimal(doc, branches[0], depth + 1)
    kind = node.get("type")
    if kind == "object" or "properties" in node:
        properties = cast("dict[str, JSONSchema]", node.get("properties") or {})
        return {
            key: _required_value(doc, properties[key], depth + 1)
            for key in cast("list[str]", node.get("required") or [])
        }
    if kind == "array":
        return []
    return _scalar(kind)


def _required_value(doc: JSONSchema, node: JSONSchema, depth: int) -> Any:
    """A required field's minimal value; a required array holds one item, since the schema
    requiring it is a sign an empty one means nothing (a message with no content)."""
    resolved, _ = _resolve(doc, node)
    if resolved.get("type") == "array" and "items" in resolved:
        return [_minimal(doc, cast("JSONSchema", resolved["items"]), depth + 1)]
    return _minimal(doc, node, depth)


def _sample(doc: JSONSchema, node: JSONSchema) -> Any:
    """The probed value for a path: like `_minimal`, but an array holds one item and a free-form
    object one key, so the probe carries content."""
    node, _ = _resolve(doc, node)
    branches = cast("list[JSONSchema]", node.get("anyOf") or [])
    if len(branches) == 2 and any(_is_null(b) for b in branches):
        return _sample(doc, next(b for b in branches if not _is_null(b)))
    if node.get("type") == "array" and "items" in node:
        return [_minimal(doc, cast("JSONSchema", node["items"]), 1)]
    if (node.get("type") == "object" and not node.get("properties")) or _is_open(node):
        return {_STRING: _STRING}
    return _minimal(doc, node, 0)


def _is_open(node: JSONSchema) -> bool:
    """Whether the schema allows any value (an SDK `object`), which in practice is a free-form
    JSON object (a function's `parameters` schema)."""
    return not node.keys() - {"description", "title"}


def _scalar(kind: Any) -> Any:
    if kind == "string":
        return _STRING
    if kind == "integer":
        return 1
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return _STRING


_NONE = object()


def _alternate(doc: JSONSchema, node: JSONSchema) -> Any:
    """A second value of the probed value's type, or `_NONE` when there is none to offer."""
    node, _ = _resolve(doc, node)
    if "const" in node:
        return _NONE
    if "enum" in node:
        values = cast("list[Any]", node["enum"])
        return values[1] if len(values) > 1 else _NONE
    kind = node.get("type")
    if kind == "string":
        return _STRING_ALT
    if kind == "integer":
        return 2
    if kind == "number":
        return 0.25
    if kind == "boolean":
        return False
    if (kind == "object" and not node.get("properties")) or _is_open(node):
        return {_STRING_ALT: _STRING_ALT}
    return _NONE


# --- JSON pointers -------------------------------------------------------------------------------


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _tokens(pointer: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]


_MISSING = object()


def get_pointer(node: Any, pointer: str) -> Any:
    """The value at `pointer`, or `_MISSING`."""
    for token in _tokens(pointer):
        if isinstance(node, dict) and token in node:
            node = cast("dict[str, Any]", node)[token]
        elif (
            isinstance(node, list) and token.isdigit() and int(token) < len(cast("list[Any]", node))
        ):
            node = cast("list[Any]", node)[int(token)]
        else:
            return _MISSING
    return node


def _parent(node: Any, tokens: list[str]) -> Any:
    for token in tokens:
        node = (
            cast("list[Any]", node)[int(token)]
            if isinstance(node, list)
            else cast("dict[str, Any]", node)[token]
        )
    return node


def _set_pointer(node: Any, pointer: str, value: Any) -> None:
    *parents, last = _tokens(pointer)
    node = _parent(node, parents)
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def _delete_pointer(node: Any, pointer: str) -> None:
    *parents, last = _tokens(pointer)
    node = _parent(node, parents)
    if isinstance(node, list):
        del node[int(last)]
    else:
        del node[last]


# --- probing -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DialectAudit:
    """What the audit needs to know about one dialect.

    Attributes:
        dialect: The dialect.
        codec: Its codec (both halves: the passthrough check decodes with the harness side and
            re-encodes with the model side).
        target: The request target a harness would post to.
        schema: The request schema (`schema.py`).
        schema_source: Where the schema came from, pinned (`anthropic 0.83.0 <root type>`).
        sdk: The distribution the schema is read from (`anthropic`), or None for a schema read
            from a checked-in API document, which pins itself.
        adjust: Completes a witness where the schema alone cannot say how (a forced tool choice
            needs a tool to choose, a call's result needs the call before it), in place, given
            the probed pointer; returns the headers to send it with (a field that needs its
            feature flag) and the pointer, moved if the completion moved the probed value.
            None for no completion.
    """

    dialect: Dialect
    codec: DialectCodec
    target: str
    schema: JSONSchema
    schema_source: str
    sdk: str | None
    adjust: Callable[[dict[str, Any], str], tuple[Headers, str]] | None = None


@dataclass(frozen=True)
class _Run:
    differences: tuple[Difference, ...]
    dropped: frozenset[tuple[str, str]]
    extras: JSONObject
    encoded: JSONObject
    sent: str  # the body run, as canonical JSON
    # The pointer of the value this witness probes, or of the nearest path above whose witness
    # is this one (a required field's, set as its parent sets it): what its differences are of.
    scope: str = ""


@dataclass(frozen=True)
class Row:
    """One path's classification (the table's row), with the witness it was probed with."""

    path: str
    verdict: Verdict
    fate: Fate | None = None
    treatment: Treatment | None = None
    reasons: tuple[str, ...] = ()
    pointer: str | None = None
    witness: dict[str, Any] | None = field(default=None, compare=False)


def audit(spec: DialectAudit) -> list[Row]:
    """Every path of the dialect's request schema, probed and classified."""
    paths = schema_paths(spec.schema)
    labels = [path.label for path in paths]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(f"the schema walk names distinct paths alike: {duplicates[:5]}")
    runs: dict[str, _Run | None] = {}
    parents: dict[str, SchemaPath | None] = {}

    def run_of(path: SchemaPath | None) -> _Run | None:
        if path is None:
            return None
        if path.label not in runs:
            runs[path.label] = _witness_run(spec, path, run_of(parents.get(path.label)))
        return runs[path.label]

    rows: list[Row] = []
    for path, parent in _with_parents(paths):
        parents[path.label] = parent
        rows.append(probe(spec, path, run_of(parent)))
    return rows


def _with_parents(paths: list[SchemaPath]) -> Iterator[tuple[SchemaPath, SchemaPath | None]]:
    """Each path with the nearest path above it (the walk yields a path before those below)."""
    above: list[SchemaPath] = []
    for path in paths:
        while above and not _is_above(above[-1], path):
            above.pop()
        yield path, above[-1] if above else None
        above.append(path)


def _is_above(ancestor: SchemaPath, path: SchemaPath) -> bool:
    return (
        len(ancestor.steps) < len(path.steps)
        and path.steps[: len(ancestor.steps)] == ancestor.steps
    )


def _witness_run(spec: DialectAudit, path: SchemaPath, above: _Run | None) -> _Run | None:
    """The codec's run on the path's witness, or None when it has none to run. `above` is the
    run of the path just above it."""
    if path.unwalkable is not None:
        return None
    try:
        witness = build_witness(spec.schema, path)
        body = witness.body
        headers: Headers = ()
        pointer = witness.pointer
        if spec.adjust is not None:
            headers, pointer = spec.adjust(body, pointer)
        run = _run(spec, body, headers)
    except Exception:  # an unprobed parent: its children fall back to the bare root's baseline
        return None
    return replace(run, scope=_scope(run, pointer, above))


def _scope(run: _Run, pointer: str, above: _Run | None) -> str:
    return above.scope if above is not None and above.sent == run.sent else pointer


def probe(spec: DialectAudit, path: SchemaPath, parent: _Run | None = None) -> Row:
    """One path's row. `parent` is the run on the witness of the path just above it, so that a
    difference the path's surroundings cause (a namespace's member re-encoded with a field it was
    sent without) is reported once, on the path that brings them, not on every path below it."""
    if path.unwalkable is not None:
        return Row(path.label, "unprobed", reasons=(path.unwalkable,))
    try:
        witness = build_witness(spec.schema, path)
    except Unconstructible as exc:
        return Row(path.label, "unprobed", reasons=(f"no witness: {exc}",))
    body = witness.body
    headers: Headers = ()
    pointer = witness.pointer
    if spec.adjust is not None:
        headers, pointer = spec.adjust(body, pointer)
    try:
        run = _run(spec, body, headers)
    except Exception as exc:  # the codec's reaction is what is being recorded
        return Row(
            path.label,
            "unprobed",
            reasons=(f"codec raised {type(exc).__name__}: {exc}",),
            pointer=pointer,
            witness=body,
        )
    # A difference at the probed value is the path's. One elsewhere in the witness is the path's
    # only when neither the bare root body nor the parent's witness, which share everything in
    # this witness but the probed value, gets it too. Losses are attributed against the bare
    # root alone: a field inside a dropped block is lost with it, for the block's reason.
    empty = _empty_run(spec)
    scope = _scope(run, pointer, parent)
    touching = [d for d in run.differences if _touches(d.path, pointer) or _touches(d.path, scope)]
    known_elsewhere = {_key(d) for d in empty.differences}
    # A path whose witness is its parent's (a required field, set to the value the parent's
    # witness already gives it) adds nothing to subtract against: it shares the parent's row.
    if parent is not None and parent.sent != run.sent:
        known_elsewhere |= {_key(d) for d in parent.differences}
    baseline_body = copy.deepcopy(body)
    _delete_pointer(baseline_body, pointer)
    # Nor one the witness gets without the probed value (a null field's sibling block, dropped
    # whole, is reported where the normalization moves it, with or without the null).
    try:
        known_elsewhere |= {_key(d) for d in _run(spec, baseline_body, headers).differences}
    except Exception:  # a body the codec cannot take without the value: nothing to subtract
        pass
    elsewhere = [d for d in run.differences if d not in touching and _key(d) not in known_elsewhere]
    losses = sorted(run.dropped - empty.dropped)

    normalized, fired = normalized_request(spec.dialect, cast("JSONObject", body))
    normalized_baseline, fired_baseline = normalized_request(
        spec.dialect, cast("JSONObject", baseline_body)
    )
    as_absent = normalized == normalized_baseline
    alternate_alike: bool | None = None
    if witness.alternate is not None:
        alternate = copy.deepcopy(witness.alternate)
        if spec.adjust is not None:
            spec.adjust(alternate, witness.pointer)
        alternate_alike = (
            normalized_request(spec.dialect, cast("JSONObject", alternate))[0] == normalized
        )
    labels = [label for label in dict.fromkeys(fired) if label not in fired_baseline]
    # The API's default, spelled out: the check reads it as absent, and any other value as
    # itself, or a normalization names it as the default (a `type` constant, a null).
    default = (
        as_absent and alternate_alike is not True and (alternate_alike is False or bool(labels))
    )
    blind = not default and (as_absent or alternate_alike is True)
    if blind and not labels:
        # The normalization that hides the value acts on what encloses it (an ignored object,
        # emptied but still present without the value): it is that one's reason too.
        labels = _enclosing_labels(spec, body, pointer, fired)
    treatment: Treatment = "ignored" if blind else ("normalized" if labels or default else "exact")
    # Where the probed value is in the normalized body: what the check compares, and so where
    # a difference it brings is reported, once the normalization has moved it.
    lands = _changed_paths(normalized, normalized_baseline, "")

    # The check compares normalized bodies, so a value the normalization moves (a tool an
    # `additional_tools` item declares joins `tools`) differs where it lands, not at the pointer.
    own = [*touching, *elsewhere]
    fate: Fate
    if get_pointer(run.extras, pointer) is not _MISSING:
        fate = "extra"
    elif own:
        fate = "dropped" if all(d.kind == "missing_in_translated" for d in own) else "altered"
    elif treatment == "ignored":
        same = get_pointer(run.encoded, pointer)
        fate = "modeled" if same is not _MISSING and same == witness.value else "dropped"
    else:
        fate = "modeled"

    reasons = [f"{code}: {detail}" for code, detail in losses]
    reasons.extend(f"ignored: {label}" for label in labels if treatment == "ignored")
    reasons.extend(f"normalized: {label}" for label in labels if treatment == "normalized")
    if default and not labels:
        reasons.append("normalized: the default, read as absent")
    # A recorded loss explains a difference at the probed value, wherever the normalization put
    # it; not one anywhere else in the witness, which the loss is not about.
    unexplained = (
        [d for d in elsewhere if not any(_touches(d.path, at) for at in lands)] if losses else own
    )
    verdict: Verdict
    if unexplained:
        verdict = "unhandled"
        reasons.extend(_difference_reason(d) for d in unexplained[:4])
    elif own or losses or (fate == "dropped" and treatment == "ignored"):
        verdict = "known_loss"
        if not reasons:
            # A loss nothing names: neither a recorded loss nor a normalization that hides it.
            verdict = "unhandled"
            reasons.append("no recorded loss or ignored normalization accounts for it")
    else:
        verdict = "ok"
    return Row(
        path.label,
        verdict,
        fate=fate,
        treatment=treatment,
        reasons=tuple(reasons),
        pointer=pointer,
        witness=body,
    )


_EMPTY_RUNS: dict[int, _Run] = {}


def _empty_run(spec: DialectAudit) -> _Run:
    key = id(spec)
    if key not in _EMPTY_RUNS:
        _EMPTY_RUNS[key] = _run(spec, cast("dict[str, Any]", _minimal(spec.schema, spec.schema, 0)))
    return _EMPTY_RUNS[key]


def _run(spec: DialectAudit, body: dict[str, Any], headers: Headers = ()) -> _Run:
    codec = spec.codec
    decoded = codec.decode_request(spec.target, headers, json.dumps(body).encode())
    encoded = codec.encode_request(
        decoded.model or "probe", decoded.request, streaming=decoded.streaming
    )
    raw_headers, raw_query = reference_extras(spec.dialect, headers, spec.target)
    differences, _ = compare_request(
        spec.dialect,
        cast("JSONObject", body),
        encoded.body,
        raw_headers=raw_headers,
        raw_query=raw_query,
        encoded_headers=encoded.headers,
        encoded_query=encoded.query,
    )
    extras = decoded.request.extras.body if decoded.request.extras is not None else {}
    return _Run(
        differences=differences,
        dropped=frozenset((d.code, d.detail) for d in decoded.decode_losses),
        extras=extras,
        encoded=encoded.body,
        sent=json.dumps(body, sort_keys=True),
    )


def _enclosing_labels(
    spec: DialectAudit, body: dict[str, Any], pointer: str, fired: list[str]
) -> list[str]:
    """The labels that fire only for what encloses the value at `pointer`: those of the
    nearest enclosing value whose removal stops a label firing."""
    tokens = _tokens(pointer)
    for depth in range(len(tokens) - 1, 0, -1):
        enclosing = "".join("/" + _escape(token) for token in tokens[:depth])
        without = copy.deepcopy(body)
        _delete_pointer(without, enclosing)
        remaining = normalized_request(spec.dialect, cast("JSONObject", without))[1]
        labels = [label for label in dict.fromkeys(fired) if label not in remaining]
        if labels:
            return labels
    return []


def _changed_paths(left: Any, right: Any, at: str) -> list[str]:
    """The pointers at which two JSON values differ, as deep as both share a shape (an array
    whose length changes is reported whole)."""
    if left == right:
        return []
    if isinstance(left, dict) and isinstance(right, dict):
        left_object = cast("dict[str, Any]", left)
        right_object = cast("dict[str, Any]", right)
        return [
            path
            for key in dict.fromkeys([*left_object, *right_object])
            for path in _changed_paths(
                left_object.get(key, _MISSING),
                right_object.get(key, _MISSING),
                f"{at}/{_escape(key)}",
            )
        ]
    if isinstance(left, list) and isinstance(right, list):
        left_items, right_items = cast("list[Any]", left), cast("list[Any]", right)
        if len(left_items) == len(right_items):
            return [
                path
                for index, (a, b) in enumerate(zip(left_items, right_items, strict=True))
                for path in _changed_paths(a, b, f"{at}/{index}")
            ]
    return [at]


def _key(difference: Difference) -> str:
    return json.dumps(
        [difference.path, difference.kind, difference.harness, difference.translated],
        sort_keys=True,
        default=repr,
    )


def _touches(difference: str, pointer: str) -> bool:
    return (
        difference == pointer
        or difference.startswith(pointer + "/")
        or pointer.startswith(difference + "/")
    )


def _difference_reason(difference: Difference) -> str:
    def show(value: Any) -> str:
        text = json.dumps(value, sort_keys=True)
        return text if len(text) <= 80 else text[:77] + "..."

    if difference.kind == "missing_in_translated":
        return f"{difference.path}: missing from the re-encoding"
    if difference.kind == "extra_in_translated":
        return f"{difference.path}: added by the re-encoding ({show(difference.translated)})"
    return (
        f"{difference.path}: re-encoded as {show(difference.translated)}, "
        f"sent as {show(difference.harness)}"
    )
