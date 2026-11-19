"""Shared types for the dialect translation modules.

The dialect modules are pure functions between their wire dialect and the behaviors model
vocabulary (`behaviors.types`), wrapped by one codec object each
(`behaviors.interfaces.dialect_codecs`). A `ModelAPI` adapter calls a provider through a codec's
model side; a server translating for a harness would use its harness side. This module holds what
the dialect modules share.
"""

import copy
import json
import urllib.parse
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from behaviors.interfaces.dialect_codecs import CodecError
from behaviors.types.adaptation import RequestLoss
from behaviors.types.content import Content, ContentReasoning
from behaviors.types.dialect import Dialect, DialectExtras, Headers, QueryParams
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import AssistantMessage, ChatMessage
from behaviors.types.response import ModelRequest
from behaviors.types.tool import ToolParam
from fxtr.entity_defns.dynamic_data import DynamicData


def failure_message(failure: ModelCallFailure) -> str:
    """The message rendered into a dialect error body.

    The provider's HTTP status is appended for observability: the wire status
    the harness sees is the *dialect's* canonical status for the category, not
    the provider's (the provider may not even speak this dialect).
    """
    if failure.provider_http_status is None:
        return failure.description
    return f"{failure.description} (provider HTTP status {failure.provider_http_status})"


def normalized_path(target: str) -> str:
    """The path of a request target with the query string and any trailing slash removed."""
    path = target.split("?", 1)[0]
    return path.rstrip("/") or "/"


def path_matches(target: str, endpoint: str) -> bool:
    """Whether `target` is `endpoint`, a version-prefixed variant of it (`.../v1/messages`), or
    one of its sub-endpoints (`/v1/messages/count_tokens`)."""
    path = normalized_path(target)
    return path == endpoint or path.endswith(endpoint) or path.startswith(endpoint + "/")


def has_header(headers: Headers, name: str) -> bool:
    """Whether `headers` carries `name`, case-insensitively."""
    lowered = name.lower()
    return any(key.lower() == lowered for key, _ in headers)


def merge_provider_params(body: dict[str, Any], extra: Mapping[str, Any]) -> None:
    """Merge a request's verbatim provider parameters (`SamplingParams.extra`) over `body`.

    Values are deep-copied: the body must share nothing with caller-owned data, so that a nested
    default applied later cannot edit the caller's settings and editing the body cannot change a
    later request.
    """
    for key, value in extra.items():
        body[key] = copy.deepcopy(value)


def apply_declared_defaults(body: dict[str, Any], defaults: Mapping[str, Any]) -> None:
    """Fill `defaults` in where `body` leaves the key absent (never overriding a value)."""
    for key, value in defaults.items():
        if key not in body:
            body[key] = copy.deepcopy(value)


# --- extras: what a harness request carries that the vocabulary has no slot for ------------------


_REMOVED = object()


class BodyReader:
    """Reads a request body while tracking what was consumed, so the leftover is the extras body.

    A decoder consumes a top-level key in one of two ways. `take(key)` consumes it wholly: the
    vocabulary models the structure as a whole and rebuilds it from its own types (messages,
    tools, the scalar sampling fields), so nothing of it is leftover, not even material inside
    it the decoder did not understand. `take_leaf(key, *path)` consumes one leaf of a parameter
    object the vocabulary models partially (`thinking.type`), leaving its other leaves in place
    (`thinking.display`). `peek` reads without consuming, for a decoder that only consumes a
    value it could map. Whatever was never taken is leftover in its entirety; an object emptied
    by leaf consumption disappears, an object that was empty to begin with stays.
    """

    def __init__(self, body: Mapping[str, Any]) -> None:
        self._body = body
        # key -> True (wholly consumed) or a nested dict of consumed leaves.
        self._consumed: dict[str, Any] = {}

    def peek(self, key: str, *path: str) -> Any:
        """The value at `key` (then `path` inside it), or None when absent or not an object."""
        value: Any = self._body.get(key)
        for step in path:
            value = cast("dict[str, Any]", value).get(step) if isinstance(value, dict) else None
        return value

    def take(self, key: str) -> Any:
        """Consume a top-level key wholly and return its value (None when absent)."""
        self._consumed[key] = True
        return self._body.get(key)

    def take_leaf(self, key: str, *path: str) -> Any:
        """Consume one leaf and return its value (None when absent or not reachable)."""
        if not path:
            return self.take(key)
        node: Any = self._consumed.get(key)
        if node is True:
            return self.peek(key, *path)
        if node is None:
            node = self._consumed[key] = {}
        tree = cast("dict[str, Any]", node)
        for step in path[:-1]:
            child: Any = tree.get(step)
            if child is True:
                return self.peek(key, *path)
            if child is None:
                child = tree[step] = {}
            tree = cast("dict[str, Any]", child)
        tree[path[-1]] = True
        return self.peek(key, *path)

    def leftover(self) -> dict[str, Any]:
        """A deep copy of everything not consumed, in place."""
        out: dict[str, Any] = {}
        for key, value in self._body.items():
            remaining = _subtract(value, self._consumed.get(key))
            if remaining is not _REMOVED:
                out[key] = remaining
        return out


