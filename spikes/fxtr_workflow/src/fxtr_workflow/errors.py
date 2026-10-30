"""Error types for the workflow system."""

from __future__ import annotations


class StepAbortError(BaseException):
    """Raised when a step encounters an uncaught exception.

    This is a BaseException (not Exception) so that normal except clauses
    don't accidentally catch it. The step's cache slot stays in Partial state
    so the step can be retried on next execution.
    """

    def __init__(self, original_exception: BaseException, step_name: str = ""):
        self.original_exception = original_exception
        self.step_name = step_name
        super().__init__(f"Step aborted: {original_exception}")


class CacheSlotError(Exception):
    """Raised for cache slot state violations (write-once, wrong state, etc)."""


class ReplayCheckError(CacheSlotError):
    """Raised when the cache-slot-arg replay check fails."""


class FallibleRunError(Exception):
    """Base for errors cached by run_fallible. Always catchable (Exception, not BaseException).

    On both the first execution and on replay, the same subclass is raised with
    the same data — no pickled exception state is involved.
    """


class CustomFallibleRunError(FallibleRunError):
    """The step explicitly raised this error with a string message.

    Use this when you want precise control over the cached error message.
    """

    def __init__(self, message: str, serialized_traceback: str = "") -> None:
        self.message = message
        self.original_type_name: str | None = None
        self.serialized_traceback = serialized_traceback
        super().__init__(message)


class WrappedFallibleRunError(FallibleRunError):
    """The step raised some other exception, lossily stringified.

    Created automatically by run_fallible when the step raises anything
    other than CustomFallibleRunError.
    """

    def __init__(
        self, original_type_name: str, message: str, serialized_traceback: str = ""
    ) -> None:
        self.original_type_name: str = original_type_name
        self.message = message
        self.serialized_traceback = serialized_traceback
        super().__init__(f"{original_type_name}: {message}")
