"""Canonical-encoding tests: identity text, repacking, payload entities."""

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as e
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import EncodingError


def test_coords_roundtrip_and_injectivity() -> None:
    coords = ("föö", 3, "", "a,b][", 0)
    text = e.encode_coords(coords)
    assert e.decode_coords(text) == coords
    # str vs int of the same digits encode differently (JSON types).
    assert e.encode_coords(("1",)) != e.encode_coords((1,))
    # Canonical: no spaces.
    assert " " not in e.encode_coords(("a b",)).replace('"a b"', "")


def test_coords_reject_bool_and_float() -> None:
    with pytest.raises(EncodingError):
        e.encode_coords((True,))
    with pytest.raises(EncodingError):
        e.encode_coords((1.5,))  # type: ignore[arg-type]
    with pytest.raises(EncodingError):
        e.decode_coords("[1.5]")
    with pytest.raises(EncodingError):
        e.decode_coords("[true]")
    with pytest.raises(EncodingError):
        e.decode_coords('{"a":1}')


def test_key_path_roundtrip_and_parent() -> None:
    assert e.encode_key_path([]) == e.ROOT_KEY_PATH
    text = e.encode_key_path(["mh", "grades", "judge_calls"])
    assert e.decode_key_path(text) == ("mh", "grades", "judge_calls")
    assert e.parent_key_path(text) == e.encode_key_path(["mh", "grades"])
    assert e.parent_key_path(e.encode_key_path(["top"])) == e.ROOT_KEY_PATH
    with pytest.raises(EncodingError):
        e.parent_key_path(e.ROOT_KEY_PATH)


def test_cellref_repacking_through_internals() -> None:
    cell = m.CellRef(
        grid=(
            m.SubgridStep(cell=(), key="mh"),
            m.SubgridStep(cell=("v1",), key="grades"),
            m.TaskInternalsStep(cell=("s1", "j1"), key="judge_calls"),
        ),
        coords=("p1",),
    )
    assert e.key_path_of(cell.grid) == ("mh", "grades", "judge_calls")
    assert e.flattened_coords_of(cell) == ("v1", "s1", "j1", "p1")
    key_text, coords_text = e.storage_cell_location(cell)
    assert e.decode_key_path(key_text) == ("mh", "grades", "judge_calls")
    assert e.decode_coords(coords_text) == ("v1", "s1", "j1", "p1")


def _task_config() -> m.TaskGridConfig:
    """The smoke-test rule: judges-over-transcripts, with named outputs."""
    rule = m.TaskRule(
        task_fn=m.SliceTemplate(
            path=(m.SubgridStepTemplate(cell=(), key="judges"),),
            target_axes=(m.Param(1),),
        ),
        inputs=frozendict(
            {
                "transcripts": m.SliceBinding(
                    slice=m.SliceTemplate(
                        path=(m.SubgridStepTemplate(cell=(), key="transcripts"),),
                        target_axes=(m.Param(0), m.KeepAxis()),
                    ),
                    mode="read",
                    received_axis_names=("ix",),
                ),
            }
        ),
    )
    return m.TaskGridConfig(
        axes=(m.AxisSpec("category", "str"), m.AxisSpec("judge", "str")),
        rule=rule,
        outputs=frozendict(
            {
                "result": m.EntityOutput(),
                "scores": m.FrameOutput(
                    schema=(m.ColumnSpec("score", "float"), m.ColumnSpec("evidence", "bytes"))
                ),
            }
        ),
    )


ALL_CONFIGS: tuple[m.GridConfig, ...] = (
    m.DataGridConfig(axes=(m.AxisSpec("x", "str"),)),
    m.DataFrameGridConfig(axes=(m.AxisSpec("x", "str"),), schema=(m.ColumnSpec("n", "int"),)),
    m.HandleGridConfig(axes=(m.AxisSpec("x", "str"),)),
    m.CheckpointGridConfig(axes=(m.AxisSpec("x", "str"),)),
    m.SubtreeGridConfig(axes=(m.AxisSpec("x", "str"),)),
    _task_config(),
)


@pytest.mark.parametrize("config", ALL_CONFIGS, ids=lambda c: type(c).__name__)
def test_config_json_roundtrip(config: m.GridConfig) -> None:
    assert e.config_from_json(e.config_to_json(config)) == config


def test_shape_identical_kinds_encode_distinctly() -> None:
    """Data/handle/checkpoint/subtree configs share the axes-only shape —
    the kind tags are what keep their fingerprints distinct."""
    axes = (m.AxisSpec("x", "str"),)
    ids = {
        e.store_config(cfg)
        for cfg in (
            m.DataGridConfig(axes=axes),
            m.HandleGridConfig(axes=axes),
            m.CheckpointGridConfig(axes=axes),
            m.SubtreeGridConfig(axes=axes),
        )
    }
    assert len(ids) == 4


def test_axis_names_are_identity_bearing() -> None:
    a = m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))
    b = m.DataGridConfig(axes=(m.AxisSpec("y", "str"),))
    assert e.store_config(a) != e.store_config(b)


