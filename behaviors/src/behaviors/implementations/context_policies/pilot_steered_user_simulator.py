"""A pilot-steered user simulator: a chat model steers any serializable simulator's next message.

At each choice point the wrapper restores ``candidate_count`` copies of the
base session from its captured state and takes one turn from each, giving
candidates drawn from the base simulator's own distribution. The pilot model
sees them (with the conversation so far and, at the first choice point, the
user's profile) and answers with a log-space shift per candidate plus optional
custom text (see ``pilot_prompt``); one candidate is softmax-sampled from the
shifted distribution and its session becomes the live base session. Custom
text goes to a freshly restored base session as a ``user_turn_override``, so
the base must accept overrides whenever ``allow_custom_text`` is set.

Whatever is selected, the wrapper applies the ``</transcript>`` convention
the pilot prompt describes: a selected candidate or custom text containing
the marker ends the rollout immediately, and nothing around the marker is
sent. With ``steer_first_turn`` off, the user's opening message is taken from
the base as is, for a base whose first message is fixed. Every steered turn's
metadata carries a ``PilotSteering`` record — transcript length, candidates, shifts,
custom text, selected index and text, thinking, the pilot request messages,
and the raw response — after the base turn's own metadata.

Pilot failures raise: a ``ModelCallFailedError`` once the retry schedule is
exhausted, and ``ValueError`` after ``PILOT_RESPONSE_MAX_ATTEMPTS`` malformed
responses in a row.
"""

import dataclasses
import logging
import random
from types import TracebackType
from typing import Protocol, cast

import anyio

from behaviors.implementations.context_policies._shared import SimulatedUserText, ends_transcript
from behaviors.implementations.context_policies.pilot_prompt import (
    END_TRANSCRIPT_MARKER,
    LastSteeringOutcome,
    PilotResponse,
    augmented_shifts_for_selection,
    parse_pilot_response,
    render_pilot_followup_user_message,
    render_pilot_initial_user_message,
    render_pilot_system_message,
    sample_pilot_selection,
    softmax_probabilities,
)
from behaviors.implementations.models.retries import (
    DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    generate_with_retries,
)
from behaviors.interfaces.context_policies import (
    SerializableUserSimulator,
    SerializableUserSimulatorSession,
)
from behaviors.interfaces.model_apis import ModelAPI, ModelCallFailedError
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    ContentReasoning,
    ContentSummarizedReasoning,
    ContentText,
)
from behaviors.types.message import AssistantMessage, ChatMessage, SystemMessage, UserMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.response import ModelRequest, ModelResponse, ReasoningParams, SamplingParams
from behaviors.types.retry import RetryConfig
from behaviors.types.rollout import EndRollout, UserTurn, find_metadata
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

logger = logging.getLogger(__name__)

PILOT_RESPONSE_MAX_ATTEMPTS = 3
"""Pilot calls per choice point before a malformed pilot response fails the turn."""


def _default_pilot_sampling() -> SamplingParams:
    return SamplingParams(max_tokens=8192, reasoning=ReasoningParams(budget_tokens=4096))


@dataclasses.dataclass(frozen=True)
class PilotSteeredUserSimulatorConfig(
    TaggedDataclass, fxtr_type=behaviors_type("PilotSteeredUserSimulatorConfig.v1")
):
    """Everything about pilot steering except the pilot model and the base simulator.

    Attributes:
        user_bio: The simulated user's vignette, shown to the pilot at its first call.
        pilot_instructions: Steering guidance shown only to the pilot, if any.
        candidate_count: Candidates sampled from the base simulator per choice point.
        allow_custom_text: Whether the pilot may write the user's message itself. A
            response that supplies custom text anyway counts as malformed.
        steer_first_turn: Whether the pilot is consulted for the user's opening message
            (a choice point at an empty transcript). Set False when the base simulator's
            opening message is fixed, e.g. a profile with an ``initial_user_message``:
            there is nothing to steer, and the base's turn is taken as is.
        random_seed: Seed for candidate selection, shared by every session of the
            simulator (each choice point derives its own generator from it), or None
            for unseeded selection.
        pilot_sampling: Sampling parameters for pilot calls.
    """

    user_bio: str
    pilot_instructions: str | None = None
    candidate_count: int = 4
    allow_custom_text: bool = True
    steer_first_turn: bool = True
    random_seed: int | None = None
    pilot_sampling: SamplingParams = dataclasses.field(default_factory=_default_pilot_sampling)


