"""Step-0 spike for substage 8 — wrapper-dataclass + mashumaro TypeVar propagation.

Validates the three risk items from part8_plan.md §4:

1. ``types.new_class`` (or ``make_dataclass`` with explicit ``Generic[*tvars]``
   bases) produces a class that mashumaro recognizes as parameterizable.
2. ``pre_decoder_func`` / ``post_encoder_func`` cleanly strip a Result wrapper.
3. The configured dialect threads through to nested ``EntityRef[T]``-style
   strategies inside the wrapper's fields, with the right ``T`` substituted.

Run with: ``uv run python plans/phase3/stage8c_user_caps/part8_spike.py``.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from typing import Any, Generic, TypeVar

import mashumaro
import mashumaro.codecs.basic
import mashumaro.dialect
import mashumaro.types

# ---------------------------------------------------------------------------
# A minimal stand-in for ``EntityRef[T]`` whose strategy strips/re-attaches T.
# ---------------------------------------------------------------------------


_T_FER = TypeVar("_T_FER")


class FakeEntityRef(Generic[_T_FER]):
    """Pretend EntityRef[T]. Generic so mashumaro can read the [T] off the field
    annotation and pass it through to the helper. NOT a dataclass — mashumaro
    descends into dataclass fields directly, bypassing the dialect's strategy.
    The real ``EntityRef`` in fxtr2 is also a plain class for the same reason.
    """

    def __init__(self, entity_id: str, target_type: type | None = None) -> None:
        self.entity_id = entity_id
        self.target_type = target_type

    def __repr__(self) -> str:
        return f"FakeEntityRef({self.entity_id!r}, target_type={self.target_type!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FakeEntityRef)
            and self.entity_id == other.entity_id
            and self.target_type == other.target_type
        )

    def __hash__(self) -> int:
        return hash((self.entity_id, self.target_type))


@dataclasses.dataclass(frozen=True)
class _FakeEntityRefHelper(Generic[_T_FER], mashumaro.types.GenericSerializableType):
    """Helper that captures the ``T`` from ``FakeEntityRef[T]`` at decode time.

    Mirrors the ``_InlineEntityDeserializationHelper`` /
    ``_EntityRefSerializationHelper`` pattern from
    ``fxtr2/conversion/converters/mashumaro_entities.py``.
    """

    entity_id: str
    target_type: type | None = None

    def _serialize(self, types: list[type]) -> str:  # noqa: A002
        return self.entity_id

    @classmethod
    def _deserialize(
        cls, value: Any, types: list[type]
    ) -> "_FakeEntityRefHelper[Any]":  # noqa: A002
        if not isinstance(value, str):
            raise TypeError(f"_FakeEntityRefHelper expected str, got {type(value).__name__}")
        target = types[0] if types else None
        return cls(entity_id=value, target_type=target)


class FakeEntityRefStrategy(mashumaro.types.SerializationStrategy, Generic[_T_FER]):
    """Dialect-level strategy: ``FakeEntityRef[T]`` ↔ ``_FakeEntityRefHelper[T]``."""

    def serialize(self, value: FakeEntityRef[_T_FER]) -> _FakeEntityRefHelper[_T_FER]:
        return _FakeEntityRefHelper(entity_id=value.entity_id)

    def deserialize(self, value: _FakeEntityRefHelper[_T_FER]) -> FakeEntityRef[_T_FER]:
        return FakeEntityRef(entity_id=value.entity_id, target_type=value.target_type)


# ---------------------------------------------------------------------------
# Two stand-in user types we'll embed in our wrapper's fields.
# ---------------------------------------------------------------------------


class Note:
    """Stand-in for an entity type."""


class OtherNote:
    """A second stand-in to confirm T substitution actually differs by call."""


# ---------------------------------------------------------------------------
# The custom dialect.
# ---------------------------------------------------------------------------


class FakeServiceDialect(mashumaro.dialect.Dialect):
    serialization_strategy = {
        FakeEntityRef: FakeEntityRefStrategy(),
    }


# ---------------------------------------------------------------------------
# Risk item 1: dynamic Generic dataclass via make_dataclass / types.new_class.
# ---------------------------------------------------------------------------


def build_wrapper_via_make_dataclass(
    name: str,
    fields: list[tuple[str, Any]],
    type_params: tuple[TypeVar, ...],
) -> type:
    """Generate a Generic-parameterized plain ``@dataclass`` via ``make_dataclass``.

    Uses the *same TypeVar objects* as ``type_params`` so passing
    ``Wrapper[int]`` substitutes correctly throughout the wrapper's fields.

    NOTE: not a ``DataClassDictMixin`` subclass — that mixin compiles its
    unpacker eagerly at class creation, before the dialect can be supplied. We
    rely on ``BasicEncoder/Decoder(...)`` to compile against the dialect at
    codec-build time instead, so a plain dataclass is what we need.
    """
    bases: tuple[type, ...] = (Generic[*type_params],)  # type: ignore[valid-type]
    cls = dataclasses.make_dataclass(
        name,
        fields=[(n, ann) for n, ann in fields],
        bases=bases,
        frozen=True,
    )
    return cls


def build_wrapper_via_new_class(
    name: str,
    fields: list[tuple[str, Any]],
    type_params: tuple[TypeVar, ...],
) -> type:
    """Alternative path: ``types.new_class`` + ``@dataclass`` afterwards."""
    bases: tuple[type, ...] = (Generic[*type_params],)  # type: ignore[valid-type]

    def exec_body(ns: dict[str, Any]) -> None:
        ns["__annotations__"] = {n: ann for n, ann in fields}

    cls = types.new_class(name, bases=bases, exec_body=exec_body)
    cls = dataclasses.dataclass(frozen=True)(cls)
    return cls


# ---------------------------------------------------------------------------
# Risk item 2: pre_decoder_func / post_encoder_func to strip Result wrapper.
# ---------------------------------------------------------------------------


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        marker = "OK " if ok else "FAIL"
        print(f"[{marker}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # ---------- Risk 1 — Generic dynamic dataclass via make_dataclass ----------

    T = TypeVar("T")

    Wrapper = build_wrapper_via_make_dataclass(
        "Wrapper",
        fields=[("ref", FakeEntityRef[T]), ("count", int)],  # type: ignore[valid-type]
        type_params=(T,),
    )

    # Confirm Wrapper accepts subscript.
    try:
        param_form = Wrapper[Note]  # type: ignore[index]
        check(
            "make_dataclass: Generic-parameterizable",
            True,
            f"Wrapper[Note] = {param_form!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check("make_dataclass: Generic-parameterizable", False, repr(exc))
        return _summary(failures)

    # ---------- Risk 1b — same again via types.new_class ----------

    WrapperNC = build_wrapper_via_new_class(
        "WrapperNC",
        fields=[("ref", FakeEntityRef[T]), ("count", int)],  # type: ignore[valid-type]
        type_params=(T,),
    )
    try:
        WrapperNC[Note]  # type: ignore[index]
        check("types.new_class: Generic-parameterizable", True)
    except Exception as exc:  # noqa: BLE001
        check("types.new_class: Generic-parameterizable", False, repr(exc))

    # ---------- Risk 1c — encoder roundtrip with Wrapper[Note] ----------

    try:
        enc = mashumaro.codecs.basic.BasicEncoder(
            Wrapper[Note], default_dialect=FakeServiceDialect  # type: ignore[index]
        )
        dec = mashumaro.codecs.basic.BasicDecoder(
            Wrapper[Note], default_dialect=FakeServiceDialect  # type: ignore[index]
        )
    except Exception as exc:  # noqa: BLE001
        check("BasicEncoder/Decoder build for Wrapper[Note]", False, repr(exc))
        return _summary(failures)
    check("BasicEncoder/Decoder build for Wrapper[Note]", True)

    inst = Wrapper(ref=FakeEntityRef("e-1"), count=42)  # type: ignore[call-arg]
    encoded = enc.encode(inst)
    check(
        "encode produces dict with bare entity_id",
        isinstance(encoded, dict) and encoded.get("ref") == "e-1" and encoded.get("count") == 42,
        f"encoded = {encoded!r}",
    )

    decoded = dec.decode(encoded)
    # The decode side should re-attach Note via the GenericSerializableType helper.
    check(
        "decode round-trips ref",
        isinstance(decoded.ref, FakeEntityRef) and decoded.ref.entity_id == "e-1",
        f"decoded = {decoded!r}",
    )
    check(
        "Risk 3: T substituted to Note in decoded ref.target_type",
        decoded.ref.target_type is Note,
        f"target_type = {decoded.ref.target_type!r}",
    )

    # Build a parallel wrapper for OtherNote and confirm we get a different T.
    enc2 = mashumaro.codecs.basic.BasicEncoder(
        Wrapper[OtherNote], default_dialect=FakeServiceDialect  # type: ignore[index]
    )
    dec2 = mashumaro.codecs.basic.BasicDecoder(
        Wrapper[OtherNote], default_dialect=FakeServiceDialect  # type: ignore[index]
    )
    encoded2 = enc2.encode(Wrapper(ref=FakeEntityRef("e-2"), count=7))  # type: ignore[call-arg]
    decoded2 = dec2.decode(encoded2)
    check(
        "Risk 3: independent param yields different target_type",
        decoded2.ref.target_type is OtherNote,
        f"target_type = {decoded2.ref.target_type!r}",
    )

    # ---------- Risk 2 — pre_decoder_func / post_encoder_func ----------

    # Build a "Result" wrapper that holds a single ``value`` field. Encode/decode
    # via post_encoder_func that strips ``{"value": x}`` → ``x`` and
    # pre_decoder_func that re-wraps ``x`` → ``{"value": x}``.

    ResultWrapper = build_wrapper_via_make_dataclass(
        "ResultWrapper",
        fields=[("value", FakeEntityRef[T])],  # type: ignore[valid-type]
        type_params=(T,),
    )

    try:
        ret_enc = mashumaro.codecs.basic.BasicEncoder(
            ResultWrapper[Note],  # type: ignore[index]
            default_dialect=FakeServiceDialect,
            post_encoder_func=lambda d: d["value"],  # type: ignore[index]
        )
        ret_dec = mashumaro.codecs.basic.BasicDecoder(
            ResultWrapper[Note],  # type: ignore[index]
            default_dialect=FakeServiceDialect,
            pre_decoder_func=lambda v: {"value": v},
        )
    except Exception as exc:  # noqa: BLE001
        check("ResultWrapper codecs build with pre/post hooks", False, repr(exc))
        return _summary(failures)
    check("ResultWrapper codecs build with pre/post hooks", True)

    bare_encoded = ret_enc.encode(ResultWrapper(value=FakeEntityRef("r-1")))  # type: ignore[call-arg]
    check(
        "post_encoder_func strips Result wrapper to bare value",
        bare_encoded == "r-1",
        f"bare_encoded = {bare_encoded!r}",
    )

    bare_decoded = ret_dec.decode("r-1")
    check(
        "pre_decoder_func re-wraps + decodes",
        isinstance(bare_decoded.value, FakeEntityRef)
        and bare_decoded.value.entity_id == "r-1"
        and bare_decoded.value.target_type is Note,
        f"bare_decoded = {bare_decoded!r}",
    )

    # ---------- Bonus: shared TypeVar object survives across wrappers ----------

    # Confirm the wrapper's __type_params__ / __parameters__ contain the *same*
    # TypeVar object we passed in. (Important for the eventual fan-out from a
    # single owner-class type-param set into per-method wrappers.)
    type_params_attr = getattr(Wrapper, "__type_params__", ()) or ()
    parameters_attr = getattr(Wrapper, "__parameters__", ()) or ()
    has_T = (len(type_params_attr) >= 1 and type_params_attr[0] is T) or (
        len(parameters_attr) >= 1 and parameters_attr[0] is T
    )
    check(
        "Wrapper exposes the same TypeVar object",
        has_T,
        f"__type_params__ = {type_params_attr!r}, __parameters__ = {parameters_attr!r}",
    )

    # ---------- Bonus: nested *args / **kwargs as tuple / dict fields ----------

    NestedWrapper = build_wrapper_via_make_dataclass(
        "NestedWrapper",
        fields=[
            ("a", int),
            ("_var_positional", tuple[FakeEntityRef[T], ...]),  # type: ignore[valid-type]
            ("_var_keyword", dict[str, FakeEntityRef[T]]),  # type: ignore[valid-type]
        ],
        type_params=(T,),
    )
    try:
        n_enc = mashumaro.codecs.basic.BasicEncoder(
            NestedWrapper[Note], default_dialect=FakeServiceDialect  # type: ignore[index]
        )
        n_dec = mashumaro.codecs.basic.BasicDecoder(
            NestedWrapper[Note], default_dialect=FakeServiceDialect  # type: ignore[index]
        )
        n_inst = NestedWrapper(  # type: ignore[call-arg]
            a=1,
            _var_positional=(FakeEntityRef("p-1"), FakeEntityRef("p-2")),
            _var_keyword={"k": FakeEntityRef("k-1")},
        )
        n_round = n_dec.decode(n_enc.encode(n_inst))
        check(
            "Nested *args / **kwargs fields round-trip",
            n_round.a == 1
            and len(n_round._var_positional) == 2
            and n_round._var_positional[0].target_type is Note
            and n_round._var_keyword["k"].target_type is Note,
            f"n_round = {n_round!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check("Nested *args / **kwargs fields round-trip", False, repr(exc))

    return _summary(failures)


def _summary(failures: list[str]) -> int:
    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All spike checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
