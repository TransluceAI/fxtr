"""Per-model fixes learned from a provider's rejections, and what each model API knows of them.

A model API declares the `RejectionWorkaround`s its provider may need: each recognizes one
rejection and supplies the change that avoids it, in the vocabulary request
(`apply_to_request`) where it has a form there, else in the rendered request (`apply_to_wire`),
recording its losses either way. The API's `WorkaroundLedger` remembers which it has seen, and
`_raw_stream.call_and_decode` applies them and retries a rejected call with a newly recognized
one.

Recognizing a rejection is evidence, not proof: a matcher can be broad, and an unrelated bad
request can match it. So a recognized workaround is *provisional*, and applies only to requests
identical to one it was recognized on (by `fingerprint`): the retry within the call, or a
caller's retry of a call it cancelled. A success with it applied *confirms* it,
after which it applies to every call. A workaround is forgotten when a request with it applied
gets the rejection it recognizes again.
"""

import hashlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from behaviors.interfaces.dialect_codecs import AdaptedRequest
from behaviors.types.adaptation import RequestLoss
from behaviors.types.dialect import EncodedRequest
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest


@dataclass(frozen=True)
class Prepared:
    """One attempt at a call, ready to send.

    Attributes:
        original: The request as the caller asked for it.
        adapted: The request after the dialect's adaptation and the workarounds' vocabulary
            changes.
        encoded: What is sent: the rendered request after the workarounds' wire changes.
        losses: Every change made, in order.
    """

    original: ModelRequest
    adapted: AdaptedRequest
    encoded: EncodedRequest
    losses: tuple[RequestLoss, ...]


@dataclass(frozen=True)
class RejectionWorkaround:
    """A fix for one provider rejection.

    Attributes:
        name: A stable name, for the ledger and the diagnostics.
        recognizes: Whether a failure of a prepared attempt is the rejection this fixes, judged
            against what was sent.
        apply_to_request: The change in the vocabulary request, adding its losses, or None.
        apply_to_wire: The change in the rendered request, for a fix with no vocabulary form,
            with its losses, or None.
    """

    name: str
    recognizes: Callable[[Prepared, ModelCallFailure], bool]
    apply_to_request: Callable[[AdaptedRequest], AdaptedRequest] | None = None
    apply_to_wire: (
        Callable[[EncodedRequest], tuple[EncodedRequest, tuple[RequestLoss, ...]]] | None
    ) = None


def fingerprint(request: ModelRequest) -> str:
    """An identifier of `request`'s content, to tell a retry of a request from another one."""
    return hashlib.sha256(repr(request).encode()).hexdigest()


# How many rejected requests a provisional workaround remembers, for the retries that confirm it.
_PROVISIONAL_REQUESTS = 8


class WorkaroundLedger:
    """What one model API knows of its declared workarounds (see the module docstring).

    Args:
        declared: The workarounds, in the order they apply.
        on_confirm: Called once with each workaround as it is confirmed (a diagnostic).
    """

    def __init__(
        self,
        declared: tuple[RejectionWorkaround, ...],
        on_confirm: Callable[[RejectionWorkaround], None] | None = None,
    ) -> None:
        self.declared = declared
        self._on_confirm = on_confirm
        self._confirmed: set[str] = set()
        self._provisional: dict[str, deque[str]] = {}

    def applicable(self, request_fingerprint: str) -> tuple[RejectionWorkaround, ...]:
        """The workarounds that apply to a request: the confirmed ones, and those provisional for
        it, in declaration order."""
        return tuple(
            w
            for w in self.declared
            if w.name in self._confirmed or request_fingerprint in self._provisional.get(w.name, ())
        )

    def is_confirmed(self, workaround: RejectionWorkaround) -> bool:
        return workaround.name in self._confirmed

    def mark_provisional(self, workaround: RejectionWorkaround, request_fingerprint: str) -> None:
        if workaround.name in self._confirmed:
            return
        seen = self._provisional.setdefault(workaround.name, deque(maxlen=_PROVISIONAL_REQUESTS))
        if request_fingerprint not in seen:
            seen.append(request_fingerprint)

    def confirm(self, workaround: RejectionWorkaround) -> None:
        if workaround.name in self._confirmed:
            return
        self._confirmed.add(workaround.name)
        self._provisional.pop(workaround.name, None)
        if self._on_confirm is not None:
            self._on_confirm(workaround)

    def forget(self, workaround: RejectionWorkaround) -> None:
        self._confirmed.discard(workaround.name)
        self._provisional.pop(workaround.name, None)
