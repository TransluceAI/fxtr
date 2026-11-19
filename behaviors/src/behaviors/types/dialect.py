"""Wire dialects: the provider API schemas that requests and responses are written in.

A `Dialect` names one concrete HTTP+JSON schema for LLM inference by its vendor surface,
independent of who serves it: OpenRouter speaks chat completions, OpenCode speaks Anthropic
Messages, a vLLM server may speak Anthropic Messages. The dialect codecs
(`behaviors.interfaces.dialect_codecs`) convert between a dialect and the model vocabulary; the
types here are the shapes that cross that boundary without being vocabulary themselves.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass

Dialect = Literal[
    "anthropic_messages",
    "openai_chat_completions",
    "openai_responses",
    "gemini_generate_content",
]

ALL_DIALECTS: tuple[Dialect, ...] = (
    "anthropic_messages",
    "openai_chat_completions",
    "openai_responses",
    "gemini_generate_content",
)

Headers = tuple[tuple[str, str], ...]
"""HTTP header (name, value) pairs in wire order, names not lowercased, decoded latin-1."""

QueryParams = tuple[tuple[str, str], ...]
"""URL query (name, value) pairs in wire order."""


@dataclass(frozen=True)
class DialectExtras(TaggedDataclass, fxtr_type=behaviors_type("DialectExtras.v1")):
    """Request material a dialect carries that the model vocabulary has no slot for.

    Tagged with the dialect it is written in and applied only by a codec of that dialect, so a
    key that exists in two dialects with different meanings (`metadata` in Anthropic Messages and
    in OpenAI Responses) never crosses between them.

    Attributes:
        dialect: The dialect the material is written in.
        headers: Request headers the codec does not model, e.g. `anthropic-beta`.
        query: Query parameters, e.g. `beta=true`.
        body: The leftover leaves of the request body, in place: a nested partial object such as
            `{"thinking": {"display": "summarized"}, "context_management": {...}}`.
    """

    dialect: Dialect
    headers: Headers = ()
    query: QueryParams = ()
    body: JSONObject = field(default_factory=dict[str, JSONValue])


@dataclass(frozen=True)
class EncodedRequest:
    """A model request rendered into a dialect: what a `ModelAPI` sends, minus credentials.

    Attributes:
        path: The endpoint path, e.g. `/v1/messages`.
        body: The JSON body.
        query: Query parameters.
        headers: Dialect-semantic headers only (feature flags such as `anthropic-beta`); the
            client adds credentials, content negotiation and its own identification.
    """

    path: str
    body: JSONObject
    query: QueryParams = ()
    headers: Headers = ()


@dataclass(frozen=True)
class RawHttpResponse:
    """A provider HTTP response relayed verbatim.

    Attributes:
        status: HTTP status code.
        headers: Response headers with framing and content-encoding headers removed, so the body
            may be re-framed by whoever relays it.
        body: The response body, identity-encoded, as it arrives.
        aclose: Releases the underlying connection; call once, whether or not the body was
            drained.
    """

    status: int
    headers: Headers
    body: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]
