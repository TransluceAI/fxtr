"""Prompt-cache breakpoints, shared by content blocks and tool definitions."""

from dataclasses import dataclass
from typing import Literal

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class CacheControl(TaggedDataclass, fxtr_type=behaviors_type("CacheControl.v1")):
    """A prompt-cache breakpoint with an optional provider-specific lifetime.

    Corresponds to the `cache_control` field on content blocks and tool
    definitions in the Anthropic Messages API
    (https://platform.claude.com/docs/en/build-with-claude/prompt-caching);
    OpenAI-compatible providers that support Anthropic-style caching (e.g.
    OpenRouter) accept the same shape. OpenAI Responses renders it as
    `prompt_cache_breakpoint` on a text part.

    Attributes:
        ttl: Cache time-to-live (Anthropic accepts "5m" or "1h", OpenAI Responses
            "30m" or "12h"), or None to inherit the destination's default, including
            Responses' request-wide TTL.
    """

    ttl: str | None = None
    type: Literal["ephemeral"] = "ephemeral"
