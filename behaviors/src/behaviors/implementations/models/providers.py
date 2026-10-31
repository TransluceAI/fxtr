"""Generic ``ModelProvider`` implementations: a fixed single model, and per-id overrides.

The vendor providers (``AnthropicMessagesProvider``, ...) live next to the ``ModelAPI``
classes they construct; the providers here are vendor-agnostic building blocks.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from behaviors.interfaces.model_apis import ModelAPI, ModelProvider


@dataclass(frozen=True)
class SingleModelProvider:
    """A ``ModelProvider`` that serves one ``ModelAPI`` for every requested id.

    Attributes:
        model_api: The API serving every request, whatever model id it names.
    """

    model_api: ModelAPI

    def get_model_api(self, model_id: str) -> ModelAPI | None:
        return self.model_api


@dataclass(frozen=True)
class ModelOverrides:
    """A ``ModelProvider`` that reroutes specific model ids, delegating the rest.

    Attributes:
        overrides: Model id -> the ``ModelAPI`` serving it, matched exactly.
        base: The provider serving ids not in ``overrides``; None makes unmatched
            ids unservable (``get_model_api`` returns None).
    """

    overrides: Mapping[str, ModelAPI]
    base: ModelProvider | None = None

    def get_model_api(self, model_id: str) -> ModelAPI | None:
        override = self.overrides.get(model_id)
        if override is not None:
            return override
        if self.base is None:
            return None
        return self.base.get_model_api(model_id)
