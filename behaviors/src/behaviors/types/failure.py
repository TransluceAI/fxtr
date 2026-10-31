"""Model call failures, returned in-band instead of raised.

A model that cannot produce a response returns a ``ModelCallFailure`` — from
``generate`` in place of a response, or from ``generate_turn`` in place of a
turn — rather than raising an exception. Failures are values because
consumers dispatch on them and record them: the rollout runner turns some
categories into a ``RolloutFailed`` status stored inside the ``ChatRollout``
entity and raises on the rest; ``chat_session_model_from_api`` sessions retry
configured categories. Different consumers can handle the same failure
differently, so the model reports and the consumer decides.
``ModelCallFailedError`` exists for consumers that decide a failure is fatal
after all.
"""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass

type FailureCategory = Literal[
    "context_length",
    "content_filter",
    "rate_limited",
    "overloaded",
    "authentication",
    "invalid_request",
    "server_error",
]
"""Why a model call failed.

- ``context_length``: the request does not fit in the model's context window
  (OpenAI ``context_length_exceeded``, Anthropic "prompt is too long", or a
  chatbot UI refusing further input because the conversation is too long).
- ``content_filter``: an input-side safety system rejected the request before
  the model produced anything (OpenAI/Azure content-policy errors, a chatbot
  UI refusing to send a message). Distinct from ``StopContentFilter`` (see
  ``behaviors.types.response``), where generation started and a response
  exists.
- ``rate_limited``: the provider refused the request for quota reasons (429).
- ``overloaded``: the provider is temporarily unable to serve (Anthropic 529,
  timeouts, connection failures).
- ``authentication``: the credentials were rejected (401/403).
- ``invalid_request``: the provider rejected the request as malformed (other
  4xx).
- ``server_error``: the provider failed internally (5xx).

The categories are deliberately exhaustive over known provider failure modes;
a failure that fits none of them is a bug or an unknown provider behavior, and
implementations raise ``ModelError``.
"""


@dataclass(frozen=True)
class ModelCallFailure(TaggedDataclass, fxtr_type=behaviors_type("ModelCallFailure.v1")):
    """One model call's failure, as data.

    The tag distinguishes a serialized failure from a serialized
    ``ModelTurn`` or ``ModelResponse``.

    Attributes:
        category: Why the call failed.
        description: Human-readable detail, e.g. the provider's error message.
        provider_http_status: The HTTP status the provider returned, or None if
            the failure did not come from an HTTP response.
    """

    category: FailureCategory
    description: str
    provider_http_status: int | None
    kind: Literal["model_call_failure"] = "model_call_failure"
