"""``Transcript`` entity type — wraps a single Docent transcript
(typically the ``main`` assistant transcript pulled via DQL).

Use :func:`build_transcript` to construct one from a Docent
``transcripts`` row (where ``messages`` is the raw JSON string),
then ``.store()`` and ``present_to_user`` it. The matching
renderer lives at ``renderers/transcript/``.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities

#: Stable namespace UUID for the transcripts package's entity types.
_TRANSCRIPTS_NS = uuid.UUID("4a6d7e8f-1b2c-4d3e-9f56-0a1b2c3d4e5f")


@dataclasses.dataclass(frozen=True)
class TranscriptMessage:
    """One message in a transcript. ``content`` is always
    flattened to a single string (list-of-parts payloads have
    their ``text`` fields joined with newlines)."""

    role: str
    content: str


@dataclasses.dataclass(frozen=True)
class Transcript(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TRANSCRIPTS_NS, "transcript"),
):
    """A single transcript pulled from a Docent collection."""

    transcript_id: str
    agent_run_id: str
    name: str
    description: str | None
    created_at: str
    messages: tuple[TranscriptMessage, ...]


def _flatten_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and "text" in p:
                parts.append(str(p["text"]))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def build_transcript(
    *,
    transcript_id: str,
    agent_run_id: str,
    name: str,
    description: str | None,
    created_at: str,
    messages_json: str,
) -> Transcript:
    """Parse a Docent ``transcripts`` row into a :class:`Transcript`."""
    raw = json.loads(messages_json)
    msgs = tuple(
        TranscriptMessage(
            role=str(m.get("role", "?")),
            content=_flatten_content(m.get("content")),
        )
        for m in raw
    )
    return Transcript(
        transcript_id=transcript_id,
        agent_run_id=agent_run_id,
        name=name,
        description=description,
        created_at=created_at,
        messages=msgs,
    )


__all__ = ["Transcript", "TranscriptMessage", "build_transcript"]