def test_config_fingerprint_is_deterministic() -> None:
    id_1 = e.store_config(_task_config())
    id_2 = e.store_config(_task_config())
    assert id_1 == id_2
    assert id_1 != e.store_config(m.DataGridConfig(axes=()))


def test_mapping_insertion_order_does_not_change_fingerprint() -> None:
    """frozendict fields compare order-insensitively; canonical entity CBOR
    (sorted map keys) must make the *ids* order-insensitive too."""

    def outputs(order: list[str]) -> "frozendict[str, m.OutputSpec]":
        specs: dict[str, m.OutputSpec] = {
            "result": m.EntityOutput(),
            "scores": m.FrameOutput(schema=(m.ColumnSpec("score", "float"),)),
        }
        return frozendict({k: specs[k] for k in order})

    base = _task_config()
    a = m.TaskGridConfig(axes=base.axes, rule=base.rule, outputs=outputs(["result", "scores"]))
    b = m.TaskGridConfig(axes=base.axes, rule=base.rule, outputs=outputs(["scores", "result"]))
    assert a == b  # frozendict equality is order-insensitive
    assert e.store_config(a) == e.store_config(b)


def test_slice_template_roundtrip_with_internals_step() -> None:
    """Sub vs internals steps are shape-identical — the tag is load-bearing
    (an untagged codec would collapse them)."""
    s = m.SliceTemplate(
        path=(
            m.SubgridStepTemplate(cell=(m.Param(0),), key="grades"),
            m.InternalsStepTemplate(cell=(m.KeepAxis(),), key="judge_calls"),
        ),
        target_axes=(m.LiteralCoord("p1"), m.KeepAxis()),
        output="scores",
    )
    assert e.slice_from_json(e.slice_to_json(s)) == s
    twin = m.SliceTemplate(
        path=(
            m.SubgridStepTemplate(cell=(m.Param(0),), key="grades"),
            m.SubgridStepTemplate(cell=(m.KeepAxis(),), key="judge_calls"),
        ),
        target_axes=s.target_axes,
        output="scores",
    )
    assert e.store_slice(twin) != e.store_slice(s)
    assert e.store_slice(s) == e.store_slice(s)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_entity_store_load_roundtrip() -> None:
    """The strategy-backed entity holds the actual model object and
    roundtrips through fxtr storage (store → load → store, same id)."""
    cfg = _task_config()
    ref = e.GridConfigEntity(config=cfg).store()
    loaded = await ref.load()
    assert loaded.config == cfg
    assert loaded.store().entity_id == ref.entity_id


# -- step payloads -----------------------------------------------------------


def _payload_id(value) -> "e.FxtrID":
    return e.store_step_payload(value)


def _at(v: object, key: "str | int") -> object:
    """Index into a decoded payload, narrowing the service-data union."""
    if isinstance(key, str):
        assert isinstance(v, Mapping)
        return v[key]
    assert isinstance(v, Sequence)
    return v[key]


def test_step_payload_roundtrip() -> None:
    inner = e.GridConfigEntity(config=m.DataGridConfig(axes=())).store().entity_id
    value = {
        "text": "hello",
        "n": 3,
        "flag": True,
        "none": None,
        "blob": b"\x00\x01",
        "nested": [1, {"id": inner}, ["deep"]],
    }
    encoded = e.service_data_to_frozen_cbor(value)
    decoded = e.frozen_cbor_to_service_data(encoded)
    # Canonical forms: mappings → dicts, sequences → lists, ids → bare ids.
    assert decoded == value
    nested = _at(decoded, "nested")
    assert isinstance(nested, list)
    assert _at(_at(nested, 1), "id") == inner


def test_step_payload_fingerprint_determinism() -> None:
    a = _payload_id({"x": 1, "y": [1, 2]})
    b = _payload_id({"y": [1, 2], "x": 1})  # key order irrelevant
    assert a == b
    assert a != _payload_id({"x": 1, "y": [2, 1]})  # sequence order isn't
    # Tuple vs list encode identically (both are sequences).
    assert _payload_id({"y": (1, 2), "x": 1}) == a


def test_step_payload_rejects_non_durable_data() -> None:
    from fxtr_experiment_task.errors import StepPayloadError

    class Opaque:
        pass

    with pytest.raises(StepPayloadError):
        e.service_data_to_frozen_cbor(cast(Any, {"bad": Opaque()}))
    with pytest.raises(StepPayloadError):
        e.service_data_to_frozen_cbor({1: "non-str key"})  # type: ignore[dict-item]


def test_step_payload_rejects_service_refs() -> None:
    from fxtr2.data.entity_ref_scope import ServiceRef
    from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing

    from fxtr_experiment_task.errors import StepPayloadError

    ref = ServiceRef(handle=None, target_type=OverrideThisTypeBeforeUsing, service_type_id=None)  # type: ignore[arg-type]
    with pytest.raises(StepPayloadError):
        e.service_data_to_frozen_cbor({"svc": ref})


def test_step_payload_collapses_entity_refs() -> None:
    ref = e.GridConfigEntity(config=m.DataGridConfig(axes=())).store()
    encoded = e.service_data_to_frozen_cbor({"ref": ref})
    assert e.frozen_cbor_to_service_data(encoded) == {"ref": ref.entity_id}
