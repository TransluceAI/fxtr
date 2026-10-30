"""OLD OUTDATED exploration of protocol spec typing."""

from __future__ import annotations

import abc
import dataclasses
import typing
from typing import Any, Callable, Protocol, TypeIs


@dataclasses.dataclass(frozen=True)
class ProtocolSpec[T]:
    """The specification of a protocol (simplified)."""

    name: str
    doc: str

    def _phantom(self, val: T) -> T:
        raise NotImplementedError


PROTOCOL_REGISTRY: dict[type[Any], ProtocolSpec] = {}


def define_protocol[Cls: type](name: str) -> Callable[[Cls], Cls]:
    """Decorator to define a protocol."""

    def decorator(proto_cls: Cls) -> Cls:
        PROTOCOL_REGISTRY[proto_cls] = ProtocolSpec(
            name=name, doc=proto_cls.__doc__ or "No docstring"
        )
        return proto_cls

    return decorator


def protocol_spec[T](proto_cls: type[T]) -> ProtocolSpec[T]:
    return PROTOCOL_REGISTRY[proto_cls]


# class FxtrActor(abc.ABC):
#     """Base class for Hub-protocol implementation objects.

#     All Hub-protocol implementation objects are expected to subclass this class. Implementations
#     directly expose their protocol methods and can be called like normal Python objects.
#     """

#     @abc.abstractmethod
#     def protocol_spec(self) -> Spec[Self]:
#         """Get the protocol that this implementation implements."""
#         raise NotImplementedError


class Plan[T](Protocol):
    @property
    def protocol_spec(self) -> ProtocolSpec[T]: ...


@dataclasses.dataclass(frozen=True)
class FakeImplPlan[T]:
    protocol_spec: ProtocolSpec[T]
    key: str


class HasProtocolSpec(Protocol):

    def protocol_spec(self) -> ProtocolSpec:
        """Get the protocol that this implementation implements."""
        ...


@define_protocol("SomeFooProtocol^1")
class SomeFooProtocol(HasProtocolSpec, Protocol):
    async def some_method(self) -> int: ...


@define_protocol("SomeBarProtocol^1")
class SomeBarProtocol(HasProtocolSpec, Protocol):
    async def some_other_method(self) -> int: ...


class FxtrProtocolImpl(abc.ABC):

    @abc.abstractmethod
    def protocol_spec(self) -> ProtocolSpec:
        """Get the protocol that this implementation implements."""
        ...


class SomeFooImpl(FxtrProtocolImpl):  # (SomeFooProtocol, FxtrTarget[SomeFooProtocol]):
    def protocol_spec(self) -> ProtocolSpec[SomeFooProtocol]:
        return protocol_spec(SomeFooProtocol)

    async def some_method(self) -> int:
        return 42


class SomeBarImpl(FxtrProtocolImpl):  # (SomeBarProtocol, FxtrTarget[SomeBarProtocol]):
    def protocol_spec(self) -> ProtocolSpec[SomeBarProtocol]:
        return protocol_spec(SomeBarProtocol)

    async def some_method(self) -> int:
        return 42


def make_foo_impl() -> SomeFooProtocol:
    return SomeFooImpl()


def is_plan_for[T](plan: Plan[Any], proto_cls: type[T]) -> TypeIs[Plan[T]]:
    return plan.protocol_spec == protocol_spec(proto_cls)


def is_impl_for[T: HasProtocolSpec](impl: HasProtocolSpec, proto_cls: type[T]) -> TypeIs[T]:
    return impl.protocol_spec() == protocol_spec(proto_cls)


def provide_fake[T](plan: FakeImplPlan[T]) -> T:
    if is_plan_for(plan, SomeFooProtocol):
        impl = SomeFooImpl()
        return typing.cast(T, impl)
    elif plan.protocol_spec == protocol_spec(SomeBarProtocol):
        return typing.cast(T, SomeBarImpl())
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")


def provide_magic[T](plan: Plan[T]) -> T:
    if isinstance(plan, FakeImplPlan):
        return provide_fake(plan)
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")


class Resolver(Protocol):
    def __call__[T](self, plan: Plan[T]) -> T: ...


async def use_provider_known(plan: Plan[SomeFooProtocol], resolver: Resolver) -> int:
    obj = resolver(plan)
    val = await obj.some_method()
    return val


async def use_provider_dyn(plan: Plan, resolver: Resolver) -> int:
    obj = resolver(plan)
    if is_impl_for(obj, SomeFooProtocol):
        val = await obj.some_method()
    elif is_impl_for(obj, SomeBarProtocol):
        val = await obj.some_other_method()
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")
    return val
