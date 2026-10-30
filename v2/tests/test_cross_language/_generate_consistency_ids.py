"""Generate hardcoded entity IDs for cross-language consistency tests.

Run with: cd v2 && uv run python tests/test_cross_language/generate_consistency_ids.py
"""

import asyncio
import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import FxtrBasicType, define_type

NS = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")


async def main() -> None:
    async with FxtrScope() as scope:
        # 1. defineType with a known namespace + name
        type_id = define_type(NS, "CrossLangTest")
        print(f"defineType CrossLangTest: {type_id.id_string()}")

        # 2. FxtrBasicType round-trip
        bt = FxtrBasicType(namespace_uuid=NS, type_name="CrossLangTest")
        bt_ref = bt.store()
        print(f"FxtrBasicType entity ID: {bt_ref.entity_id.id_string()}")

        # 3. Another defineType to verify different names produce different IDs
        type_id2 = define_type(NS, "AnotherType")
        print(f"defineType AnotherType: {type_id2.id_string()}")

        # 4. A simple entity with string + number fields
        @dataclasses.dataclass(frozen=True)
        class SimpleEntity(DataclassEntity, fxtr_type=define_type(NS, "SimpleEntity")):
            name: str
            value: int

        simple = SimpleEntity(name="hello", value=42)
        simple_ref = simple.store()
        print(f"SimpleEntity ID: {simple_ref.entity_id.id_string()}")

        # 5. Entity with frozenset field (sorted as canonical set)
        @dataclasses.dataclass(frozen=True)
        class SetEntity(DataclassEntity, fxtr_type=define_type(NS, "SetEntity")):
            tags: frozenset[str]

        se = SetEntity(tags=frozenset(["beta", "alpha", "gamma"]))
        se_ref = se.store()
        print(f"SetEntity ID: {se_ref.entity_id.id_string()}")

        # 6. Entity with nullable field
        @dataclasses.dataclass(frozen=True)
        class NullableEntity(DataclassEntity, fxtr_type=define_type(NS, "NullableEntity")):
            label: str | None

        ne_null = NullableEntity(label=None)
        ne_null_ref = ne_null.store()
        print(f"NullableEntity (null) ID: {ne_null_ref.entity_id.id_string()}")

        ne_val = NullableEntity(label="present")
        ne_val_ref = ne_val.store()
        print(f"NullableEntity (present) ID: {ne_val_ref.entity_id.id_string()}")


if __name__ == "__main__":
    asyncio.run(main())
