"""The behavior scenario's one seam to the Anthropic API.

Every real LLM service impl in this directory takes an
:class:`AnthropicChat` and calls :meth:`AnthropicChat.complete`; the
behavior runner mount constructs one shared instance per mount (real
mode only) and closes it on unwind. Keeping the ``anthropic`` import
localized here means the mock/off modes never import the SDK.

Messages use the scenario wire shape
(``[{"role": ..., "text": ...}, ...]`` — see
``behavior_research_types.protocols``) and are translated to the
Messages API here.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import anthropic

#: Cap on completion length — the demo wants short conversational
#: turns and one-paragraph verdicts, never essays.
_MAX_TOKENS = 1024


class AnthropicChat:
    """A thin shared wrapper over one :class:`anthropic.AsyncAnthropic`.

    Args:
        api_key: The Anthropic API key (the orchestrator reads it from
            the environment; this module never touches ``os.environ``).
    """

    def __init__(self, *, api_key: str) -> None:
        # The SDK client retries transient failures itself (its default
        # max_retries); anything that still escapes surfaces as an
        # errored/interrupted generation, retryable from the UI.
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(
        self, *, model: str, system: str, messages: Sequence[Mapping[str, Any]]
    ) -> str:
        """One chat completion; returns the assistant text.

        Args:
            model: Anthropic model id.
            system: The system prompt.
            messages: Scenario wire-shape messages, oldest first; must
                be non-empty with alternating roles ending in ``user``
                (the Messages API requirement — callers arrange this).
        """
        response = await self._client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": str(m["role"]), "content": str(m["text"])} for m in messages],
        )
        parts = [block.text for block in response.content if block.type == "text"]
        return "\n".join(parts).strip()
