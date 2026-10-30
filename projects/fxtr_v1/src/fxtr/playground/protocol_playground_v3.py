from __future__ import annotations

import abc
import dataclasses
import types
import typing
from types import NotImplementedType
from typing import Any, Callable, ClassVar, TypeGuard, reveal_type


# Unbound definition
@dataclasses.dataclass(frozen=True)
class InterfaceDefn[T]:
    """The definition of a interface (simplified).

    T should be bound to the FxtrCallTarget type that this interface defn is for, without any
    bound type variables.
    """

    type_id: str
    doc: str
    methods: tuple[str, ...]
    type_vars: tuple[str, ...]

    def bind(self, **bindings: Any) -> InterfaceSpec:
        return InterfaceSpec(defn=self, bindings=bindings)


@dataclasses.dataclass(frozen=True)
class InterfaceSpec[T]:
    """The concrete specification of a interface with all type variables bound."""

    defn: InterfaceDefn
    bindings: dict[str, Any]


INFER_FROM_TYPE = typing.cast(Callable[..., InterfaceSpec[Any]], object())


def spec_of[T](ty: type[T]) -> InterfaceSpec[T]:
    origin = typing.get_origin(ty) or ty
    binding_names = getattr(origin, "__type_params__", ())
    args = typing.get_args(ty)
    if not issubclass(origin, FxtrCallTarget):
        raise TypeError(f"Type {ty.__name__} is not a subclass of FxtrCallTarget")
    if not hasattr(origin, "_fxtr_interface_defn"):
        raise TypeError(f"Type {ty.__name__} does not have a interface definition")
    defn = origin._fxtr_interface_defn
    if len(binding_names) != len(args):
        raise TypeError(
            f"Number of binding names ({len(binding_names)}) does not match number of"
            f" arguments ({len(args)}) for {ty.__name__}; cannot automatically infer"
            " interface spec from type."
        )
    return InterfaceSpec(
        defn=defn, bindings={str(binding_names[i]): args[i] for i in range(len(binding_names))}
    )


# base class for call targets; replaces define_interface and FxtrInterfaceImpl;
# instead now the "interface" equivalent is just a direct subclass of FxtrCallTarget and the
# implementation is a subclass of that subclass.
class FxtrCallTarget(abc.ABC):
    """Base class for callable types in fxtr.

    There are two types of call target class:
    - Abstract call targets, which define fxtr interfaces. These specify the methods, types, and
      associated rules for the object. All methods of an abstract call target (except
      interface_spec) must be abstract, async, and have arguments and a return type that are
      fxtr-sendable. Every abstract call target is associated with an interface_id, which (along
      with its namespace) uniquely identifies this type across the hub. Abstract call targets are
      allowed to have type parameters.
    - Concrete call targets, which implement a single abstract call target, and usually directly
      subclass the abstract call target they implement. Each instance of a concrete call target
      must be able to report its concrete interface type (the fxtr id of the interface definition
      along with any type variables) using the method `interface_spec`. Concrete call target types
      are allowed to have type variables, but each instance must be associated with a specific
      binding of those type variables.

    It is not a hard requirement that a concrete call target subclass the abstract call target it
    implements; the main purpose of doing so is to improve Python-side type checking. However, even
    if it is not directly a subclass, the concrete call target should be treated as a "virtual
    instance" of the abstract call target, and fxtr provides functions for casting it to the
    appropriate abstract type for the purposes of the Python type checker.
    """

    _fxtr_interface_defn: ClassVar[InterfaceDefn]

    @abc.abstractmethod
    def interface_spec(self) -> InterfaceSpec[FxtrCallTarget]: ...

    def __init_subclass__(
        cls, *, type_id: str | None | NotImplementedType = None, impl: bool = False, **kwargs
    ):
        super().__init_subclass__(**kwargs)

        # Case 1: a concrete subclass with a concrete interface spec.
        if cls.interface_spec is not FxtrCallTarget.interface_spec:
            # TODO fix this logic, does it make any sense?
            super_pc = cls.__bases__[0].interface_spec
            if super_pc is not FxtrCallTarget.interface_spec and cls.interface_spec is not super_pc:
                raise TypeError(
                    f"Cannot override interface_spec for {cls.__name__} because it has a superclass"
                    " with a different interface_spec!"
                )
            elif type_id is not None and type_id is not NotImplemented:
                raise TypeError(
                    f"Cannot provide type_id for {cls.__name__} because it already has a"
                    " interface_spec!"
                )
            print(f"need to validate implementation {cls.__name__} of {cls.interface_spec}")

            if cls.interface_spec is INFER_FROM_TYPE:
                print("Inferring interface spec from type. TODO make this more sophisticated.")
                defn = cls._fxtr_interface_defn
                for base in types.get_original_bases(cls):
                    # TODO catch if more than one
                    try:
                        spec = spec_of(base)
                        break
                    except TypeError:
                        continue
                else:
                    raise ValueError(
                        f"Could not find base class with interface spec {defn} for {cls.__name__}"
                    )
                cls.interface_spec = lambda self: spec
        # Case 2: a direct subclass that is defining a new interface.
        elif not hasattr(cls, "_fxtr_interface_defn"):
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
            # Also validate that it has a interface_spec annotated as ClassVar[InterfaceSpec[cls]]
            # where cls is the class itself. this makes sure that the typechecker can safely infer
            # that, if a subclass of FxtrCallTarget identifies as having a specific interface spec,
            # then it must be a subclass of the abstract type that has that interface spec.
            print(f"need to validate abstract methods on {cls.__name__}")
            # TODO also need to check for methods here
            cls._fxtr_interface_defn = InterfaceDefn(
                type_id=type_id,
                doc=cls.__doc__ or "No docstring",
                methods=tuple(cls.__dict__.keys()),  # <- not right yet
                type_vars=tuple(str(x) for x in cls.__type_params__),  # <- not quite right yet
            )