@dataclasses.dataclass(frozen=True)
class _SessionState(TaggedDataclass, fxtr_type=behaviors_type("PilotSteeredUserSimulatorState.v1")):
    """A session's captured state.

    Attributes:
        base: The live base session's own state.
    """

    base: DynamicData
    transcript: tuple[ChatMessage, ...]
    pilot_history: tuple[ChatMessage, ...]
    last_transcript_length_shown: int
    last_steering_outcome: LastSteeringOutcome | None


@dataclasses.dataclass(frozen=True)
class PilotSteering(TaggedDataclass, fxtr_type=behaviors_type("PilotSteering.v1")):
    """Metadata of a steered turn: what the pilot was shown and what it chose.

    ``candidates`` and ``shifts`` cover the base candidates only; ``selected_index`` counts
    the custom-text slot after them, so ``selected_index == len(candidates)`` means custom
    text was chosen. ``selected_text`` is the raw selected option, before any end-marker
    handling.
    """

    transcript_length: int
    candidates: tuple[str, ...]
    shifts: tuple[float, ...]
    custom_text: str | None
    selected_index: int
    selected_text: str
    thinking: str | None
    pilot_messages: tuple[ChatMessage, ...]
    raw_response: str


@dataclasses.dataclass(frozen=True)
class _PilotCall:
    """One successfully parsed pilot model call."""

    user_content: str
    request_messages: tuple[ChatMessage, ...]
    raw_response: str
    thinking: str | None
    response: PilotResponse


def _candidate_text(turn: UserTurn | EndRollout) -> str:
    """How a candidate is shown to the pilot.

    A base simulator's own raw text (e.g. a base-model completion, end marker included) when
    it records one as ``SimulatedUserText``; otherwise the message text, or the end marker for
    a session that ended.
    """
    text = find_metadata(turn.metadata, SimulatedUserText)
    if text is not None:
        return text.raw_text
    if isinstance(turn, EndRollout):
        return END_TRANSCRIPT_MARKER
    return "".join(block.text for block in turn.message.content)


def _ended(turn: UserTurn | EndRollout) -> EndRollout:
    """The turn as an end of the rollout, keeping whatever metadata the base attached."""
    if isinstance(turn, EndRollout):
        return turn
    return EndRollout(metadata=turn.metadata)


