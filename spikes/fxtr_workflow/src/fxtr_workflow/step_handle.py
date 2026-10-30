"""Step handles: identifiers for dispatchable steps."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, Protocol

from fxtr2.data.entity_core import FxtrID

if TYPE_CHECKING:
    from fxtr_workflow.step_context import StepContext


class StepImpl(Protocol):
    """Protocol for step function implementations."""

    async def __call__(self, step_ctx: StepContext, /, *args: Any, **kwargs: Any) -> Any: ...


class StepHandle(abc.ABC):
    """Handle identifying a step that can be dispatched."""

    @property
    @abc.abstractmethod
    def identity(self) -> FxtrID:
        """The identity of this step, used for cache key computation."""
        ...


class ExternalStepHandle(StepHandle):
    """A step handle for externally-provided step functions.

    The key_id is an opaque FxtrID chosen by the caller to identify what this
    step represents (e.g., a contract or capability). The impl is the actual
    callable. The optional config_id identifies a config entity that the step
    can access at runtime (NOT part of the cache key).
    """

    def __init__(self, impl: StepImpl, key_id: FxtrID, config_id: FxtrID | None = None) -> None:
        self._impl = impl
        self._key_id = key_id
        self._config_id = config_id

    @property
    def identity(self) -> FxtrID:
        return self._key_id

    @property
    def impl(self) -> StepImpl:
        return self._impl

    @property
    def config_id(self) -> FxtrID | None:
        return self._config_id
