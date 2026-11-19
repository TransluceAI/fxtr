"""Adapting a vocabulary request to what a dialect, and a model, can carry.

A model API sends a `ModelRequest` through its dialect's codec. Before the codec renders it,
`ModelSideCodec.adapt_request` changes the request so the dialect can carry it (a setting it has
no form for left out, a value replaced by its nearest equivalent, a schema fitted to the subset
the provider enforces; the result is an `AdaptedRequest`, `behaviors.interfaces.dialect_codecs`),
and the model API applies the `RejectionWorkaround`s it has learned for
its model. Each change is recorded as a `RequestLoss`, and the losses of the attempt that
produced a result travel on it (`ModelResponse.adaptation_losses`,
`ModelCallFailure.adaptation_losses`).
"""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class RequestLoss(TaggedDataclass, fxtr_type=behaviors_type("RequestLoss.v1")):
    """One way a request was not carried as asked.

    Two steps record these, each at the point it makes the change, so a list of them is what was
    done rather than a guess about it:

    - Decoding a harness request (`HarnessSideCodec.decode_request`), for what the model
      vocabulary cannot hold (an image, a provider-side tool).
    - Adapting a vocabulary request to a dialect (`ModelSideCodec.adapt_request`, and the
      `RejectionWorkaround`s a model API applies), for what the dialect or the model cannot carry
      as stated: something left out, replaced by its nearest equivalent, or sent unenforced.

    Attributes:
        code: A short, stable identifier for what changed (`stop_sequences`, `image`,
            `provider_tool:web_search`), used to deduplicate reports. The set is open: a codec
            adds a code for each new kind of change it makes. A code is always recorded with
            the same `semantic` class.
        detail: What exactly changed, for the message.
        semantic: Whether the model is asked something different (True: a dropped instruction,
            a substituted setting, an unenforced contract), as opposed to the same request
            served differently (False: cost, latency or caching only). A caller that refuses
            changes in meaning refuses semantic losses only (`AdaptationPolicy`).
    """

    code: str
    detail: str
    semantic: bool = True


@dataclass(frozen=True)
class ResponseLoss(TaggedDataclass, fxtr_type=behaviors_type("ResponseLoss.v1")):
    """One way a response did not reach a harness as the subject model gave it: something the
    harness's dialect has no form for, rendered as its nearest equivalent (a streamed refusal as
    ordinary text, another provider's content filter as Anthropic's refusal stop). A server
    serving a harness records these (`HarnessSideCodec.render_stop`); unlike a `RequestLoss`
    there is no class, since no policy acts on them.

    Attributes:
        code: A short, stable identifier for what changed, used to deduplicate reports.
        detail: What exactly changed, for the message.
    """

    code: str
    detail: str


@dataclass(frozen=True)
class AdaptationPolicy(TaggedDataclass, fxtr_type=behaviors_type("AdaptationPolicy.v1")):
    """What a call may change about a request in order to send it.

    Set on the request (`ModelRequest.adaptation_policy`), so it reaches the model API through
    retries and wrappers, and the stored request records what the call allowed. Read by the
    codec-backed model APIs; a `ModelAPI` that does not adapt requests does not read it.

    Attributes:
        semantic_changes: `allow` (the default) makes every change and reports it; `refuse`
            fails the call instead of making a change that asks the model something different
            (a `RequestLoss` with `semantic = True`). Changes that only affect cost, latency or
            caching are always allowed. A refused call is a `ModelCallFailure` with category
            `adaptation_refused`.
    """

    semantic_changes: Literal["allow", "refuse"] = "allow"

    def refuses(self, losses: tuple[RequestLoss, ...]) -> RequestLoss | None:
        """The first loss in `losses` this policy refuses, or None."""
        if self.semantic_changes == "allow":
            return None
        return next((loss for loss in losses if loss.semantic), None)


DEFAULT_ADAPTATION_POLICY = AdaptationPolicy()
