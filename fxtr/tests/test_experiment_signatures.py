"""Tests for step/workflow signatures, the run_output_type rule, the decorators, and pathname and
cache key formatting."""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import pytest

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import (
    Array,
    ArrayError,
    ArrayType,
    ArrayTypeSpec,
    Dim,
    ScalarArray,
    parse_dims,
)
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import as_schema, id_of, optional
from fxtr.experiment.annotations import AnnotatedType, declared_array_type
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.signatures import (
    FixedSignature,
    Signature,
    SignatureError,
    mapped_dims,
    replica_dims,
    resolve_signature,
    run_output_type,
)
from fxtr.experiment.steps import PLAIN_ARG, ArgConversion, Step, StepContext, step
from fxtr.experiment.workflows import (
    Workflow,
    WorkflowContext,
    append_mapped_key,
    join_path,
    workflow,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

# A step taking a scalar prompt and count and producing samples along a new dim.
SAMPLE = FixedSignature.of({"prompt": str, "n": int}, ("[sample: int]", str))
# A step reducing over a dim.
MEAN = FixedSignature.of({"scores": ("[sample: int]", float)}, float)


def types(**specs: ArrayTypeSpec) -> dict[str, ArrayType]:
    return {name: ArrayType.of(spec) for name, spec in specs.items()}


# ---- signatures ----


def test_fixed_signature_checks_names_and_assignability() -> None:
    assert SAMPLE.output_type(types(prompt=str, n=int)) == ArrayType.of(("[sample: int]", str))
    # Nullable covariance in the schema, and dim order does not matter.
    wide = FixedSignature.of({"t": ("[a: int, b: str]", float | None)}, int)
    assert wide.output_type(types(t=("[b: str, a: int]", float))) == ArrayType.of(int)
    with pytest.raises(SignatureError, match="Expected inputs"):
        SAMPLE.output_type(types(prompt=str))
    with pytest.raises(SignatureError, match="Expected inputs"):
        SAMPLE.output_type(types(prompt=str, n=int, extra=int))
    with pytest.raises(SignatureError, match="Input 'prompt'"):
        SAMPLE.output_type(types(prompt=str | None, n=int))
    with pytest.raises(SignatureError, match="Input 'prompt'"):
        SAMPLE.output_type(types(prompt=("[x: int]", str), n=int))


def test_resolve_signature_requires_exactly_one_form() -> None:
    assert resolve_signature(inputs={"a": int}, output=int) == FixedSignature.of({"a": int}, int)
    assert resolve_signature(signature=SAMPLE) is SAMPLE
    with pytest.raises(TypeError):
        resolve_signature()
    with pytest.raises(TypeError):
        resolve_signature(inputs={"a": int})
    with pytest.raises(TypeError):
        resolve_signature(inputs={"a": int}, output=int, signature=SAMPLE)


class Passthrough:
    """A signature generic over its single input's type."""

    def output_type(self, inputs: Mapping[str, ArrayType]) -> ArrayType:
        (only,) = inputs.values()
        return only


def test_custom_signature_satisfies_protocol() -> None:
    assert isinstance(Passthrough(), Signature)
    assert run_output_type(Passthrough(), types(x=("[a: int]", Sequence[int]))) == ArrayType.of(
        ("[a: int]", Sequence[int])
    )


# ---- run_output_type ----


def test_run_without_mapping_passes_arrays_whole() -> None:
    assert run_output_type(MEAN, types(scores=("[sample: int]", float))) == ArrayType.of(float)
    with pytest.raises(SignatureError, match="Input 'scores'"):
        run_output_type(MEAN, types(scores=float))


def test_run_mapped_prepends_mapped_dims_and_broadcasts() -> None:
    out = run_output_type(SAMPLE, types(prompt=("[prompt: int]", str), n=int), map_over=["prompt"])
    assert out == ArrayType.of(("[prompt: int, sample: int]", str))
    # Reduce over sample per prompt: the input keeps its core dim after mapping.
    out = run_output_type(
        MEAN, types(scores=("[prompt: int, sample: int]", float)), map_over=["prompt"]
    )
    assert out == ArrayType.of(("[prompt: int]", float))
    # Mapped dims come out in map_over order, whatever the inputs' order.
    out = run_output_type(
        MEAN,
        types(scores=("[sample: int, a: int, b: str]", float)),
        map_over=["b", "a"],
    )
    assert out == ArrayType.of(("[b: str, a: int]", float))


def test_run_replicas_add_new_dims_after_mapped_ones() -> None:
    out = run_output_type(
        SAMPLE,
        types(prompt=("[prompt: int]", str), n=int),
        map_over=["prompt"],
        replicas={"rep": [0, 1, 2], "split": ["a", "b"]},
    )
    assert out == ArrayType.of(("[prompt: int, rep: int, split: str, sample: int]", str))
    # Replicas alone, with nothing mapped over, still work.
    assert run_output_type(
        MEAN, types(scores=("[sample: int]", float)), replicas={"rep": [0, 1]}
    ) == ArrayType.of(("[rep: int]", float))
    assert replica_dims({"rep": (0, 1), "s": ("x",)}) == (Dim("rep", "int"), Dim("s", "str"))
    # A replica is a new dim: it may not be an input dim, a mapped dim, or an output dim.
    with pytest.raises(SignatureError, match="dims of the inputs"):
        run_output_type(MEAN, types(scores=("[sample: int]", float)), replicas={"sample": [0]})
    with pytest.raises(SignatureError, match="dims of the inputs"):
        run_output_type(
            MEAN,
            types(scores=("[prompt: int, sample: int]", float)),
            map_over=["prompt"],
            replicas={"prompt": [0]},
        )
    with pytest.raises(SignatureError, match="collide"):
        run_output_type(SAMPLE, types(prompt=str, n=int), replicas={"sample": [0]})
    # Values must be non-empty, of one key kind, and unique.
    with pytest.raises(SignatureError, match="Replica 'rep'"):
        replica_dims({"rep": []})
    with pytest.raises(SignatureError, match="Replica 'rep'"):
        replica_dims({"rep": [0, "a"]})
    with pytest.raises(SignatureError, match="unique"):
        replica_dims({"rep": [0, 0]})


def test_mapped_dims_resolution() -> None:
    inputs = types(x=("[a: int]", int), y=("[b: str]", int), z=int)
    assert [d.name for d in mapped_dims(inputs, ["b", "a"])] == ["b", "a"]
    with pytest.raises(SignatureError, match="unique"):
        mapped_dims(inputs, ["a", "a"])
    with pytest.raises(SignatureError, match="No input has dims"):
        mapped_dims(inputs, ["zzz"])
    with pytest.raises(SignatureError, match="kind"):
        mapped_dims(types(x=("[a: int]", int), y=("[a: str]", int)), ["a"])


def test_run_rejects_leftover_and_colliding_dims() -> None:
    # A dim that is neither mapped nor declared is an error.
    with pytest.raises(SignatureError, match="Input 'prompt'"):
        run_output_type(SAMPLE, types(prompt=("[prompt: int]", str), n=int))
    # The step's output dim collides with a mapped dim.
    with pytest.raises(SignatureError, match="collide"):
        run_output_type(SAMPLE, types(prompt=("[sample: int]", str), n=int), map_over=["sample"])


# ---- decorators ----


def test_step_decorator_produces_step_objects() -> None:
    @step(inputs={"a": int, "b": int}, output=int)
    async def add(context: StepContext, a: Array[int], b: Array[int]) -> Array[int]:
        return Array.scalar(a.item() + b.item(), int)

    assert isinstance(add, Step)
    assert add.name == f"{__name__}.test_step_decorator_produces_step_objects.<locals>.add"
    assert add.signature == FixedSignature.of({"a": int, "b": int}, int)
    assert not isinstance(add.body, Step)

    @step(name="custom", signature=SAMPLE)
    async def sample(context: StepContext, prompt: Array[str], n: Array[int]) -> Array[str]:
        return prompt

    assert sample.name == "custom"
    assert sample.signature is SAMPLE
    # A bare Array[X] annotation declares a schema but no dims, so it cannot declare a signature.
    with pytest.raises(TypeError, match="does not give the dims"):
        step()(add.body)


async def test_step_call_passes_arguments_through() -> None:
    @step(inputs={"a": int}, output=int)
    async def double(context: StepContext, a: int) -> int:
        return 2 * a

    assert await double(None, 21) == 42  # type: ignore[arg-type]


async def test_invoke_unwraps_scalar_inputs_and_wraps_scalar_results() -> None:
    seen: dict[str, object] = {}

    @step(inputs={"prompt": str, "n": int, "scores": ("[s: int]", float)}, output=float)
    async def summarize(context: StepContext, prompt: str, n: int, scores: Array[float]) -> float:
        seen.update(prompt=prompt, n=n, scores=scores)
        return sum(scores.values()) / n

    scores = Array.from_items([((0,), 1.0), ((1,), 3.0)], ("[s: int]", float))
    result = await summarize.invoke(
        None,  # type: ignore[arg-type]
        {"prompt": Array.scalar("hi", str), "n": Array.scalar(2, int), "scores": scores},
    )
    assert seen == {"prompt": "hi", "n": 2, "scores": scores}
    assert result == Array.scalar(2.0, float)


async def test_invoke_accepts_a_array_for_a_scalar_output() -> None:
    @step(inputs={"a": int}, output=int)
    async def as_array(context: StepContext, a: int) -> Array[int]:
        return Array.scalar(a, int)

    result = await as_array.invoke(None, {"a": Array.scalar(3, int)})  # type: ignore[arg-type]
    assert result == Array.scalar(3, int)


async def test_invoke_checks_inputs_and_results() -> None:
    # An `Any` result annotation declares nothing, so the body is free to return a bare value.
    @step(inputs={"a": int}, output=("[k: int]", int))
    async def bad_bare(context: StepContext, a: int) -> Any:
        return a

    with pytest.raises(SignatureError, match="Expected inputs"):
        await bad_bare.invoke(None, {"b": Array.scalar(1, int)})  # type: ignore[arg-type]
    with pytest.raises(SignatureError, match="bare value"):
        await bad_bare.invoke(None, {"a": Array.scalar(1, int)})  # type: ignore[arg-type]

    @step(inputs={"a": int}, output=("[k: int]", int))
    async def wrong_dims(context: StepContext, a: int) -> Array[int]:
        return Array.from_items([((0,), a)], ("[j: int]", int))

    with pytest.raises(SignatureError, match="expected \\[k: int\\]: int"):
        await wrong_dims.invoke(None, {"a": Array.scalar(1, int)})  # type: ignore[arg-type]

    @step(inputs={"a": int}, output=str)
    async def wrong_schema(context: StepContext, a: int) -> str:
        return a  # type: ignore[return-value]

    with pytest.raises(ArrayError, match="expected str"):
        await wrong_schema.invoke(None, {"a": Array.scalar(1, int)})  # type: ignore[arg-type]


def test_workflow_decorator_produces_workflow_objects() -> None:
    @workflow(inputs={"prompts": ("[prompt: int]", str), "n": int}, output=("[prompt: int]", float))
    async def wf(
        context: WorkflowContext, prompts: ArrayHandle[str], n: ArrayHandle[int]
    ) -> ArrayHandle[Any]:
        return prompts

    assert isinstance(wf, Workflow)
    assert wf.name.endswith("<locals>.wf")
    assert wf.signature == FixedSignature.of(
        {"prompts": ("[prompt: int]", str), "n": int}, ("[prompt: int]", float)
    )
    with pytest.raises(TypeError, match="both inputs"):
        workflow(inputs={"a": int})(wf.body)


def test_join_path_and_append_mapped_key() -> None:
    assert join_path("root", "sample") == "root/sample"
    assert append_mapped_key("root/sample", {}) == "root/sample"
    assert append_mapped_key("root/sample", {"prompt": 3, "split": "a"}) == (
        'root/sample[prompt:3,split:"a"]'
    )
    # Order follows the mapping (map_over order), and strings are JSON-quoted, so a str key
    # never collides with an int key.
    assert append_mapped_key("p", {"b": "1", "a": 1}) == 'p[b:"1",a:1]'
    assert append_mapped_key("p", {"s": 'x"y'}) == 'p[s:"x\\"y"]'


# ---- annotation-driven conversions ----

CONFIG_TYPE = "test.fxtr.signatures.config"
CONFIG_REF = id_of(CONFIG_TYPE)


@dataclasses.dataclass(frozen=True, kw_only=True)
class Config(DataclassEntity, fxtr_type=CONFIG_TYPE):
    model: str


class _StoreContext(StepContext):
    """A step context over a MemoryStorage, for invoking steps directly in tests."""

    def __init__(self) -> None:
        self.storage = MemoryStorage()

    @property
    def cache_address(self) -> str:
        return "test"

    def log(self, message: str) -> None:
        pass

    def is_known(self, entity_id: EntityID) -> bool:
        return self.storage.is_known(entity_id)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return await self.storage.store_raw_data(data)

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return await self.storage.load_raw_data(entity_id)


def test_step_derives_conversions_from_annotations() -> None:
    @step(inputs={"a": CONFIG_REF, "b": CONFIG_REF, "c": int, "d": EntityID, "e": int}, output=int)
    async def fn(
        context: StepContext, a: Config, b: BoundID[Config], c: Array[int], d: EntityID, e, f: Any
    ) -> int:
        return 0

    assert dict(fn.conversions) == {
        "a": ArgConversion("load", Config, view=AnnotatedType((), CONFIG_REF)),
        "b": ArgConversion("plain", view=AnnotatedType((), CONFIG_REF)),
        "c": ArgConversion("array", view=AnnotatedType(None, as_schema(int))),
        "d": ArgConversion("plain", view=AnnotatedType((), id_of())),
        "f": PLAIN_ARG,
    }
    assert fn.conversions.get("e", PLAIN_ARG) == PLAIN_ARG

    # An explicit signature is not checked against the annotations, so this reaches the
    # conversion-deriving pass.
    with pytest.raises(TypeError, match="entity class as its target"):

        @step(signature=FixedSignature.of({"a": EntityID}, int))
        async def bare_bound(context: StepContext, a: BoundID) -> int:  # type: ignore[type-arg]
            return 0

    with pytest.raises(TypeError, match="Cannot resolve"):

        @step(inputs={"a": EntityID}, output=int)
        async def unresolvable(context: StepContext, a: NoSuchType) -> int:  # type: ignore[name-defined]  # noqa: F821
            return 0


def test_body_parameters_come_from_the_signature() -> None:
    def passthrough(fn: Any) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

        return wrapper

    # A wrapped body reports the parameters it wraps, not the wrapper's (*args, **kwargs).
    @step(inputs={"a": int}, output=int)
    @passthrough
    async def wrapped(context: StepContext, a: int) -> int:
        return a

    assert dict(wrapped.conversions) == {
        "a": ArgConversion("plain", view=AnnotatedType((), as_schema(int)))
    }

    # A body is called with one array per named argument, so it cannot be variadic.
    with pytest.raises(TypeError, match="must name every argument"):

        @step(inputs={"a": int}, output=int)
        async def variadic(context: StepContext, a: int, *rest: int) -> int:
            return a


async def test_invoke_passes_scalar_refs_by_annotation() -> None:
    context = _StoreContext()
    cfg_id = (await context.store(Config(model="m"))).entity_id
    seen: dict[str, Any] = {}

    @step(
        inputs={"loaded": CONFIG_REF, "bound": CONFIG_REF, "bare": EntityID, "plain": EntityID},
        output=str,
    )
    async def fn(
        context: StepContext,
        loaded: Config,
        bound: BoundID[Config],
        bare: EntityID,
        plain,
    ) -> str:
        seen.update(loaded=loaded, bound=bound, bare=bare, plain=plain)
        return loaded.model

    scalar = Array.scalar(cfg_id, CONFIG_REF)
    result = await fn.invoke(
        context, {"loaded": scalar, "bound": scalar, "bare": scalar, "plain": scalar}
    )
    assert result.item() == "m"
    assert seen["loaded"] == Config(model="m")
    assert seen["bound"] == cfg_id.bind_type(Config)
    assert seen["bare"] == cfg_id
    assert seen["plain"] == cfg_id


async def test_invoke_hands_over_arrays_and_nullable_refs_as_annotated() -> None:
    context = _StoreContext()
    cfg_id = (await context.store(Config(model="m"))).entity_id

    @step(inputs={"n": int, "cfg": optional(CONFIG_REF)}, output=str)
    async def fn(context: StepContext, n: Array[int], cfg: Config | None) -> str:
        assert isinstance(n, Array)
        return "none" if cfg is None else cfg.model

    assert (
        await fn.invoke(
            context,
            {"n": Array.scalar(1, int), "cfg": Array.scalar(None, optional(CONFIG_REF))},
        )
    ).item() == "none"
    assert (
        await fn.invoke(
            context,
            {"n": Array.scalar(1, int), "cfg": Array.scalar(cfg_id, optional(CONFIG_REF))},
        )
    ).item() == "m"

    # A parameter annotated for a scalar entity id cannot take an array with dims. A value
    # annotation always declares dims of its own, so the signature is given explicitly here.
    @step(signature=FixedSignature.of({"cfgs": ("[i: int]", CONFIG_REF)}, int))
    async def over_dims(context: StepContext, cfgs: EntityID) -> int:
        return 0

    with pytest.raises(SignatureError, match="received an array"):
        await over_dims.invoke(
            context, {"cfgs": Array.from_items([((0,), cfg_id)], ("[i: int]", CONFIG_REF))}
        )


async def test_invoke_canonicalizes_entity_results_to_ids() -> None:
    context = _StoreContext()

    @step(inputs={"model": str}, output=CONFIG_REF)
    async def make(context: StepContext, model: str) -> Config:
        return Config(model=model)

    @step(inputs={"model": str}, output=CONFIG_REF)
    async def make_bound(context: StepContext, model: str) -> BoundID[Config]:
        return await context.store(Config(model=model))

    expected = (await context.store(Config(model="m"))).entity_id
    assert (await make.invoke(context, {"model": Array.scalar("m", str)})).item() == expected
    assert (await make_bound.invoke(context, {"model": Array.scalar("m", str)})).item() == expected
    assert await context.load(expected, as_type=Config) == Config(model="m")


# ---- annotation-declared signatures ----


def test_annotations_declare_the_signature() -> None:
    @step()
    async def sample(
        context: StepContext, prompt: str, n: int
    ) -> Annotated[Array[str], "[sample: int]"]:
        return Array.from_items([((0,), prompt)], ("[sample: int]", str))

    assert sample.signature == SAMPLE

    @workflow()
    async def wf(
        context: WorkflowContext,
        prompts: Annotated[ArrayHandle[str], "[prompt: int]"],
        cfg: Config,
    ) -> Annotated[ArrayHandle[float], "[prompt: int]"]:
        return prompts  # type: ignore[return-value]

    assert wf.signature == FixedSignature.of(
        {"prompts": ("[prompt: int]", str), "cfg": CONFIG_REF}, ("[prompt: int]", float)
    )
    # The base type keeps its ordinary meaning, so it still drives how a step is called.
    assert dict(sample.conversions) == {
        "prompt": ArgConversion("plain", view=AnnotatedType((), as_schema(str))),
        "n": ArgConversion("plain", view=AnnotatedType((), as_schema(int))),
    }

    # A bare entity class means an id at the top level of an argument or the result.
    @step()
    async def make(context: StepContext, model: str, temperature: float | None) -> Config:
        return Config(model=model)

    assert make.signature == FixedSignature.of(
        {"model": str, "temperature": float | None}, CONFIG_REF
    )


def test_scalar_aliases_declare_dim_less_array_annotations() -> None:
    # ScalarHandle[X] / ScalarArray[X] are type aliases for Annotated[...[X], "[]"]; the reader
    # expands them, so a body can use them in place of the spelled-out annotation.
    @workflow()
    async def wf(
        context: WorkflowContext, cfg: ScalarHandle[BoundID[Config]]
    ) -> ScalarHandle[BoundID[Config]]:
        return cfg

    assert wf.signature == FixedSignature.of({"cfg": CONFIG_REF}, CONFIG_REF)

    @step()
    async def st(context: StepContext, cfg: ScalarArray[EntityID]) -> ScalarArray[EntityID]:
        return cfg

    assert st.signature == FixedSignature.of({"cfg": EntityID}, EntityID)


def test_annotations_are_checked_against_a_declared_signature() -> None:
    # An annotation without a dims string constrains only the schema; a dims string must agree
    # exactly.
    @step(inputs={"scores": ("[sample: int]", float)}, output=float)
    async def mean(context: StepContext, scores: Annotated[Array[float], "[sample: int]"]) -> float:
        return 0.0

    assert mean.signature == MEAN

    with pytest.raises(SignatureError, match=r"annotated dims \[wrong: int\]"):

        @step(inputs={"scores": ("[sample: int]", float)}, output=float)
        async def wrong_dims(
            context: StepContext, scores: Annotated[Array[float], "[wrong: int]"]
        ) -> float:
            return 0.0

    with pytest.raises(SignatureError, match="result"):

        @step(inputs={"n": int}, output=int)
        async def wrong_output(
            context: StepContext, n: int
        ) -> Annotated[Array[str], "[sample: int]"]:
            return Array.from_items([], ("[sample: int]", str))

    with pytest.raises(SignatureError, match="not in inputs="):

        @step(inputs={"n": int}, output=int)
        async def extra(context: StepContext, n: int, m: int) -> int:
            return 0

    # An explicit signature object may be dynamic, so annotations alongside it are not checked.
    @step(signature=MEAN)
    async def dynamic(
        context: StepContext, scores: Annotated[Array[float], "[anything: str]"]
    ) -> float:
        return 0.0

    assert dynamic.signature is MEAN


def test_schemas_are_checked_by_function_subtyping() -> None:
    # An input may be declared more precisely than the body's annotation accepts: a body reading
    # the bare id works for a declared typed id.
    @step(inputs={"cfg": CONFIG_REF}, output=int)
    async def loose_input(context: StepContext, cfg: EntityID) -> int:
        return 0

    assert loose_input.signature == FixedSignature.of({"cfg": CONFIG_REF}, int)

    with pytest.raises(SignatureError, match="does not accept the declared input"):

        @step(inputs={"n": str}, output=int)
        async def wrong_input(context: StepContext, n: int) -> int:
            return 0

    # The result goes the other way: a narrower annotated result fits a wider declaration.
    @step(inputs={"n": int}, output=str | None)
    async def narrow_result(context: StepContext, n: int) -> str:
        return ""

    assert narrow_result.signature == FixedSignature.of({"n": int}, str | None)

    # A wider annotated result does not: nothing would catch an id of the wrong type.
    with pytest.raises(SignatureError, match="not assignable to the declared"):

        @step(inputs={"n": int}, output=CONFIG_REF)
        async def wide_result(context: StepContext, n: int) -> EntityID:
            raise NotImplementedError


def test_annotation_declaration_is_all_or_nothing() -> None:
    # An `Any` parameter declares nothing at all, so nothing gives it a type.
    with pytest.raises(TypeError, match="annotate cfg"):

        @step()
        async def missing_one(context: StepContext, prompt: str, cfg: Any) -> int:
            return 0

    with pytest.raises(TypeError, match="annotate the result"):

        @step()
        async def missing_result(context: StepContext, prompt: str) -> Any:
            return {}

    # An Array[Any] names its dims but says nothing about its values.
    with pytest.raises(TypeError, match="value schema"):

        @step()
        async def no_schema(
            context: StepContext, scores: Annotated[Array[Any], "[sample: int]"]
        ) -> float:
            return 0.0

    # An untyped id is a schema of its own, so it needs no further declaration.
    @step()
    async def untyped(context: StepContext, cfg: EntityID) -> int:
        return 0

    assert untyped.signature == FixedSignature.of({"cfg": EntityID}, int)


def test_annotation_forms_and_their_errors() -> None:
    # A bare string is read as dims only on an Array or an ArrayHandle.
    assert declared_array_type(Annotated[Array[int], "[a: int]"], "x") == AnnotatedType(
        parse_dims("a: int"), as_schema(int)
    )
    assert declared_array_type(Annotated[ArrayHandle[int], "[]"], "x") == AnnotatedType(
        (), as_schema(int)
    )
    # Without one, an array annotation declares its schema but not its dims.
    assert declared_array_type(Array[int], "x") == AnnotatedType(None, as_schema(int))
    assert declared_array_type(Array[Any], "x") == AnnotatedType(None, None)
    # Elsewhere a string is another tool's metadata, and a value has no dims.
    assert declared_array_type(Annotated[int, "[a: int]"], "x") == AnnotatedType((), as_schema(int))
    assert declared_array_type(int, "x") == AnnotatedType((), as_schema(int))
    assert declared_array_type(Any, "x") is None
    assert declared_array_type(object, "x") is None
    # A bare entity class, and its optional form, are ids at the top level.
    assert declared_array_type(Config, "x") == AnnotatedType((), id_of(Config))
    assert declared_array_type(Config | None, "x") == AnnotatedType((), optional(CONFIG_REF))

    with pytest.raises(TypeError, match="Unclosed"):
        declared_array_type(Annotated[Array[int], "[a: int"], "x")
    with pytest.raises(TypeError, match="expected one"):
        declared_array_type(Annotated[Array[int], "[a: int]", "[b: str]"], "x")
    with pytest.raises(TypeError, match="never optional"):
        declared_array_type(Array[int] | None, "x")
    with pytest.raises(TypeError, match="never optional"):
        declared_array_type(ArrayHandle[int] | None, "x")
    with pytest.raises(TypeError, match="no fixed fields"):
        declared_array_type(dict[str, Any], "x")


async def test_arguments_are_converted_when_annotations_declare_the_signature() -> None:
    context = _StoreContext()
    cfg = await context.store(Config(model="m"))

    @step()
    async def fn(
        context: StepContext,
        loaded: Config,
        bound: BoundID[Config],
        missing: Config | None,
        whole: Annotated[Array[int], "[]"],
    ) -> str:
        assert loaded == Config(model="m")
        assert bound == cfg
        assert missing is None
        assert whole.item() == 3
        return "ok"

    assert dict(fn.conversions) == {
        "loaded": ArgConversion("load", Config, view=AnnotatedType((), CONFIG_REF)),
        "bound": ArgConversion("plain", view=AnnotatedType((), CONFIG_REF)),
        "missing": ArgConversion("load", Config, view=AnnotatedType((), optional(CONFIG_REF))),
        "whole": ArgConversion("array", view=AnnotatedType((), as_schema(int))),
    }
    result = await fn.invoke(
        context,
        {
            "loaded": Array.scalar(cfg.entity_id, CONFIG_REF),
            "bound": Array.scalar(cfg.entity_id, CONFIG_REF),
            "missing": Array.scalar(None, optional(CONFIG_REF)),
            "whole": Array.scalar(3, int),
        },
    )
    assert result.item() == "ok"


async def test_invoke_retypes_array_inputs_to_the_annotated_view() -> None:
    context = _StoreContext()
    cfg_id = (await context.store(Config(model="m"))).entity_id
    seen: dict[str, Any] = {}

    @step()
    async def fn(
        context: StepContext, cfgs: Annotated[Array[BoundID[Config]], "[i: int]"]
    ) -> Annotated[Array[str], "[i: int]"]:
        seen["values"] = list(cfgs.values())
        return Array.from_items([((0,), "ok")], ("[i: int]", str))

    # The stored input carries a bare typed id (CONFIG_REF has no loader); retyping to the
    # annotation's view adds the loader, so the body reads the values back as bound ids.
    stored = Array.from_items([((0,), cfg_id)], ("[i: int]", CONFIG_REF))
    await fn.invoke(context, {"cfgs": stored})
    assert seen["values"] == [cfg_id.bind_type(Config)]