def _subtract(value: Any, consumed: Any) -> Any:
    if consumed is True:
        return _REMOVED
    if consumed is None or not isinstance(value, dict):
        # Nothing consumed here, or leaves were "consumed" from a value that is not an object
        # (the decoder could not have used them): keep the value whole.
        return copy.deepcopy(value)
    consumed_tree = cast("dict[str, Any]", consumed)
    source = cast("dict[str, Any]", value)
    out: dict[str, Any] = {}
    subtracted = False
    for key, child in source.items():
        remaining = _subtract(child, consumed_tree.get(key))
        if remaining is _REMOVED:
            subtracted = True
        else:
            out[key] = remaining
    if not out and subtracted:
        return _REMOVED
    return out


# Headers that belong to the transport (framing, content negotiation, hop-by-hop).
_TRANSPORT_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "accept",
        "accept-encoding",
        "te",
        "trailer",
        "upgrade",
        "expect",
        "proxy-connection",
    }
)
# Credentials and account selection: the subject's configuration supplies these. Proxy
# credentials belong to the hop the harness used to reach us and must never travel further.
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "proxy-authenticate",
        "x-api-key",
        "x-goog-api-key",
        "openai-organization",
        "openai-project",
        "x-goog-user-project",
    }
)
_CREDENTIAL_QUERY = frozenset({"key"})  # the Gemini API's `?key=` form
# Client identification: recorded on the exchange and forwarded on passthrough, but not extras
# and not compared (a deliberate deferral: no provider is known to key behavior on it).
_CLIENT_IDENTIFICATION_HEADERS = frozenset({"user-agent", "x-app", "x-goog-api-client"})
_CLIENT_IDENTIFICATION_PREFIXES = ("x-stainless-",)


def leftover_headers(headers: Headers, *, contract: Collection[str] = ()) -> Headers:
    """The request headers that are extras: feature flags (`anthropic-beta`, `openai-beta`) and
    any unknown provider header. Dropped: transport, credentials and account selection, client
    identification, and the dialect's own contract-selection headers (`contract`, e.g.
    `anthropic-version`, which the subject's client sets for itself)."""
    excluded = _TRANSPORT_HEADERS | _CREDENTIAL_HEADERS | _CLIENT_IDENTIFICATION_HEADERS
    excluded |= {name.lower() for name in contract}
    # A `Connection` header names further hop-by-hop headers of this connection (RFC 9110 §7.6.1);
    # they are the transport's too.
    for name, value in headers:
        if name.lower() == "connection":
            excluded |= {token.strip().lower() for token in value.split(",") if token.strip()}
    kept: list[tuple[str, str]] = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in excluded or lowered.startswith(_CLIENT_IDENTIFICATION_PREFIXES):
            continue
        kept.append((name, value))
    return tuple(kept)


def forwardable_headers(headers: Headers) -> Headers:
    """The request headers that travel upstream when a request is forwarded as sent: everything
    but the transport's (framing, negotiation, and what a `Connection` header names),
    credentials and account selection (the forwarding client adds its own), and
    `accept-encoding` (the forwarder asks for identity so the relayed body stays as it is).
    Client identification and the dialect's contract headers do travel."""
    excluded = _TRANSPORT_HEADERS | _CREDENTIAL_HEADERS
    for name, value in headers:
        if name.lower() == "connection":
            excluded |= {token.strip().lower() for token in value.split(",") if token.strip()}
    return tuple(
        (name, value)
        for name, value in headers
        if name.lower() not in excluded or name.lower() == "content-type"
    )