def _response_texts(response: ModelResponse) -> tuple[str, str | None]:
    """The pilot's answer text and its reasoning text (None if it produced none)."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for message in response.output:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.content:
            if isinstance(block, ContentText):
                text_parts.append(block.text)
            elif isinstance(block, (ContentReasoning, ContentSummarizedReasoning)):
                thinking_parts.append(block.text)
    thinking = "".join(thinking_parts)
    return "".join(text_parts).strip(), thinking or None


class _AsyncClosable(Protocol):
    async def aclose(self) -> None: ...


class _SessionScope[S: _AsyncClosable]:
    """Owns sessions until they are released, and closes whatever it still owns on exit.

    The ownership pattern for keeping one session out of many: each session is ``add``ed
    the moment it is created, so a failure or cancellation anywhere before the choice
    closes them all; the chosen session is ``release``d and its predecessor ``add``ed in
    the same synchronous step, so no session is ever without an owner. On exit every
    session still owned is closed under a cancellation shield, each close attempted even
    if another fails; a failing close is raised only if the body itself succeeded, and is
    logged otherwise.

    Written for the pilot's candidate sessions but not specific to them: vine sampling, or
    resampling a turn from a restored session, would own their sessions the same way.
    """

    def __init__(self) -> None:
        self._owned: list[S] = []

    def add(self, session: S) -> S:
        self._owned.append(session)
        return session

    def release(self, session: S) -> S:
        for index, owned in enumerate(self._owned):
            if owned is session:
                del self._owned[index]
                return session
        raise ValueError("the session is not owned by this scope")

    async def __aenter__(self) -> "_SessionScope[S]":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        first_failure: Exception | None = None
        with anyio.CancelScope(shield=True):
            for session in self._owned:
                try:
                    await session.aclose()
                except Exception as close_failure:
                    if first_failure is None:
                        first_failure = close_failure
                    else:
                        logger.warning("closing a session failed: %r", close_failure)
        self._owned.clear()
        if first_failure is None:
            return
        if exc is None:
            raise first_failure
        logger.warning("closing a session failed while handling %r: %r", exc, first_failure)


async def _take_turns(
    sessions: list[SerializableUserSimulatorSession],
    model_messages: tuple[ChatMessage, ...] | None,
) -> list[UserTurn | EndRollout]:
    """One turn from each session, concurrently; if any fails, the first failure propagates."""
    turns: dict[int, UserTurn | EndRollout] = {}

    async def take(index: int, session: SerializableUserSimulatorSession) -> None:
        turns[index] = await session.next_user_turn(model_messages)

    try:
        async with anyio.create_task_group() as task_group:
            for index, session in enumerate(sessions):
                task_group.start_soon(take, index, session)
    except BaseExceptionGroup as group:
        raise _first_leaf(group)
    return [turns[index] for index in range(len(sessions))]


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    exceptions: tuple[BaseException, ...] = group.exceptions
    first = exceptions[0]
    if isinstance(first, BaseExceptionGroup):
        return _first_leaf(cast(BaseExceptionGroup[BaseException], first))
    return first


class PilotSteeredUserSimulatorSession:
    """One rollout's worth of pilot-steered user messages."""

    def __init__(
        self,
        simulator: "PilotSteeredUserSimulator",
        base_session: SerializableUserSimulatorSession,
        transcript: tuple[ChatMessage, ...],
        pilot_history: tuple[ChatMessage, ...],
        last_transcript_length_shown: int,
        last_steering_outcome: LastSteeringOutcome | None,
    ):
        self._simulator = simulator
        self._base_session = base_session
        self._transcript = transcript
        self._pilot_history = pilot_history
        self._last_transcript_length_shown = last_transcript_length_shown
        self._last_steering_outcome = last_steering_outcome

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        if model_messages is not None:
            self._transcript = self._transcript + model_messages
        if user_turn_override is not None:
            turn = await self._base_session.next_user_turn(
                model_messages, user_turn_override=user_turn_override
            )
            return self._record(turn)

        config = self._simulator.config
        if not self._transcript and not config.steer_first_turn:
            return self._record(await self._base_session.next_user_turn(model_messages))
        base_state = await self._base_session.get_state()
        async with _SessionScope[SerializableUserSimulatorSession]() as scope:
            sessions = [
                scope.add(await self._simulator.base.restore_session(base_state))
                for _ in range(config.candidate_count)
            ]
            turns = await _take_turns(sessions, model_messages)
            candidates = tuple(_candidate_text(turn) for turn in turns)
            call = await self._consult_pilot(candidates)
            augmented_shifts = augmented_shifts_for_selection(
                call.response, candidate_count=len(candidates)
            )
            selected_index = sample_pilot_selection(shifts=augmented_shifts, rng=self._rng())
            custom_text_used = selected_index == len(candidates)
            selected_text = (
                call.response.custom_text if custom_text_used else candidates[selected_index]
            )
            assert selected_text is not None  # the custom-text slot exists only when it is set

            chosen: SerializableUserSimulatorSession | None = None
            turn: UserTurn | EndRollout
            if ends_transcript(selected_text):
                turn = EndRollout() if custom_text_used else _ended(turns[selected_index])
            elif not custom_text_used:
                chosen, turn = sessions[selected_index], turns[selected_index]
            else:
                chosen = scope.add(await self._simulator.base.restore_session(base_state))
                turn = await chosen.next_user_turn(
                    model_messages,
                    user_turn_override=UserMessage(
                        content=(ContentText(text=selected_text.strip()),)
                    ),
                )

            # No awaits from here on, so a cancellation cannot leave the state half-updated.
            record = PilotSteering(
                transcript_length=len(self._transcript),
                candidates=candidates,
                shifts=call.response.shifts,
                custom_text=call.response.custom_text,
                selected_index=selected_index,
                selected_text=selected_text,
                thinking=call.thinking,
                pilot_messages=call.request_messages,
                raw_response=call.raw_response,
            )
            self._pilot_history = (
                *self._pilot_history,
                UserMessage(content=(ContentText(text=call.user_content),)),
                AssistantMessage(content=(ContentText(text=call.raw_response),)),
            )
            self._last_transcript_length_shown = len(self._transcript)
            self._last_steering_outcome = LastSteeringOutcome(
                shifts=augmented_shifts,
                softmax_probabilities=softmax_probabilities(augmented_shifts),
                selected_index=selected_index,
                selected_text=selected_text,
                custom_text_used=custom_text_used,
            )
            if chosen is not None:
                scope.release(chosen)
                scope.add(self._base_session)
                self._base_session = chosen
            return self._record(turn, record)

    def _record(
        self, turn: UserTurn | EndRollout, record: PilotSteering | None = None
    ) -> UserTurn | EndRollout:
        """Append the turn to the transcript and attach the steering record, if any."""
        metadata = turn.metadata
        if record is not None:
            if find_metadata(metadata, PilotSteering) is not None:
                raise ValueError("the base simulator's turn metadata already has a PilotSteering")
            metadata = (*metadata, DynamicData.from_tagged(record))
        if isinstance(turn, EndRollout):
            return EndRollout(metadata=metadata)
        self._transcript = (*self._transcript, turn.message)
        return UserTurn(message=turn.message, metadata=metadata)

    def _rng(self) -> random.Random:
        """A fresh generator per choice point, from the seed and the choice point's ordinal."""
        seed = self._simulator.config.random_seed
        if seed is None:
            return random.Random()
        return random.Random(f"{seed}:{len(self._pilot_history)}")

    async def _consult_pilot(self, candidates: tuple[str, ...]) -> _PilotCall:
        """Call the pilot and parse its response, re-sampling when the response is malformed.

        A malformed response is re-sampled from the same prompt (nothing is fed back to the
        pilot), at most ``PILOT_RESPONSE_MAX_ATTEMPTS`` times in total; the last failure's
        ``ValueError`` propagates.
        """
        config = self._simulator.config
        system_text = render_pilot_system_message(
            allow_custom_text=config.allow_custom_text, candidate_count=config.candidate_count
        )
        if not self._pilot_history:
            user_content = render_pilot_initial_user_message(
                user_bio=config.user_bio,
                pilot_instructions=config.pilot_instructions,
                transcript=self._transcript,
                candidates=candidates,
            )
        else:
            assert self._last_steering_outcome is not None  # set with the first history entry
            user_content = render_pilot_followup_user_message(
                transcript=self._transcript,
                new_messages=self._transcript[self._last_transcript_length_shown :],
                candidates=candidates,
                last_steering_outcome=self._last_steering_outcome,
            )
        request_messages: tuple[ChatMessage, ...] = (
            SystemMessage(
                content=(CacheableContentText(text=system_text, cache_control=CacheControl()),)
            ),
            *self._pilot_history,
            UserMessage(
                content=(CacheableContentText(text=user_content, cache_control=CacheControl()),)
            ),
        )
        request = ModelRequest(
            context=request_messages,
            tools=(),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=config.pilot_sampling,
        )
        attempt = 1
        while True:
            response = await generate_with_retries(
                self._simulator.pilot_model,
                request,
                retry=self._simulator.retry,
                failure_categories_to_retry=DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
            )
            if not isinstance(response, ModelResponse):
                raise ModelCallFailedError(response)
            raw_response, thinking = _response_texts(response)
            try:
                parsed = parse_pilot_response(
                    raw_response,
                    expected_candidate_count=len(candidates),
                    allow_custom_text=config.allow_custom_text,
                )
            except ValueError as exc:
                if attempt >= PILOT_RESPONSE_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "Pilot response failed to parse (attempt %d of %d): %s",
                    attempt,
                    PILOT_RESPONSE_MAX_ATTEMPTS,
                    exc,
                )
                attempt += 1
                continue
            return _PilotCall(
                user_content=user_content,
                request_messages=request_messages,
                raw_response=raw_response,
                thinking=thinking,
                response=parsed,
            )

    async def get_state(self) -> DynamicData:
        state = _SessionState(
            base=await self._base_session.get_state(),
            transcript=self._transcript,
            pilot_history=self._pilot_history,
            last_transcript_length_shown=self._last_transcript_length_shown,
            last_steering_outcome=self._last_steering_outcome,
        )
        return DynamicData.from_tagged(state)

    async def aclose(self) -> None:
        await self._base_session.aclose()


