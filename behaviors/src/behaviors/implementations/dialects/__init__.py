"""Dialect codecs: one module per provider API dialect, each converting between that dialect's
wire shapes (request JSON, response JSON, streaming SSE, error bodies) and the behaviors model
vocabulary (`behaviors.types`).

Pure functions and codec objects with no I/O and no provider SDK imports (see
`behaviors.interfaces.dialect_codecs` for the protocols). A codec carries only configuration
(declared defaults such as Anthropic's `max_tokens`, a provider's `extra_body`), never per-call
state. The `ModelAPI` implementations use their own dialect's codec to render requests and
parse responses; composing one codec's harness side with another's model side translates a
harness's dialect into a subject model's.
"""

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import HarnessSideCodec
from behaviors.types.dialect import Dialect

__all__ = [
    "AnthropicMessagesCodec",
    "GeminiGenerateContentCodec",
    "OpenAIChatCompletionsCodec",
    "OpenAIResponsesCodec",
    "builtin_codecs",
]


def builtin_codecs() -> dict[Dialect, HarnessSideCodec]:
    """The harness-side codecs this library ships, keyed by dialect, in detection priority order.

    A fresh mapping each call, so a caller can extend or replace entries for one run without
    affecting others.
    """
    codecs: tuple[HarnessSideCodec, ...] = (
        AnthropicMessagesCodec(),
        OpenAIResponsesCodec(),
        GeminiGenerateContentCodec(),
    )
    return {codec.dialect: codec for codec in codecs}
