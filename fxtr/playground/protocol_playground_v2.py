from __future__ import annotations

import abc
import dataclasses
import types
import typing
from types import NotImplementedType
from typing import Any, Callable, ClassVar, Protocol, TypeGuard


# simplified version of protocol spec; in real impl this should still be an entity like it is now
@dataclasses.dataclass(frozen=True)
class ProtocolSpec[T]:
    """The specification of a protocol (simplified)."""

    type_id: str
    doc: str
    methods: list[str]

    if typing.TYPE_CHECKING:

        def _fxtr_phantom(self) -> T:
            """Phantom method to ensure ProtocolSpec is invariant over T."""
            raise NotImplementedError


# base class for call targets; replaces define_protocol and FxtrProtocolImpl;
# instead now the "protocol" equivalent is just a direct subclass of FxtrCallTarget and the
# implementation is a subclass of that subclass.
class FxtrCallTarget(abc.ABC):
    protocol_spec: ClassVar[ProtocolSpec]

    def __init_subclass__(cls, *, type_id: str | None | NotImplementedType = None, **kwargs):
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "protocol_spec"):
            if "protocol_spec" in cls.__dict__ and hasattr(super(), "protocol_spec"):
                raise TypeError(
                    f"Cannot override protocol_spec for {cls.__name__} because it has a superclass"
                    " with a protocol_spec!"
                )
            elif type_id is not None:
                raise TypeError(
                    f"Cannot provide type_id for {cls.__name__} because it already has a"
                    " protocol_spec!"
                )
            # TODO: make sure all of the methods on the protocol spec exist on the subclass,
            # although it is OK if they are still abstract.
            print(f"need to validate implementation {cls.__name__} of {cls.protocol_spec}")
        else:
            if type_id is NotImplemented:
                # internal helper base class
                return
            if type_id is None:
                raise TypeError(
                    f"Must provide type_id when defining a new type of FxtrCallTarget!\nNote that"
                    f" direct subclasses of FxtrCallTarget should be fully abstract and have a"
                    f" type_id identifying the interface they describe; only subclasses of these"
                    f" abstract types can be instantiated."
                )

            # TODO: validate this class only contains abstract methods and all are async.
            # Also validate that it has a protocol_spec annotated as ClassVar[ProtocolSpec[cls]]
            # where cls is the class itself. this makes sure that the typechecker can safely infer
            # that, if a subclass of FxtrCallTarget identifies as having a specific protocol spec,
            # then it must be a subclass of the abstract type that has that protocol spec.
            print(f"need to validate abstract methods on {cls.__name__}")
            # TODO also need to check for methods here
            cls.protocol_spec = ProtocolSpec(
                type_id=type_id,
                doc=cls.__doc__ or "No docstring",
                methods=list(cls.__dict__.keys()),  # <- not right yet
            )

            assert (
                "__subclasshook__" not in cls.__dict__
            ), f"__subclasshook__ already defined on {cls.__name__}"

            @classmethod
            def __subclasshook__(cls, subclass):
                if (
                    issubclass(subclass, FxtrCallTarget)
                    and hasattr(subclass, "protocol_spec")
                    and subclass.protocol_spec == cls.protocol_spec
                ):
                    return True
                return NotImplemented

            cls.__subclasshook__ = __subclasshook__


# Way to set up a call target that proxies to another call target. Logic like this can be used
# to set up RemoteImpls etc (we may want to cache the dynamic class per type though).
class ProxiedCallTarget(FxtrCallTarget, type_id=NotImplemented):
    def __init__(self, target: FxtrCallTarget):
        self._proxied_target = target


def _mk_method(method: str) -> Callable[..., Any]:
    return lambda self, *args, **kwargs: getattr(self._proxied_target, method)(*args, **kwargs)


def make_proxied_foo(target: Foo) -> Foo:
    def exec_body(dct):
        dct["protocol_spec"] = Foo.protocol_spec
        for method in Foo.protocol_spec.methods:
            dct[method] = _mk_method(method)

    Tmp = types.new_class("Tmp", (ProxiedCallTarget,), exec_body=exec_body)

    return Tmp(target)


# plans describe how to construct a call target
class Plan[T: FxtrCallTarget](abc.ABC):
    protocol_spec: ProtocolSpec[T]


