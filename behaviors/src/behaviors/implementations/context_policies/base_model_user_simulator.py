"""A simulated user that is a base model continuing a transcript.

``BaseModelUserSimulator`` prompts a base (non-chat) model with a rendered
document — reference users' vignettes and example transcripts, the target
user's vignette, and the conversation so far in ChatML (see
``base_model_prompt``) — and samples the user's next message from the
completions endpoint of any OpenAI-compatible server (vLLM, sglang, ...). A
completion containing ``</transcript>`` ends the rollout, whatever surrounds
the marker. Session state is the visible conversation, so restoring N sessions
from one state and taking a turn from each samples N candidate next messages.

Each turn's (or end's) metadata carries a ``SimulatedUserText`` whose
``raw_text`` is the completion truncated after the first end marker (the form
in which a pilot sees candidates).
"""

import contextlib
import dataclasses
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, cast

import anyio
import httpx2
import openai
from openai import AsyncOpenAI

from behaviors.implementations.context_policies._shared import (
    END_TRANSCRIPT_MARKER,
    SimulatedUserText,
    ends_transcript,
    message_text,
)
from behaviors.implementations.context_policies.base_model_prompt import (
    ReferenceUser,
    SimulatedUserProfile,
    default_chatml_stop_sequences,
    render_base_model_prompt,
)
from behaviors.types.content import ContentText
from behaviors.types.message import ChatMessage, UserMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.retry import RetryConfig
from behaviors.types.rollout import EndRollout, UserTurn
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

logger = logging.getLogger(__name__)

type RequestGuard = Callable[[], AbstractAsyncContextManager[None]]
"""Makes a context manager to hold around one completions request, e.g. a rate-limit slot."""


class EmptyCompletionError(ValueError):
    """Raised when a completions response contains no generated text."""


@dataclasses.dataclass(frozen=True)
class BaseModelUserSimulatorConfig(
    TaggedDataclass, fxtr_type=behaviors_type("BaseModelUserSimulatorConfig.v1")
):
    """Everything about a base-model simulated user except how to reach the model.

    Attributes:
        profile: The user being simulated.
        model: Name of the base model, as the completions endpoint knows it.
        references: Few-shot reference users, rendered before the profile.
        use_preface: Whether to open the prompt with ``BASE_MODEL_PREFACE``.
        stop_sequences: Where the completion stops; by default the ChatML turn boundaries.
        max_tokens: Completion length limit, or None for the server's default.
        temperature: Sampling temperature, or None for the server's default.
        top_p: Nucleus sampling parameter, or None for the server's default.
    """

    profile: SimulatedUserProfile
    model: str
    references: tuple[ReferenceUser, ...] = ()
    use_preface: bool = True
    stop_sequences: tuple[str, ...] = default_chatml_stop_sequences()
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None


@dataclasses.dataclass(frozen=True)
class _SessionState(TaggedDataclass, fxtr_type=behaviors_type("BaseModelUserSimulatorState.v1")):
    """The visible conversation so far."""

    messages: tuple[ChatMessage, ...]


def _truncate_after_end_marker(text: str) -> str:
    """Keep ``</transcript>`` visible while removing base-model continuation text.

    The end marker is deliberately not a server-side stop sequence, because the simulator
    has to know whether the model actually sampled it. Having sampled it, the model may
    continue with arbitrary text; only the candidate up through the first marker is kept.
    """
    marker_index = text.find(END_TRANSCRIPT_MARKER)
    if marker_index == -1:
        return text
    return text[: marker_index + len(END_TRANSCRIPT_MARKER)].strip()


class BaseModelUserSimulatorSession:
    """One rollout's worth of simulated user messages from a base model."""

    def __init__(
        self,
        simulator: "BaseModelUserSimulator",
        messages: tuple[ChatMessage, ...],
    ):
        self._simulator = simulator
        self._messages = messages

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        if model_messages is not None:
            self._messages = self._messages + model_messages
        if user_turn_override is not None:
            self._messages = (*self._messages, user_turn_override)
            metadata = SimulatedUserText(
                source="override", raw_text=message_text(user_turn_override)
            )
            return UserTurn(
                message=user_turn_override, metadata=(DynamicData.from_tagged(metadata),)
            )
        config = self._simulator.config
        if not self._messages and config.profile.initial_user_message is not None:
            return self._turn_from_candidate(config.profile.initial_user_message, source="fixed")
        prompt = render_base_model_prompt(
            profile=config.profile,
            transcript=self._messages,
            references=config.references,
            use_preface=config.use_preface,
        )
        raw_text = await self._simulator.sample_completion(prompt)
        return self._turn_from_candidate(raw_text, source="sampled")

    def _turn_from_candidate(
        self, raw_text: str, *, source: Literal["sampled", "fixed"]
    ) -> UserTurn | EndRollout:
        metadata = (DynamicData.from_tagged(SimulatedUserText(source=source, raw_text=raw_text)),)
        if ends_transcript(raw_text):
            return EndRollout(metadata=metadata)
        message = UserMessage(content=(ContentText(text=raw_text.strip()),))
        self._messages = (*self._messages, message)
        return UserTurn(message=message, metadata=metadata)

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(_SessionState(messages=self._messages))

    async def aclose(self) -> None:
        pass