def forwardable_query(query: str) -> str:
    """A request's raw query without its credentials (the Gemini API's `?key=`): the forwarding
    client authenticates for itself. Every other pair stays as sent, in order."""
    if not query:
        return query
    kept = [
        pair
        for pair in query.split("&")
        if urllib.parse.unquote_plus(pair.partition("=")[0]) not in _CREDENTIAL_QUERY
    ]
    return "&".join(kept)


def split_origin_form_target(target: str) -> tuple[str, str]:
    """The (path, query) of a request target in origin form (`/path?query`).

    Raises `ValueError` for anything else: an absolute URL, a `//host/path` target, or a target
    without a leading slash. A proxied request's destination is the subject API's configured
    client, never something the request names.
    """
    parts = urllib.parse.urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        raise ValueError(f"request target must be a path with an optional query, not {target!r}")
    return parts.path, parts.query


def leftover_query(target: str, *, consumed: Collection[str] = ()) -> QueryParams:
    """The query parameters of a request target, minus credentials and the names the codec
    consumed (`consumed`, e.g. Gemini's `alt`, read for stream framing)."""
    _, _, query = target.partition("?")
    excluded = _CREDENTIAL_QUERY | {name for name in consumed}
    return tuple(
        (name, value)
        for name, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if name not in excluded
    )


def request_extras(
    dialect: Dialect,
    reader: BodyReader,
    headers: Headers,
    target: str,
    *,
    contract_headers: Collection[str] = (),
    consumed_query: Collection[str] = (),
) -> DialectExtras | None:
    """The extras of a decoded request: the reader's leftover body, the leftover headers and
    query; None when there is nothing.

    Extras are the leaf paths the decoder did not consume, so only what sits beside the keys it
    takes whole can become one. `messages`, `tools` and `input` are taken whole, and a field
    inside one of their items that the vocabulary has no slot for (a message's `phase`, a
    directive-only system message's `output_config`) is not an extra: on the translated path it
    is lost, and on passthrough the reconstruction check reports it.
    """
    body = reader.leftover()
    kept_headers = leftover_headers(headers, contract=contract_headers)
    query = leftover_query(target, consumed=consumed_query)
    if not body and not kept_headers and not query:
        return None
    return DialectExtras(
        dialect=dialect, headers=kept_headers, query=query, body=cast("JSONObject", body)
    )


def without_reasoning(request: ModelRequest, paths: Collection[tuple[str, ...]]) -> ModelRequest:
    """`request` with `sampling.reasoning` cleared and the extras at `paths` (each a key path
    into the extras body) removed; a parent object emptied by the removal goes too, and a bag
    left with nothing becomes None."""
    sampling = replace(request.sampling, reasoning=None)
    extras = request.extras
    if extras is None:
        return replace(request, sampling=sampling)
    body = copy.deepcopy(dict(extras.body))
    for path in paths:
        _remove_path(body, path)
    if not body and not extras.headers and not extras.query:
        return replace(request, sampling=sampling, extras=None)
    return replace(request, sampling=sampling, extras=replace(extras, body=body))


def _remove_path(node: dict[str, Any], path: tuple[str, ...]) -> None:
    key, rest = path[0], path[1:]
    if key not in node:
        return
    if not rest:
        del node[key]
        return
    child = node[key]
    if isinstance(child, dict):
        child = cast("dict[str, Any]", child)
        _remove_path(child, rest)
        if not child:
            del node[key]


def unadapted(what: str) -> CodecError:
    """The error an encoder raises for part of a request its dialect cannot carry: adapting the
    request (`ModelSideCodec.adapt_request`) changes or removes it, with a recorded loss, so an
    encoder that meets it was handed a request that was not adapted."""
    return CodecError(
        f"{what} reached the encoder: the dialect cannot carry it, and adapt_request changes or "
        "removes it (adapt the request before encoding it)"
    )


def apply_dialect_extras(
    body: dict[str, Any], extras: DialectExtras | None, dialect: Dialect
) -> tuple[Headers, QueryParams]:
    """Deep-merge a bag of `dialect` into the rendered `body` and return the headers and query
    it carries, to be sent with the request.

    A bag of another dialect is left out by adaptation (`adaptation.drop_foreign_extras`), so one
    reaching the encoder is a `CodecError`. A leaf the bag would overwrite raises `CodecError`,
    even for an equal value: consumed leaves were subtracted at decode, so a collision means the
    codec's decoder and encoder disagree about what the codec models.
    """
    if extras is None:
        return (), ()
    if extras.dialect != dialect:
        raise unadapted(f"{extras.dialect} extras")
    _merge_extras(body, extras.body, "")
    return extras.headers, extras.query


