"""Cross-language consistency tests for bicodec entity encoding.

These tests verify that Python and TypeScript produce byte-identical
content-addressed entity IDs for equivalent entity definitions and data.

The TypeScript counterpart is at:
  v2/ts/tests/common/crossLanguageConsistency.test.ts
"""

import dataclasses
import uuid

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import FxtrBasicType, define_type

NS = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")

# Hardcoded expected IDs — must match TypeScript test expectations
EXPECTED_DEFINE_TYPE_CROSS_LANG = "dheggkym46dt6g7rxg6ftcpldoly6r4fjcwihuqgaisvei636ja4a"
EXPECTED_DEFINE_TYPE_ANOTHER = "dhwtdxe3t4jp5djsqgo6upturz74d34eauwcjz3txlasdj6gw43ba"
EXPECTED_SIMPLE_ENTITY = "df2dsm46vlwfcf7dyqqvid7eud3dazzj6z3h6lkzixahpimntvb7a"
EXPECTED_SET_ENTITY = "d2igkdhjh4tx2kbn3guz4prs2lzuje7wz6lrw7ypizce2x2szqc4q"
EXPECTED_NULLABLE_NULL = "dfjtvfjbdlkowhknevyk6umyxjxbgwd3v3a4dvze3plugqab5oqfq"
EXPECTED_NULLABLE_PRESENT = "dshw3655bvyeegydyiiqxujvsgfv6axca7x5b66ekph5a4hxufglq"


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=define_type(NS, "SimpleEntity")):
    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class SetEntity(DataclassEntity, fxtr_type=define_type(NS, "SetEntity")):
    tags: frozenset[str]


@dataclasses.dataclass(frozen=True)
class NullableEntity(DataclassEntity, fxtr_type=define_type(NS, "NullableEntity")):
    label: str | None


# `define_type` always stores into GLOBAL_SCOPE (it's meant for import-time use),
# so these calls live at module level rather than inside a test body — otherwise
# they would leak into the global scope and trip the leak detector in conftest.
_CROSS_LANG_TYPE_ID = define_type(NS, "CrossLangTest")
_ANOTHER_TYPE_ID = define_type(NS, "AnotherType")


@pytest.mark.anyio
class TestCrossLanguageConsistency:
    async def test_define_type_deterministic(self) -> None:
        assert _CROSS_LANG_TYPE_ID.id_string() == EXPECTED_DEFINE_TYPE_CROSS_LANG

    async def test_define_type_different_names(self) -> None:
        assert _ANOTHER_TYPE_ID.id_string() == EXPECTED_DEFINE_TYPE_ANOTHER

    async def test_fxtr_basic_type_round_trip(self) -> None:
        async with FxtrScope():
            bt = FxtrBasicType(namespace_uuid=NS, type_name="CrossLangTest")
            ref = bt.store()
            assert ref.entity_id.id_string() == EXPECTED_DEFINE_TYPE_CROSS_LANG

    async def test_simple_entity(self) -> None:
        async with FxtrScope():
            simple = SimpleEntity(name="hello", value=42)
            ref = simple.store()
            assert ref.entity_id.id_string() == EXPECTED_SIMPLE_ENTITY

    async def test_set_entity_canonical_order(self) -> None:
        async with FxtrScope():
            se = SetEntity(tags=frozenset(["beta", "alpha", "gamma"]))
            ref = se.store()
            assert ref.entity_id.id_string() == EXPECTED_SET_ENTITY

    async def test_nullable_entity_null(self) -> None:
        async with FxtrScope():
            ne = NullableEntity(label=None)
            ref = ne.store()
            assert ref.entity_id.id_string() == EXPECTED_NULLABLE_NULL

    async def test_nullable_entity_present(self) -> None:
        async with FxtrScope():
            ne = NullableEntity(label="present")
            ref = ne.store()
            assert ref.entity_id.id_string() == EXPECTED_NULLABLE_PRESENT
