"""Projections: from bespoke rollout results to standard types for judging.

A projection is a deterministic derivation of a result into the shape a
consumer needs: it selects and reshapes, and never authors new information.
Each rollout kind has its own result
format; a projection turns such a result into a ``Transcript`` (when the
result is a single linear conversation) or an ``AgentRun`` (when it is not).
Judges are written against the standard types, so one judge works across
rollout kinds that provide a projection. The methods are async because
deriving from stored results may require loads (content-addressed, so the
derivation stays deterministic). Implementations live with their rollout
kinds (e.g. ``behaviors.rollouts.chat.ChatRolloutTranscriptProjection``);
this module holds only the protocols.
"""

from typing import Protocol, runtime_checkable

from behaviors.types.agent_run import AgentRun, Transcript


@runtime_checkable
class TranscriptProjection[RolloutResultT](Protocol):
    """Projects a rollout result to a single transcript for judging."""

    async def to_transcript(self, result: RolloutResultT) -> Transcript: ...


@runtime_checkable
class AgentRunProjection[RolloutResultT](Protocol):
    """Projects a rollout result to an agent run for judging."""

    async def to_agent_run(self, result: RolloutResultT) -> AgentRun: ...