# an example of a plan; we would use other plans in practice
@dataclasses.dataclass(frozen=True)
class FakeImplPlan[T: FxtrCallTarget](Plan[T]):
    protocol_spec: ProtocolSpec[T]
    key: str


# an example of an abstract call target. all existing uses of define_protocol should be replaced
# with this pattern.
class Foo(FxtrCallTarget, type_id="Foo^1"):
    protocol_spec: ClassVar[ProtocolSpec[Foo]]

    @abc.abstractmethod
    async def some_method(self) -> int: ...


# another equivalent call target; we treat them as mutual virtual subclasses
class FooEquivalentCopy(FxtrCallTarget, type_id="Foo^1"):
    protocol_spec: ClassVar[ProtocolSpec[Foo]]

    @abc.abstractmethod
    async def some_method(self) -> int: ...


# a different call target
class Bar(FxtrCallTarget, type_id="Bar^1"):
    protocol_spec: ClassVar[ProtocolSpec[Bar]]

    @abc.abstractmethod
    async def some_other_method(self) -> int: ...


# an implementation of one of the call targets
class FooImpl(Foo):
    async def some_method(self) -> int:
        return 42


# an implementation of the other call target
class BarImpl(Bar):
    async def some_other_method(self) -> int:
        return 43


# a helper function to check if a plan is for a specific call target
def is_plan_for[T: FxtrCallTarget](plan: Plan[Any], target_cls: type[T]) -> TypeGuard[Plan[T]]:
    return plan.protocol_spec == target_cls.protocol_spec


# dummy method that would provide something of known plan type
def provide_fake[T: FxtrCallTarget](plan: FakeImplPlan[T]) -> T:
    if is_plan_for(plan, Foo):
        impl = FooImpl()
        # typechecker cannot infer that T is Foo from Plan[T] == Plan[Foo]
        return typing.cast(T, impl)
    elif is_plan_for(plan, Bar):
        impl = BarImpl()
        return typing.cast(T, impl)
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")


# dummy method that would provide something of any plan type
def provide_magic[T: FxtrCallTarget](plan: Plan[T]) -> T:
    if isinstance(plan, FakeImplPlan):
        return provide_fake(plan)
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")


# something that can resolve a plan to an implementation
class Resolver(Protocol):
    def __call__[T: FxtrCallTarget](self, plan: Plan[T]) -> T: ...


# some usage examples


async def use_provider_known(plan: Plan[Foo], resolver: Resolver) -> int:
    obj = resolver(plan)
    val = await obj.some_method()
    return val


async def use_provider_dyn(plan: Plan[FxtrCallTarget], resolver: Resolver) -> int:
    obj = resolver(plan)
    if isinstance(obj, Foo):
        val = await obj.some_method()
    elif isinstance(obj, Bar):
        val = await obj.some_other_method()
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")
    return val


async def use_provider_dyn_2(plan: Plan[Any], resolver: Resolver) -> int:
    if is_plan_for(plan, Foo):
        obj = resolver(plan)
        val = await obj.some_method()
    elif is_plan_for(plan, Bar):
        obj = resolver(plan)
        val = await obj.some_other_method()
    else:
        raise ValueError(f"Unsupported plan: {plan.protocol_spec}")
    return val


async def main():
    resolver = provide_magic
    foo_plan = FakeImplPlan(protocol_spec=Foo.protocol_spec, key="foo")
    bar_plan = FakeImplPlan(protocol_spec=Bar.protocol_spec, key="bar")
    print(f"  {await use_provider_known(foo_plan, resolver)=}")

    if 1 == 0:
        # this is a type error and should be caught by the linter
        # (it is disabled at runtime)
        print(f"  {await use_provider_known(bar_plan, resolver)=}")

    for plan in [foo_plan, bar_plan]:
        print(f"{plan=}")
        print(f"  {await use_provider_dyn(plan, resolver)=}")
        print(f"  {await use_provider_dyn_2(plan, resolver)=}")

    thing = resolver(foo_plan)
    assert isinstance(thing, Foo)
    assert isinstance(thing, FooEquivalentCopy)
    assert not isinstance(thing, Bar)
    assert issubclass(Foo, FooEquivalentCopy)
    assert issubclass(FooEquivalentCopy, Foo)

    proxied_foo = make_proxied_foo(thing)
    print(f"{proxied_foo=}")
    print(f"  {await proxied_foo.some_method()=}")


if __name__ == "__main__":
    import anyio

    anyio.run(main)
