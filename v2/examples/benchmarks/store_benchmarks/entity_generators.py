"""Pluggable entity generators for store benchmarks.

Each EntityScenario is a named, fully-configured workload. The generate() callable
is invoked inside an active scope and returns (pin_keys_and_ids, total_entity_count).
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.system_entities import define_type

BENCH_NAMESPACE = uuid.UUID("b3a7c1d9-4e2f-4a8b-9c6d-5f1e0a3b2d7c")


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkTreeNode(DataclassEntity, fxtr_type=define_type(BENCH_NAMESPACE, "bench_tree_node")):
    """A binary tree node with a random data payload."""

    value: int
    left: ChildRef[BenchmarkTreeNode] | None
    right: ChildRef[BenchmarkTreeNode] | None
    data: bytes


@dataclass(frozen=True)
class BenchmarkFlatNode(DataclassEntity, fxtr_type=define_type(BENCH_NAMESPACE, "bench_flat_node")):
    """A standalone entity with no child references."""

    value: int
    data: bytes


@dataclass(frozen=True)
class RichBenchmarkFlatNode(
    DataclassEntity, fxtr_type=define_type(BENCH_NAMESPACE, "rich_bench_flat_node")
):
    """A standalone entity with 20 indexable fields plus a data payload.

    Fields f_int_0..f_int_9 are ints, f_float_0..f_float_9 are floats.
    This gives 20 distinct paths for indexers to target.
    """

    value: int
    data: bytes
    f_int_0: int
    f_int_1: int
    f_int_2: int
    f_int_3: int
    f_int_4: int
    f_int_5: int
    f_int_6: int
    f_int_7: int
    f_int_8: int
    f_int_9: int
    f_float_0: float
    f_float_1: float
    f_float_2: float
    f_float_3: float
    f_float_4: float
    f_float_5: float
    f_float_6: float
    f_float_7: float
    f_float_8: float
    f_float_9: float


# ---------------------------------------------------------------------------
# EntityScenario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityScenario:
    """A named, fully-configured entity workload.

    generate() must be called inside an active FxtrScope. It stores entities into
    the scope and returns (list_of_(key, entity_id), total_entity_count).
    """

    name: str
    description: str
    generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]]


# ---------------------------------------------------------------------------
# Tree builders
# ---------------------------------------------------------------------------


def _build_binary_tree_simple(
    depth: int, payload_size: int, counter: list[int]
) -> BenchmarkTreeNode:
    """Recursively build a BenchmarkTreeNode tree. counter[0] tracks node numbering."""
    node_value = counter[0]
    counter[0] += 1

    if depth == 0:
        return BenchmarkTreeNode(
            value=node_value, left=None, right=None, data=random.randbytes(payload_size)
        )

    left = _build_binary_tree_simple(depth - 1, payload_size, counter)
    right = _build_binary_tree_simple(depth - 1, payload_size, counter)
    left_ref = left.store()
    right_ref = right.store()

    return BenchmarkTreeNode(
        value=node_value, left=left_ref, right=right_ref, data=random.randbytes(payload_size)
    )


def _build_rich_flat_node(value: int, payload_size: int) -> RichBenchmarkFlatNode:
    """Build a single RichBenchmarkFlatNode with random indexable fields."""
    rng = random.Random(value)
    return RichBenchmarkFlatNode(
        value=value,
        data=random.randbytes(payload_size),
        f_int_0=rng.randint(0, 10000),
        f_int_1=rng.randint(0, 10000),
        f_int_2=rng.randint(0, 10000),
        f_int_3=rng.randint(0, 10000),
        f_int_4=rng.randint(0, 10000),
        f_int_5=rng.randint(0, 10000),
        f_int_6=rng.randint(0, 10000),
        f_int_7=rng.randint(0, 10000),
        f_int_8=rng.randint(0, 10000),
        f_int_9=rng.randint(0, 10000),
        f_float_0=rng.random() * 100,
        f_float_1=rng.random() * 100,
        f_float_2=rng.random() * 100,
        f_float_3=rng.random() * 100,
        f_float_4=rng.random() * 100,
        f_float_5=rng.random() * 100,
        f_float_6=rng.random() * 100,
        f_float_7=rng.random() * 100,
        f_float_8=rng.random() * 100,
        f_float_9=rng.random() * 100,
    )


# ---------------------------------------------------------------------------
# Generator functions (zero-arg, called inside active scope)
# ---------------------------------------------------------------------------


def _generate_single_tree_1k() -> tuple[list[tuple[str, FxtrID]], int]:
    counter = [0]
    root = _build_binary_tree_simple(10, 1024, counter)
    root_ref = root.store()
    return [("root", root_ref.entity_id)], counter[0]


def _generate_many_flat_1k() -> tuple[list[tuple[str, FxtrID]], int]:
    keys: list[tuple[str, FxtrID]] = []
    for i in range(500):
        node = BenchmarkFlatNode(value=i, data=random.randbytes(1024))
        ref = node.store()
        keys.append((f"node/{i}", ref.entity_id))
    return keys, len(keys)


def _generate_rich_flat_1k() -> tuple[list[tuple[str, FxtrID]], int]:
    keys: list[tuple[str, FxtrID]] = []
    for i in range(100):
        node = _build_rich_flat_node(i, 1024)
        ref = node.store()
        keys.append((f"node/{i}", ref.entity_id))
    return keys, len(keys)


# ---------------------------------------------------------------------------
# Scenario instances
# ---------------------------------------------------------------------------

single_tree_1k = EntityScenario(
    name="single_tree_1k",
    description="Depth-10 binary tree, 1KB payload (2047 entities, 1 pin)",
    generate=_generate_single_tree_1k,
)

many_flat_1k = EntityScenario(
    name="many_flat_1k",
    description="500 independent flat entities, 1KB each (500 pins)",
    generate=_generate_many_flat_1k,
)

rich_flat_1k = EntityScenario(
    name="rich_flat_1k",
    description="100 independent entities with 20 indexable fields, 1KB each (100 pins)",
    generate=_generate_rich_flat_1k,
)