class BaseModelUserSimulator:
    """A ``SerializableUserSimulator`` sampling user messages from a base model.

    Attributes are fixed at construction: the config, the client (or the base
    URL / credentials to build one), the retry schedule for failed or empty
    completions requests, and an optional ``request_guard`` entered around
    each request (held for the whole streamed response).
    """

    def __init__(
        self,
        config: BaseModelUserSimulatorConfig,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        retry: RetryConfig | None = None,
        request_guard: RequestGuard | None = None,
    ):
        if client is not None and (api_key is not None or base_url is not None):
            raise ValueError("pass either a client or api_key/base_url, not both")
        self.config = config
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._retry = retry
        self._request_guard = request_guard

    async def start_session(self) -> BaseModelUserSimulatorSession:
        return BaseModelUserSimulatorSession(self, ())

    async def restore_session(self, state: DynamicData) -> BaseModelUserSimulatorSession:
        return BaseModelUserSimulatorSession(self, state.as_type(_SessionState).messages)

    async def aclose(self) -> None:
        """Release the underlying client, if this instance constructed it."""
        if self._owns_client:
            await self._client.close()

    async def sample_completion(self, prompt: str) -> str:
        """Sample one completion of `prompt`, truncated after any end-of-transcript marker.

        Retries ``openai.APIError``, transport errors, and empty completions on the
        configured schedule; once attempts are exhausted the last error propagates.
        """
        # The request streams so that bytes keep flowing on the connection while tokens
        # generate: it then only has to survive its time-to-first-token in silence under the
        # fixed inactivity timeouts of the client and of any proxies in between, rather than
        # queue wait plus the entire generation. Multiple candidates are sampled as separate
        # single-completion requests (one restored session each), never via the ``n``
        # parameter: vllm counts a request with ``n`` sub-sequences as ``n`` entries in its
        # num_requests_running metric while it occupies a single HTTP connection, so an
        # autoscaler inferring per-connection capacity from that metric under-scales.
        config = self.config
        kwargs: dict[str, Any] = {"model": config.model, "prompt": prompt, "n": 1, "stream": True}
        if config.max_tokens is not None:
            kwargs["max_tokens"] = config.max_tokens
        if config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if config.top_p is not None:
            kwargs["top_p"] = config.top_p
        if config.stop_sequences:
            kwargs["stop"] = list(config.stop_sequences)
        create = cast(Any, self._client.completions.create)

        attempt = 1
        while True:
            try:
                # The guard is entered inside the retried unit so backoff sleeps don't hold a
                # slot, and held for the whole stream because the worker is generating for that
                # whole span.
                async with (
                    self._request_guard()
                    if self._request_guard is not None
                    else contextlib.nullcontext()
                ):
                    stream = await create(**kwargs)
                    parts: list[str] = []
                    async with stream:
                        async for chunk in stream:
                            for choice in chunk.choices:
                                parts.append(choice.text)
                completion = "".join(parts).strip()
                # The base model sampling a stop sequence (typically ``<|im_end|>``) as its
                # very first token.
                if not completion:
                    raise EmptyCompletionError("the base model returned an empty completion")
                return _truncate_after_end_marker(completion)
            except (
                EmptyCompletionError,
                openai.APIError,
                httpx2.TransportError,  # a stream dying mid-flight surfaces as a transport error
            ) as exc:
                if self._retry is None or attempt >= self._retry.max_attempts:
                    raise
                backoff = self._retry.backoff_seconds(attempt)
                logger.warning(
                    "Base-model completion attempt %d failed (%r); retrying in %.1fs",
                    attempt,
                    exc,
                    backoff,
                )
                await anyio.sleep(backoff)
                attempt += 1