def _merge_extras(target: dict[str, Any], bag: Mapping[str, Any], path: str) -> None:
    for key, value in bag.items():
        here = f"{path}/{key}"
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        current = target[key]
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_extras(cast("dict[str, Any]", current), cast("Mapping[str, Any]", value), here)
            continue
        raise CodecError(
            f"dialect extras collide with the rendered request at {here}: the codec rendered "
            f"{current!r} and the request's extras carry {value!r}; the decoder must consume "
            "every leaf the encoder renders"
        )


# --- reasoning state a model cannot use ---------------------------------------------------------
#
def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def loads_strict_json(text: str | bytes) -> Any:
    """`text` parsed as JSON, rejecting the `NaN`, `Infinity` and `-Infinity` that `json.loads`
    accepts but JSON does not have: `ValueError` for those as for any other invalid text. For a
    check of what a model wrote against a schema, where a non-JSON number is not an answer."""
    return json.loads(text, parse_constant=_reject_constant)


# A model codec sends back only the reasoning state it recognizes as its own provider's, for the
# model it calls (`ModelSideCodec.accepts_reasoning_state`): a provider verifies what it issued,
# and rejects or misreads anything else. Each codec's adaptation removes a block carrying any other
# state (`adaptation.drop_foreign_reasoning`, so a direct model call and a request decoded from
# a harness alike), text and all: its text belongs with that state, and sent alone it would read
# to the provider as reasoning of its own, which some reject. Passthrough forwards a request as
# sent and is outside this rule.

ReasoningStateFilter = Callable[[DynamicData], bool]
"""Whether a destination accepts a reasoning state (a bound `accepts_reasoning_state`)."""


def same_openrouter_model(produced_by: str, requested: str) -> bool:
    """Whether the model OpenRouter reported producing some reasoning is the one a request names.
    A routing variant suffix (`:nitro`, `:floor`, ...) does not change the model; a response that
    reported no model is taken on trust."""
    if not produced_by or produced_by == requested:
        return True
    return produced_by.split(":")[0] == requested.split(":")[0]


def foreign_reasoning_state(
    request: ModelRequest, accepts: ReasoningStateFilter
) -> tuple[str, ...]:
    """The type tags of the reasoning states in `request`'s context that `accepts` rejects, one
    entry per affected block, in context order; empty when there are none."""
    return without_foreign_reasoning_state(request, accepts)[1]


def without_foreign_reasoning_state(
    request: ModelRequest, accepts: ReasoningStateFilter
) -> tuple[ModelRequest, tuple[str, ...]]:
    """`request` without the reasoning blocks whose state `accepts` rejects, and those states'
    type tags (see `foreign_reasoning_state`); the same request when there are none."""
    rejected: list[str] = []
    context: list[ChatMessage] = []
    for message in request.context:
        if isinstance(message, AssistantMessage):
            content: list[Content] = []
            changed = False
            for block in message.content:
                state = block.reasoning_state if isinstance(block, ContentReasoning) else None
                if state is not None and not accepts(state):
                    rejected.append(state.type)
                    changed = True
                    continue
                content.append(block)
            if changed:
                message = replace(message, content=tuple(content))
        context.append(message)
    if not rejected:
        return request, ()
    return replace(request, context=tuple(context)), tuple(rejected)


# --- what a decoder leaves out -------------------------------------------------------------------