class PilotSteeredUserSimulator:
    """A ``SerializableUserSimulator`` that steers another one with a pilot model.

    Attributes are fixed at construction: the base simulator, the pilot ``ModelAPI``, the
    config, and the retry schedule for pilot calls (``ModelCallFailure`` categories in
    ``DEFAULT_FAILURE_CATEGORIES_TO_RETRY``).
    """

    def __init__(
        self,
        base: SerializableUserSimulator,
        pilot_model: ModelAPI,
        config: PilotSteeredUserSimulatorConfig,
        *,
        retry: RetryConfig | None = None,
    ):
        if config.candidate_count < 1:
            raise ValueError("candidate_count must be at least 1")
        self.base = base
        self.pilot_model = pilot_model
        self.config = config
        self.retry = retry

    async def start_session(self) -> PilotSteeredUserSimulatorSession:
        return PilotSteeredUserSimulatorSession(
            self, await self.base.start_session(), (), (), 0, None
        )

    async def restore_session(self, state: DynamicData) -> PilotSteeredUserSimulatorSession:
        decoded = state.as_type(_SessionState)
        return PilotSteeredUserSimulatorSession(
            self,
            await self.base.restore_session(decoded.base),
            decoded.transcript,
            decoded.pilot_history,
            decoded.last_transcript_length_shown,
            decoded.last_steering_outcome,
        )
