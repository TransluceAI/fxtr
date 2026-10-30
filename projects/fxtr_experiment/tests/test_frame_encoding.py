"""Frame-encoding tests: the identity spec, frozen (`frame_encoding.py`).

The cbor2-conformance tests pin behaviors the spec states (canonical NaN,
``-0.0``, shortest-form floats): if a cbor2 upgrade ever changes them,
these fail loudly BEFORE any fingerprint drifts. The golden-id test
freezes the whole encoding the same way.
"""

import struct

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as e
from fxtr_experiment import frame_encoding as fe
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import EncodingError

SCHEMA = (m.ColumnSpec("name", "str"), m.ColumnSpec("score", "float"))
NULLABLE = (m.ColumnSpec("k", "int"), m.ColumnSpec("note", "str", nullable=True))


def _f64(bits: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", bits))[0]


# -- cbor2 conformance: the codec implements the spec --------------------------


def test_every_nan_normalizes_to_the_canonical_nan() -> None:
    schema = (m.ColumnSpec("x", "float"),)
    encodings = {
        fe.encode_row(schema, (v,))
        for v in (
            float("nan"),
            -float("nan"),
            _f64(0x7FF8_0000_0000_0ABC),  # payload NaN
            _f64(0xFFF8_0000_0000_0001),  # negative payload NaN
        )
    }
    assert encodings == {b"\x81\xf9\x7e\x00"}  # one-element array + canonical NaN


def test_negative_zero_is_distinct_and_preserved() -> None:
    schema = (m.ColumnSpec("x", "float"),)
    assert fe.encode_row(schema, (0.0,)) == b"\x81\xf9\x00\x00"
    assert fe.encode_row(schema, (-0.0,)) == b"\x81\xf9\x80\x00"


def test_floats_encode_shortest_form() -> None:
    schema = (m.ColumnSpec("x", "float"),)
    assert fe.encode_row(schema, (1.5,)) == b"\x81\xf9\x3e\x00"  # half
    assert fe.encode_row(schema, (0.1,)) == b"\x81\xfb\x3f\xb9\x99\x99\x99\x99\x99\x9a"
    assert fe.encode_row(schema, (float("inf"),)) == b"\x81\xf9\x7c\x00"


def test_int64_boundary() -> None:
    schema = (m.ColumnSpec("x", "int"),)
    fe.encode_row(schema, (2**63 - 1,))
    fe.encode_row(schema, (-(2**63),))
    with pytest.raises(EncodingError, match="int64"):
        fe.encode_row(schema, (2**63,))
    with pytest.raises(EncodingError, match="int64"):
        fe.encode_row(schema, (-(2**63) - 1,))


# -- value validation: exact-type, null legality --------------------------------


def test_exact_type_rejections() -> None:
    with pytest.raises(EncodingError, match="bool"):
        fe.encode_row((m.ColumnSpec("x", "int"),), (True,))
    with pytest.raises(EncodingError, match="int"):
        fe.encode_row((m.ColumnSpec("x", "float"),), (5,))
    with pytest.raises(EncodingError, match="str"):
        fe.encode_row((m.ColumnSpec("x", "bytes"),), ("s",))
    with pytest.raises(EncodingError, match="arity|columns"):
        fe.encode_row(SCHEMA, ("a",))


def test_null_legality() -> None:
    fe.encode_row(NULLABLE, (1, None))
    with pytest.raises(EncodingError, match="non-nullable"):
        fe.encode_row(NULLABLE, (None, "x"))


# -- canonicalization: the sort and the fingerprint ------------------------------


def test_permutations_and_duplicates_fingerprint_identically() -> None:
    rows = [("b", 2.0), ("a", 1.0), ("a", 1.0), ("c", -0.0)]
    ids = {
        fe.store_frame(SCHEMA, perm)
        for perm in (rows, rows[::-1], [rows[2], rows[3], rows[0], rows[1]])
    }
    assert len(ids) == 1


def test_multiplicity_is_identity_bearing() -> None:
    once = fe.store_frame(SCHEMA, [("a", 1.0)])
    twice = fe.store_frame(SCHEMA, [("a", 1.0), ("a", 1.0)])
    assert once != twice


def test_empty_frame_is_valid() -> None:
    assert fe.store_frame(SCHEMA, []) == fe.store_frame(SCHEMA, [])


def test_schema_is_identity_bearing() -> None:
    renamed = (m.ColumnSpec("other", "str"), m.ColumnSpec("score", "float"))
    assert fe.store_frame(SCHEMA, [("a", 1.0)]) != fe.store_frame(renamed, [("a", 1.0)])
    nullable = (m.ColumnSpec("name", "str", nullable=True), m.ColumnSpec("score", "float"))
    assert fe.store_frame(SCHEMA, [("a", 1.0)]) != fe.store_frame(nullable, [("a", 1.0)])


def test_sort_order_is_bytes_lex_not_semantic() -> None:
    """The canonical order is encoding-lexicographic — semantically
    arbitrary by design (length-prefixed strings sort "b" before "aa")."""
    schema = (m.ColumnSpec("s", "str"),)
    ent = fe.build_frame_entity(schema, [("aa",), ("a",), ("b",)])
    assert ent.rows == (("a",), ("b",), ("aa",))


def test_golden_fingerprint() -> None:
    """Freeze the whole encoding: ANY identity-affecting change (tagged
    schema form, sort, scalar rules, entity shape) moves this id and must
    be caught here, deliberately, before it silently strands stored data."""
    frame_id = fe.store_frame(NULLABLE, [(2, None), (1, "x"), (-3, ""), (0, None), (2**40, "é")])
    assert frame_id.id_string() == GOLDEN_ID


GOLDEN_ID = "d3gnvkgy2t7uqbmk3nnr2prewnhgh2bbruxodigvf2ik2io6rudqa"


# -- compositionality: the sort key IS the in-entity representation -------------


def test_row_encoding_is_compositional() -> None:
    r1, r2 = ("a", 1.0), ("b", -0.0)
    joined = fe.fxtr_cbor_dumps([list(r1), list(r2)])
    assert joined == b"\x82" + fe.encode_row(SCHEMA, r1) + fe.encode_row(SCHEMA, r2)


# -- schema tagged form: stability + the nullable flag ---------------------------


def test_non_nullable_schema_encoding_is_the_pair_form() -> None:
    """Pre-flag schemas keep their encoding — and config fingerprints."""
    assert e.schema_to_json(SCHEMA) == [["name", "str"], ["score", "float"]]
    assert e.schema_to_json(NULLABLE) == [["k", "int"], ["note", "str", True]]


def test_schema_json_roundtrip_and_rejections() -> None:
    assert e.schema_from_json(e.schema_to_json(NULLABLE)) == NULLABLE
    for bad in ([["x"]], [["x", "entity"]], [["x", "str", False]], [["x", "str", 1]]):
        with pytest.raises(EncodingError):
            e.schema_from_json(bad)


# -- the column-name law + its store_config funnel -------------------------------


def test_validate_frame_schema_law() -> None:
    for bad in (
        (),  # zero columns
        (m.ColumnSpec("", "str"),),
        (m.ColumnSpec("a\x00b", "str"),),
        (m.ColumnSpec("a\x7f", "str"),),
        (m.ColumnSpec("x", "str"), m.ColumnSpec("x", "int")),
    ):
        with pytest.raises(EncodingError):
            m.validate_frame_schema(bad)
    m.validate_frame_schema((m.ColumnSpec("ok name!", "str"),))  # light on purpose


def test_store_config_enforces_the_law() -> None:
    bad = (m.ColumnSpec("x", "str"), m.ColumnSpec("x", "int"))
    with pytest.raises(EncodingError):
        e.store_config(m.DataFrameGridConfig(axes=(), schema=bad))
    with pytest.raises(EncodingError):
        e.store_config(
            m.TaskGridConfig(
                axes=(),
                rule=m.TaskRule(
                    task_fn=m.SliceTemplate(
                        path=(m.SubgridStepTemplate(cell=(), key="fns"),), target_axes=()
                    ),
                    inputs=frozendict(),
                ),
                outputs=frozendict({"scores": m.FrameOutput(schema=bad)}),
            )
        )


# -- load_frame: the read half ----------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_store_load_roundtrip() -> None:
    rows = [(2, None), (1, "x"), (1, "x")]
    frame_id = fe.store_frame(NULLABLE, rows)
    schema, loaded = await fe.load_frame(frame_id)
    assert schema == NULLABLE
    assert sorted(loaded, key=lambda r: fe.encode_row(NULLABLE, r)) == list(loaded)
    assert sorted(map(repr, loaded)) == sorted(map(repr, rows))
    # Re-store of the loaded value: the same fingerprint.
    assert fe.store_frame(schema, loaded) == frame_id


@pytest.mark.anyio
async def test_load_refuses_wrong_entity_type() -> None:
    config_id = e.store_config(m.DataGridConfig(axes=()))
    with pytest.raises(ValueError):  # the typed load rejects the type tag
        await fe.load_frame(config_id)


@pytest.mark.anyio
async def test_load_refuses_non_canonical_order() -> None:
    """A hand-built frame entity with unsorted rows is corrupt whatever
    its id says — refused on load, never silently served."""
    tagged = tuple(tuple(c) for c in [["n", "int"]])
    ent = fe.FrameEntity(schema=tagged, rows=((2,), (1,)))
    with pytest.raises(EncodingError, match="canonical order"):
        await fe.load_frame(ent.store().entity_id)


@pytest.mark.anyio
async def test_load_refuses_invalid_values() -> None:
    tagged = tuple(tuple(c) for c in [["n", "int"]])
    ent = fe.FrameEntity(schema=tagged, rows=((True,),))
    with pytest.raises(EncodingError):
        await fe.load_frame(ent.store().entity_id)