class LossLog:
    """What a request decoder left out of the vocabulary, recorded at the point it was dropped.

    The decoders call `note` for a declaration they skip (a provider-side tool, a freeform tool)
    and `count` for each content part they skip (an image, a provider-side tool's result block),
    and `recorded` folds both into one `RequestLoss` per code, in first-seen order. Keeping
    the record next to the drop is what makes it match the decoder: there is no second pass
    guessing what the first one did.

    The codes decoders use: `provider_tool:<family>` for a provider-side tool,
    `provider_tool_exchange` for a provider-side tool's call and result blocks in the history,
    `builtin_tool` for a provider-defined tool the harness runs but the dialect declares without a
    schema, `custom_tool` for a freeform tool, `tool_search` for a provider's tool-search tool
    over deferred definitions, `image`, `audio`, `document`, `citations`,
    `programmatic_tool_calling` for tools callable from, and calls made by, code the provider
    runs, `async_tool_calling`, `tool_choice` for a choice the vocabulary has no form for (a
    hosted tool), `allowed_tools` for declared tools
    a choice leaves uncallable (left out of the request), `toolset`, `mid_conversation_effort`
    and `system_message_expiry` for a `clear_at` the decoder does not apply (Anthropic),
    `part_field:<field>` for a field of a Gemini content part beside its data (media settings,
    transcription, custom metadata), `structured_tool_result` for a Gemini function response
    whose content is an object rather than text, `schema_keyword` for a field of a Gemini
    OpenAPI schema with no JSON Schema form, `candidates` for a Gemini request for more than
    one candidate, `response_mime_type` for a Gemini response MIME type with no vocabulary form
    (`text/x.enum`), `system_role` for a Responses `system` item after the conversation began
    (rendered back as `developer`), `unsigned_reasoning` for a reasoning item no provider could
    verify, `unpaired_tool_output` for a tool output without a call id, and `block:<type>`,
    `item:<type>`, `part:<type>` for a content block, input item or content part of a type the
    decoder does not know or cannot place (a Gemini thought part outside a model turn). These
    are semantic: what the vocabulary cannot hold, the subject is not asked. The one delivery-only
    decode loss is `cache_breakpoint`, for a Responses breakpoint TTL that is not a string.
    """

    def __init__(self) -> None:
        self._notes: dict[str, list[str]] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._semantic: dict[str, bool] = {}

    def note(self, code: str, detail: str) -> None:
        self._notes.setdefault(code, []).append(detail)
        self._counts.setdefault(code, {})

    def count(self, code: str, noun: str, *, semantic: bool = True) -> None:
        self._notes.setdefault(code, [])
        self._semantic[code] = semantic
        nouns = self._counts.setdefault(code, {})
        nouns[noun] = nouns.get(noun, 0) + 1

    def recorded(self) -> tuple[RequestLoss, ...]:
        out: list[RequestLoss] = []
        for code, notes in self._notes.items():
            counted = [f"{n} {noun}" for noun, n in self._counts.get(code, {}).items()]
            out.append(
                RequestLoss(code, "; ".join([*notes, *counted]), self._semantic.get(code, True))
            )
        return tuple(out)


# --- tool names across dialects ------------------------------------------------------------------


def wire_tool_names(tools: Sequence[ToolParam]) -> dict[tuple[str | None, str], str]:
    """The name each tool goes by in a dialect without namespaces, keyed by (namespace, name).

    A grouped tool keeps its own name unless another declaration shares it; only then is the
    group prefixed (`group__name`), so a subject model sees `shell` rather than
    `functions__shell` in the common case and two colliding names stay distinct. The same map,
    inverted, recovers (namespace, name) from a subject's call (`ToolNames`).
    """
    counts: dict[str, int] = {}
    for tool in tools:
        counts[tool.name] = counts.get(tool.name, 0) + 1
    out: dict[tuple[str | None, str], str] = {}
    for tool in tools:
        wire = (
            f"{tool.namespace}__{tool.name}"
            if tool.namespace is not None and counts[tool.name] > 1
            else tool.name
        )
        out[(tool.namespace, tool.name)] = wire
    return out


def flattened_description(tool: ToolParam) -> str | None:
    """A tool's description for a dialect without namespaces. A grouped tool's group may carry
    instructions of its own (a Responses `namespace` description, which that dialect shows the
    model); with the group flattened away, they go ahead of the tool's own description so the
    subject still reads them."""
    if not tool.namespace or not tool.namespace_description:
        return tool.description
    group = f"Part of the `{tool.namespace}` tool group: {tool.namespace_description}"
    return f"{group}\n\n{tool.description}" if tool.description else group


@dataclass(frozen=True)
class ResolvedCall:
    """A tool call mapped to the request's declarations, as one identity.

    Attributes:
        tool: The declaration the call identifies, or None when nothing declares it.
        name: The declaration's own name (a flattened alias un-flattened), else the name as called.
        called_namespace: The namespace the call itself carried.
        how: How the call named its tool: `namespaced` (it carried its group), `alias` (a
            flattened `group__name` a dialect without namespaces used), `own` (the declaration's
            own name, unqualified), or `unknown` (nothing declares it).
    """

    tool: ToolParam | None
    name: str
    called_namespace: str | None
    how: Literal["namespaced", "alias", "own", "unknown"]

    @property
    def namespace(self) -> str | None:
        """The group that identifies the tool: as called, else the declaration's."""
        if self.called_namespace is not None:
            return self.called_namespace
        return self.tool.namespace if self.tool is not None else None


