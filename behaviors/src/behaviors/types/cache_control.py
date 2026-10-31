"""Prompt-cache breakpoints, shared by content blocks and tool definitions."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class CacheControl(TaggedDataclass, fxtr_type=behaviors_type("CacheControl.v1")):
    """An Anthropic-style prompt-cache breakpoint.

    Corresponds to the `cache_control` field on content blocks and tool
    definitions in the Anthropic Messages API
    (https://platform.claude.com/docs/en/build-with-claude/prompt-caching);
    OpenAI-compatible providers that support Anthropic-style caching (e.g.
    OpenRouter) accept the same shape.

    Attributes:
        ttl: Cache time-to-live (Anthropic accepts "5m" or "1h"), or None for
            the provider's default.
    """

    ttl: str | None = None
    type: Literal["ephemeral"] = "ephemeral"
