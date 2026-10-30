import abc
import typing
import weakref
from typing import Sequence

from fxtr.object_model import core


class SequenceProcessor[A: core.EntityBase, B: core.EntityBase](
    core.FxtrCallTarget,
    interface_id="SequenceProcessor^1",
    namespace_id="!testing.impl_debug_stuff",
    stateless=True,
):
    """Protocol for processing sequences of entities."""

    @abc.abstractmethod
    async def process_seq(self, some_list_of_entities: Sequence[A]) -> Sequence[B]:
        """Process a list of entities and return a new list."""
        ...


class SimpleWrapperImplConfig(
    core.DataEntityBase,
    namespace_id="!testing.impl_debug_stuff",
    type_id="SimpleWrapperImplConfig^1",
):
    foo: str


# SimpleWrapperImpl directly identifies itself as implementing the SequenceProcessor protocol.
# It will be passed dependencies as the first argument, but they are not used, so we ignore them
# and do a /,* dance to allow not passing this arg.
class SimpleWrapperImpl(SequenceProcessor[core.AnyEntity, core.AdHocMetadata]):
    """docstring"""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        _unused_resolver: core.PlanResolver | None = None,
        /,
        *,
        config: SimpleWrapperImplConfig,
    ):
        assert isinstance(config, SimpleWrapperImplConfig)
        self._config = config

    async def process_seq(
        self, some_list_of_entities: Sequence[core.AnyEntity]
    ) -> Sequence[core.AdHocMetadata]:
        new_seq = [
            core.AdHocMetadata(core.freeze({"foo": self._config.foo})),
            core.AdHocMetadata(core.freeze({"list_of_entities": some_list_of_entities})),
        ]
        return new_seq


class RepeaterImplConfig[A: core.EntityBase, B: core.EntityBase](
    core.DataEntityBase,
    namespace_id="!testing.impl_debug_stuff",
    type_id="RepeaterImplConfig^1",
):
    inner: core.Plan[SequenceProcessor[A, B]]
    repeats: int


# RepeaterImpl directly implements the protocol with a dynamic interface spec
class RepeaterImpl[A: core.EntityBase, B: core.EntityBase](SequenceProcessor[A, B]):
    """docstring"""

    def __init__(self, inner_processor: SequenceProcessor[A, B], repeats: int):
        self._inner_processor = inner_processor
        self._repeats = repeats

    def interface_spec(self) -> core.InterfaceSpec[SequenceProcessor[A, B]]:
        return typing.cast(
            core.InterfaceSpec[SequenceProcessor[A, B]], self._inner_processor.interface_spec()
        )

    async def process_seq(self, some_list_of_entities: Sequence[A]) -> Sequence[B]:
        stuff = []
        for _ in range(self._repeats):
            stuff.extend(await self._inner_processor.process_seq(some_list_of_entities))
        return stuff


def repeater_impl_entrypoint(
    resolver: core.PlanResolver,
    /,
    config: RepeaterImplConfig,
) -> SequenceProcessor:
    assert isinstance(config, RepeaterImplConfig)
    inner_impl = resolver(config.inner)
    assert core.implements_interface(inner_impl, SequenceProcessor)
    return RepeaterImpl(inner_impl, config.repeats)


# New protocols for testing capability passing


class Calculator(
    core.FxtrCallTarget,
    interface_id="Calculator^1",
    namespace_id="!testing.capability_passing",
    stateless=True,
):
    """Protocol for simple calculator operations."""

    @abc.abstractmethod
    async def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        ...

    @abc.abstractmethod
    async def multiply(self, a: int, b: int) -> int:
        """Multiply two numbers."""
        ...

    @abc.abstractmethod
    async def get_name(self) -> str:
        """Get the name of this calculator."""
        ...


Calculator_spec = core.spec_of(Calculator)


class CalculatorFactory(
    core.FxtrCallTarget,
    interface_id="CalculatorFactory^1",
    namespace_id="!testing.capability_passing",
    stateless=True,
):
    """Protocol for creating Calculator instances."""

    @abc.abstractmethod
    async def create_calculator(self, name: str) -> Calculator:
        """Create a new calculator instance."""
        ...

    @abc.abstractmethod
    async def create_multiple_calculators(self, count: int) -> list[Calculator]:
        """Create multiple calculator instances."""
        ...

    @abc.abstractmethod
    async def get_shared_calculator(self) -> Calculator:
        """Get a shared calculator instance (returns the same object each time)."""
        ...

    @abc.abstractmethod
    async def get_shared_calculator_list(self, count: int) -> list[Calculator]:
        """Get a list containing multiple references to the same shared calculator."""
        ...

    @abc.abstractmethod
    async def clear_shared_calculator(self) -> None:
        """Clear the shared calculator reference so it can be freed."""
        ...


CalculatorFactory_spec = core.spec_of(CalculatorFactory)


# Track finalization for testing
_finalization_tracker: dict[str, list[str]] = {}


def track_finalization(obj_id: str, tracker_key: str) -> None:
    """Track when an object is finalized."""
    if tracker_key not in _finalization_tracker:
        _finalization_tracker[tracker_key] = []
    _finalization_tracker[tracker_key].append(obj_id)


def get_finalized_objects(tracker_key: str) -> list[str]:
    """Get list of finalized objects for a tracker key."""
    return _finalization_tracker.get(tracker_key, [])


def clear_finalization_tracker(tracker_key: str) -> None:
    """Clear finalization tracker for a key."""
    if tracker_key in _finalization_tracker:
        del _finalization_tracker[tracker_key]


class CalculatorImpl(Calculator):
    """Implementation of Calculator protocol."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, name: str, tracker_key: str | None = None):
        self._name = name
        self._tracker_key = tracker_key
        if tracker_key:
            weakref.finalize(self, track_finalization, name, tracker_key)

    async def add(self, a: int, b: int) -> int:
        return a + b

    async def multiply(self, a: int, b: int) -> int:
        return a * b

    async def get_name(self) -> str:
        return self._name


class CalculatorFactoryImpl(CalculatorFactory):
    """Implementation of CalculatorFactory protocol."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, tracker_key: str | None = None):
        self._tracker_key = tracker_key
        self._shared_calculator: Calculator | None = CalculatorImpl("shared", tracker_key)
        self._calc_counter = 0

    async def create_calculator(self, name: str) -> Calculator:
        """Create a new calculator instance."""
        return CalculatorImpl(name, self._tracker_key)

    async def create_multiple_calculators(self, count: int) -> list[Calculator]:
        """Create multiple calculator instances."""
        calculators = []
        for i in range(count):
            self._calc_counter += 1
            calculators.append(CalculatorImpl(f"calc_{self._calc_counter}", self._tracker_key))
        return calculators

    async def get_shared_calculator(self) -> Calculator:
        """Get a shared calculator instance (returns the same object each time)."""
        if self._shared_calculator is None:
            raise RuntimeError("Shared calculator has been cleared")
        return self._shared_calculator

    async def get_shared_calculator_list(self, count: int) -> list[Calculator]:
        """Get a list containing multiple references to the same shared calculator."""
        if self._shared_calculator is None:
            raise RuntimeError("Shared calculator has been cleared")
        return [self._shared_calculator] * count

    async def clear_shared_calculator(self) -> None:
        """Clear the shared calculator reference so it can be freed."""
        self._shared_calculator = None