def _declared_as(tool: ToolParam) -> str:
    return f"{tool.namespace}.{tool.name}" if tool.namespace is not None else tool.name


class ToolNames:
    """One request's tool declarations, as every encoder consults them: the declaration a call
    names (by (namespace, name) when the call carries its group, else by the wire name a dialect
    without namespaces used, `wire_tool_names`), the declaration a forced tool choice names, and
    the wire names themselves.

    `flattened` is for a dialect without namespaces, which must be able to name every tool
    distinctly: two declarations sharing a wire name are a `CodecError` there, while a dialect
    with namespaces keeps them apart by group and never flattens.
    """

    def __init__(self, tools: Sequence[ToolParam] = (), *, flattened: bool = False) -> None:
        self._by_key: dict[tuple[str | None, str], ToolParam] = {
            (tool.namespace, tool.name): tool for tool in tools
        }
        self._wire = wire_tool_names(tools)
        by_wire: dict[str, list[ToolParam]] = {}
        for key, wire in self._wire.items():
            by_wire.setdefault(wire, []).append(self._by_key[key])
        if flattened:
            clashes = [
                f"{wire!r} for " + " and ".join(_declared_as(tool) for tool in group)
                for wire, group in by_wire.items()
                if len(group) > 1
            ]
            if clashes:
                raise CodecError(
                    "tool names collide once namespaces are flattened: " + "; ".join(clashes)
                )
        # A wire name two declarations share resolves to neither (a guess would be wrong half
        # the time); only a dialect without namespaces needs it, and that dialect refused above.
        self._by_wire: dict[str, ToolParam] = {
            wire: group[0] for wire, group in by_wire.items() if len(group) == 1
        }
        self._by_name: dict[str, list[ToolParam]] = {}
        for tool in tools:
            self._by_name.setdefault(tool.name, []).append(tool)

    def resolve_call(self, name: str, namespace: str | None) -> ResolvedCall:
        """The declaration a call identifies, with how the call named it (`ResolvedCall`). A
        flattened alias resolves to its group and own name together, so a renderer never
        un-flattens the name while forgetting the group."""
        if namespace is not None:
            tool = self._by_key.get((namespace, name))
            return ResolvedCall(tool=tool, name=name, called_namespace=namespace, how="namespaced")
        top = self._by_key.get((None, name))
        if top is not None:
            return ResolvedCall(tool=top, name=name, called_namespace=None, how="own")
        tool = self._by_wire.get(name)
        if tool is None:
            return ResolvedCall(tool=None, name=name, called_namespace=None, how="unknown")
        how: Literal["own", "alias"] = "own" if tool.name == name else "alias"
        return ResolvedCall(tool=tool, name=tool.name, called_namespace=None, how=how)

    def resolve(self, name: str, namespace: str | None) -> ToolParam | None:
        return self.resolve_call(name, namespace).tool

    def resolve_choice(self, name: str) -> ToolParam | None:
        """The declaration a forced tool choice names. A choice carries no group: a top-level
        tool of that name wins, else the one grouped tool of that name; two grouped tools
        sharing it leave the choice unresolved."""
        top = self._by_key.get((None, name))
        if top is not None:
            return top
        candidates = self._by_name.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def wire_name(self, name: str, namespace: str | None) -> str:
        """The name a dialect without namespaces uses for a call to (namespace, name)."""
        return self._wire.get((namespace, name), name)

    def choice_wire_name(self, name: str) -> str:
        """The name a dialect without namespaces forces for a choice of `name`: the wire name
        of the declaration it resolves to, the name as sent when nothing declares it, and a
        `CodecError` when two grouped tools share it (no one wire name stands for the choice)."""
        tool = self.resolve_choice(name)
        if tool is not None:
            return self.wire_name(tool.name, tool.namespace)
        if name in self._by_name:
            raise CodecError(
                f"forced tool choice {name!r} names a tool declared in several namespaces "
                "and cannot be flattened to one name"
            )
        return name
