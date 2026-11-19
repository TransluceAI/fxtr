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

from behaviors.types.adaptation import RequestLoss
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
    "adaptation_refused",
]
"""Why a model call failed.

- ``context_length``: the request does not fit in the model's context window
  (OpenAI ``context_length_exceeded``, Anthropic "prompt is too long", or a
  chatbot UI refusing further input because the conversation is too long).
- ``content_filter``: an input-side safety system rejected the request with an
  error before the model produced anything (OpenAI's `invalid_prompt` and Azure
  content-policy 400s, a chatbot UI refusing to send a message). Other
  providers report a blocked prompt as a response instead: Anthropic with
  `stop_reason: "refusal"`, Gemini with `promptFeedback.blockReason`, decoded
  as ``StopRefusal`` or ``StopContentFilter`` (see
  ``behaviors.types.response``), as is a stop after generation started.
- ``rate_limited``: the provider refused the request for quota reasons (429).
- ``overloaded``: the provider is temporarily unable to serve (Anthropic 529,
  timeouts, connection failures).
- ``authentication``: the credentials were rejected (401/403).
- ``invalid_request``: the provider rejected the request as malformed (other
  4xx).
- ``server_error``: the provider failed internally (5xx).
- ``adaptation_refused``: the request could not be served without a change in
  meaning, which the call's ``AdaptationPolicy`` refuses (see
  ``behaviors.types.adaptation``). The failure's ``proposed_adaptation_losses``
  are the changes that were not made; the semantic ones among them are what the
  policy refused. Either nothing was sent (``provider_http_status`` is None), or
  the provider rejected the request as sent and those changes were its fix (the
  provider's status and message are kept).

The categories are deliberately exhaustive over known provider failure modes;
a failure that fits none of them is a bug or an unknown provider behavior, and
implementations raise ``ModelError``.
"""


@dataclass(frozen=True)
class ModelCallFailure(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("ModelCallFailure.v2"),
):
    """One model call's failure, as data.

    The tag distinguishes a serialized failure from a serialized
    ``ModelTurn`` or ``ModelResponse``.

    Attributes:
        category: Why the call failed.
        description: Human-readable detail, e.g. the provider's error message.
        provider_http_status: The HTTP status the provider returned, or None if
            the failure did not come from an HTTP response.
        adaptation_losses: How the request of the attempt that failed was changed to fit
            the dialect and model before it was sent (see `behaviors.types.adaptation`);
            empty when it was sent as asked, or not sent.
        proposed_adaptation_losses: Under category ``adaptation_refused``, the changes the
            request needed that were not made, because the policy refused them (the
            semantic ones) or the attempt that needed them was not sent; empty otherwise.
    """

    category: FailureCategory
    description: str
    provider_http_status: int | None
    adaptation_losses: tuple[RequestLoss, ...] = ()
    proposed_adaptation_losses: tuple[RequestLoss, ...] = ()
    kind: Literal["model_call_failure"] = "model_call_failure"
