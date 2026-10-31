"""Context policies written as async generators.

Each ``yield`` produces the next context turn (or the ``EndRollout`` that
finishes the rollout), and the model's reply arrives as the value of the
``yield`` expression — the natural form for policies whose logic is a
straight-line conversation script with local state. Subclass
``GeneratorContextPolicy`` for a configurable, reusable policy;
``context_policy_from_generator`` wraps a bare async generator function for
inline one-offs. A suspended generator frame cannot be captured as state, so
neither form yields a ``SerializableContextPolicy`` — policies that need
checkpointing implement the session protocol directly.
"""

import abc
from collections.abc import AsyncGenerator, Callable

from behaviors.interfaces.context_policies import ContextPolicy, ContextPolicySession
from behaviors.types.rollout import ContextTurn, EndRollout, ModelTurn, SessionInitialization

type ContextPolicyGenerator = AsyncGenerator[ContextTurn | EndRollout, ModelTurn | None]
"""What a generator-based context policy yields and receives.

Yield the next context turn and receive the model's turn in response (None
arrives at the first yield, before the model has taken a turn). End the
rollout by yielding an ``EndRollout`` — async generators cannot return
values, so the end is yielded like any other step — after which nothing more
is sent in.
"""


class _GeneratorPolicySession:
    def __init__(
        self,
        generator: ContextPolicyGenerator,
        initialization: SessionInitialization | None,
    ):
        self._generator = generator
        self._initialization = initialization

    async def generate_initialization(self) -> SessionInitialization | None:
        return self._initialization

    async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn | EndRollout:
        try:
            return await self._generator.asend(model_turn)
        except StopAsyncIteration:
            raise RuntimeError(
                "the context policy generator finished without yielding an EndRollout"
            ) from None

    async def aclose(self) -> None:
        await self._generator.aclose()


class GeneratorContextPolicy(abc.ABC):
    """Base class for context policies written as async generator methods.

    Subclasses implement ``generate_turns`` and put configuration on
    themselves (e.g. as dataclass fields); a policy whose sessions need an
    initialization overrides ``generate_initialization``. One policy serves
    many sessions, so per-rollout state belongs in ``generate_turns``'
    locals, not on ``self``.
    """

    async def generate_initialization(self) -> SessionInitialization | None:
        """The initialization for each session's model; None unless overridden."""
        return None

    @abc.abstractmethod
    def generate_turns(self) -> ContextPolicyGenerator:
        """Return a fresh generator producing one rollout's context turns.

        Called once per session. An async generator method is the natural
        implementation; see ``ContextPolicyGenerator`` for the yield/send
        contract. Closing the session closes the generator, so its
        ``finally`` blocks run even when the rollout is cut off early
        (``max_turns``, a recorded failure).
        """
        ...

    async def start_session(self) -> ContextPolicySession:
        return _GeneratorPolicySession(self.generate_turns(), await self.generate_initialization())


class _GeneratorPolicy:
    def __init__(
        self,
        generator_factory: Callable[[], ContextPolicyGenerator],
        initialization: SessionInitialization | None,
    ):
        self._generator_factory = generator_factory
        self._initialization = initialization

    async def start_session(self) -> _GeneratorPolicySession:
        return _GeneratorPolicySession(self._generator_factory(), self._initialization)


def context_policy_from_generator(
    generator_factory: Callable[[], ContextPolicyGenerator],
    *,
    initialization: SessionInitialization | None,
) -> ContextPolicy:
    """Make a context policy from a bare async generator function.

    For inline one-offs where the initialization is known at construction;
    a configurable, reusable policy should subclass ``GeneratorContextPolicy``
    instead. ``generator_factory`` is called once per rollout to produce a
    fresh generator (a no-argument async generator function is one; bind
    configuration with a closure or ``functools.partial``). Closing the
    session closes the generator, so ``finally`` blocks in the generator run
    even when the rollout is cut off early (``max_turns``, a recorded
    failure).
    """
    return _GeneratorPolicy(generator_factory, initialization)
