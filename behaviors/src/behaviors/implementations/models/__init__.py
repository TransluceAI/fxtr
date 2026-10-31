"""Model implementations.

Provider-backed ``ModelAPI`` implementations live in their own modules
(``anthropic``, ``openai_compatible``, ``openai_responses``) and
are deliberately not re-exported here: importing them requires the provider
SDKs from the ``models`` extra, which this package must not need at import
time.
"""

from behaviors.implementations.models.from_api import (
    APIBackedChatSession,
    chat_session_model_from_api,
)
from behaviors.implementations.models.overrides import SamplingOverrides
from behaviors.implementations.models.retries import (
    DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    generate_with_retries,
)

__all__ = [
    "APIBackedChatSession",
    "DEFAULT_FAILURE_CATEGORIES_TO_RETRY",
    "SamplingOverrides",
    "chat_session_model_from_api",
    "generate_with_retries",
]
