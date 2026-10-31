"""Stored conversations and named, nested collections of conversations.

Transcripts and groups are contained directly in their parent records. Each
record is independently storable as a fxtr entity; metadata carries caller-
defined JSON information about a conversation or run.
"""

from __future__ import annotations

import dataclasses

from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.message import ChatMessage
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_entity import DataclassEntity


@dataclasses.dataclass(frozen=True)
class Transcript(DataclassEntity, fxtr_type=behaviors_type("Transcript.v1")):
    """A single linear conversation.

    Attributes:
        name: Short human-readable label, or None.
        description: What this transcript is, e.g. "conversation with the assistant", or None.
        messages: The messages in the transcript.
        metadata: Free-form information about the transcript.
    """

    name: str | None
    description: str | None
    messages: tuple[ChatMessage, ...]
    metadata: JSONObject = dataclasses.field(default_factory=dict[str, JSONValue])


@dataclasses.dataclass(frozen=True)
class TranscriptGroup(DataclassEntity, fxtr_type=behaviors_type("TranscriptGroup.v1")):
    """A named grouping of transcripts, nestable into a tree.

    Attributes:
        name: Short human-readable label, or None.
        description: What this group contains, or None.
        transcripts: Transcripts directly in this group.
        groups: Child groups nested under this one.
        metadata: Free-form information about the group.
    """

    name: str | None
    description: str | None
    transcripts: tuple[Transcript, ...]
    groups: tuple[TranscriptGroup, ...]
    metadata: JSONObject = dataclasses.field(default_factory=dict[str, JSONValue])


@dataclasses.dataclass(frozen=True)
class AgentRun(DataclassEntity, fxtr_type=behaviors_type("AgentRun.v1")):
    """One run of an agent/experiment: a tree of transcripts plus metadata.

    Attributes:
        name: Short human-readable label, or None.
        description: What this run is, or None.
        transcripts: Transcripts at the top level of the run.
        groups: Transcript groups at the top level of the run.
        metadata: Free-form information about the run.
    """

    name: str | None
    description: str | None
    transcripts: tuple[Transcript, ...]
    groups: tuple[TranscriptGroup, ...]
    metadata: JSONObject = dataclasses.field(default_factory=dict[str, JSONValue])
