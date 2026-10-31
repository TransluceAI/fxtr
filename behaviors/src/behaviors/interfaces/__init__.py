"""The protocols to implement to plug into the behaviors library."""

from behaviors.interfaces.chat_session_models import (
    ChatSession,
    ChatSessionModel,
    SerializableChatSession,
    SerializableChatSessionModel,
    SerializableStreamingChatSession,
    SerializableStreamingChatSessionModel,
    StreamingChatSession,
    StreamingChatSessionModel,
)
from behaviors.interfaces.context_policies import (
    ContextPolicy,
    ContextPolicySession,
    SerializableContextPolicy,
    SerializableContextPolicySession,
    SerializableToolPolicy,
    SerializableToolPolicySession,
    SerializableUserSimulator,
    SerializableUserSimulatorSession,
    ToolPolicy,
    ToolPolicySession,
    UserSimulator,
    UserSimulatorSession,
)
from behaviors.interfaces.model_apis import (
    ModelAPI,
    ModelCallFailedError,
    ModelError,
    ModelProvider,
    StreamingModelAPI,
)
from behaviors.interfaces.projections import AgentRunProjection, TranscriptProjection

__all__ = [
    "AgentRunProjection",
    "ContextPolicy",
    "ContextPolicySession",
    "ModelCallFailedError",
    "ModelError",
    "ChatSession",
    "SerializableContextPolicy",
    "SerializableContextPolicySession",
    "SerializableChatSession",
    "SerializableChatSessionModel",
    "SerializableStreamingChatSession",
    "SerializableStreamingChatSessionModel",
    "SerializableToolPolicy",
    "SerializableToolPolicySession",
    "SerializableUserSimulator",
    "SerializableUserSimulatorSession",
    "ChatSessionModel",
    "ModelAPI",
    "ModelProvider",
    "StreamingChatSession",
    "StreamingChatSessionModel",
    "StreamingModelAPI",
    "ToolPolicy",
    "ToolPolicySession",
    "TranscriptProjection",
    "UserSimulator",
    "UserSimulatorSession",
]
