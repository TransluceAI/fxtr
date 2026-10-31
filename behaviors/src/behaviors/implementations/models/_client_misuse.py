"""Telling a misused SDK client apart from an unreachable provider."""

from behaviors.interfaces.model_apis import ModelError


def raise_if_client_misused(exc: BaseException) -> None:
    """Raise ``ModelError`` if ``exc`` was caused by using an SDK client after it was closed or
    outside its event loop.

    An SDK client's connections belong to the event loop that opened them. Using the client from
    another loop, or after closing it, fails inside the transport with a ``RuntimeError`` ("Event
    loop is closed", "... attached to a different loop", "... the client has been closed"), which
    the SDK may report as a connection error. Classified as one, it would be retried as provider
    overload on every attempt; it is a caller bug instead.
    """
    cause = exc.__cause__
    while cause is not None:
        if isinstance(cause, RuntimeError):
            raise ModelError(
                f"SDK transport failed with RuntimeError ({cause}); a client was probably used "
                "after it was closed, or outside the event loop it belongs to"
            ) from exc
        cause = cause.__cause__