def interface_defn[T: FxtrCallTarget](cls: type[T]) -> InterfaceDefn[T]:
    return cls.__dict__["_fxtr_interface_defn"]


# Way to set up a call target that proxies to another call target. Logic like this can be used
# to set up RemoteImpls etc (we may want to cache the dynamic class per type though).
class ProxiedCallTarget[T: FxtrCallTarget](FxtrCallTarget, type_id=NotImplemented):
    def __init__(self, target: T):
        self._proxied_target = target


def _mk_method(method: str) -> Callable[..., Any]:
    return lambda self, *args, **kwargs: getattr(self._proxied_target, method)(*args, **kwargs)


def make_proxied_foo(target: Foo) -> Foo:
    def exec_body(dct):
        dct["interface_spec"] = _mk_method("interface_spec")
        for method in interface_defn(Foo).methods:
            dct[method] = _mk_method(method)

    Tmp = types.new_class("Tmp", (ProxiedCallTarget,), exec_body=exec_body)

    return Tmp(target)


# plans describe how to construct a call target
class Plan[T: FxtrCallTarget](abc.ABC):
    interface_spec: InterfaceSpec[T]


# an example of a plan; we would use other plans in practice
@dataclasses.dataclass(frozen=True)
class FakeImplPlan[T: FxtrCallTarget](Plan[T]):
    interface_spec: InterfaceSpec[T]
    key: str


# an example of an abstract call target. all existing uses of define_interface should be replaced
# with this pattern.
class Foo(FxtrCallTarget, type_id="Foo^1"):
    @abc.abstractmethod
    async def some_method(self) -> int: ...


# another equivalent call target; we treat them as effective subclasses for the typechecker even if
# they are not literally so in python
class FooEquivalentCopy(FxtrCallTarget, type_id="Foo^1"):
    @abc.abstractmethod
    async def some_method(self) -> int: ...


# a different call target
class Bar(FxtrCallTarget, type_id="Bar^1"):
    @abc.abstractmethod
    async def some_other_method(self) -> int: ...


# an implementation of one of the call targets
class FooImpl(Foo):
    interface_spec = INFER_FROM_TYPE

    async def some_method(self) -> int:
        return 42


# an implementation of the other call target
class BarImpl(Bar):
    interface_spec = INFER_FROM_TYPE

    async def some_other_method(self) -> int:
        return 43


# a generic interface
class Duplicator[T](FxtrCallTarget, type_id="Duplicator^1"):
    @abc.abstractmethod
    async def duplicate(self, value: T) -> tuple[T, T]: ...


# a concrete implementation
class IntDuplicator(Duplicator[int]):
    interface_spec = INFER_FROM_TYPE

    async def duplicate(self, value: int) -> tuple[int, int]:
        return value, 2 * value


class AnyDuplicator[T](Duplicator[T]):
    def __init__(self, ty: type[T]):
        self._ty = ty

    def interface_spec(self) -> InterfaceSpec[Duplicator[T]]:
        return spec_of(Duplicator[self._ty])
        # return interface_defn(Duplicator).bind(T=self._ty)

    async def duplicate(self, value: T) -> tuple[T, T]:
        return value, value


# a helper function to check if a plan is for a specific call target
def is_plan_for[T: FxtrCallTarget](plan: Plan[Any], target_cls: type[T]) -> TypeGuard[Plan[T]]:
    # TODO: this is incomplete
    return plan.interface_spec.defn == interface_defn(target_cls)
    # return plan.interface_spec == target_cls.interface_spec


def implements_spec[
    T: FxtrCallTarget
](impl: FxtrCallTarget, spec: InterfaceSpec[T]) -> TypeGuard[T]:
    return impl.interface_spec() == spec


def implements_interface[T: FxtrCallTarget](impl: FxtrCallTarget, abscls: type[T]) -> TypeGuard[T]:
    return impl.interface_spec() == spec_of(abscls)


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
        raise ValueError(f"Unsupported plan: {plan.interface_spec}")


# dummy method that would provide something of any plan type
def provide_magic[T: FxtrCallTarget](plan: Plan[T]) -> T:
    if isinstance(plan, FakeImplPlan):
        return provide_fake(plan)
    else:
        raise ValueError(f"Unsupported plan: {plan.interface_spec}")


# something that can resolve a plan to an implementation
class Resolver(Interface):
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
        raise ValueError(f"Unsupported plan: {plan.interface_spec}")
    return val


async def use_provider_dyn_2(plan: Plan[Any], resolver: Resolver) -> int:
    if is_plan_for(plan, Foo):
        obj = resolver(plan)
        val = await obj.some_method()
    elif is_plan_for(plan, Bar):
        obj = resolver(plan)
        val = await obj.some_other_method()
    else:
        raise ValueError(f"Unsupported plan: {plan.interface_spec}")
    return val


async def main():
    resolver = provide_magic
    foo_plan = FakeImplPlan(interface_spec=spec_of(Foo), key="foo")
    bar_plan = FakeImplPlan(interface_spec=spec_of(Bar), key="bar")
    print(f"{foo_plan=}")
    print(f"{bar_plan=}")
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
    assert implements_interface(thing, Foo)
    # assert isinstance(thing, FooEquivalentCopy)
    assert not isinstance(thing, Bar)
    # assert issubclass(Foo, FooEquivalentCopy)
    # assert issubclass(FooEquivalentCopy, Foo)

    proxied_foo = make_proxied_foo(thing)
    print(f"{proxied_foo=}")
    print(f"  {await proxied_foo.some_method()=}")

    def _check_intdup(intdup: Duplicator[int]):
        pass

    dup1 = IntDuplicator()
    _check_intdup(dup1)
    dup2 = AnyDuplicator(int)
    reveal_type(dup2)
    _check_intdup(dup2)

    print(f"{dup1.interface_spec()=}")
    print(f"{dup2.interface_spec()=}")

    dup3 = typing.cast(Any, dup2)
    assert implements_interface(dup1, Duplicator[int])
    assert implements_interface(dup2, Duplicator[int])
    assert implements_interface(dup3, Duplicator[int])
    reveal_type(dup3)


if __name__ == "__main__":
    import anyio

    anyio.run(main)
