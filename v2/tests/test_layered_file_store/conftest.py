"""Shared fixtures and helpers for layered_file_store tests.

Includes a tiny in-memory `StubDeepFetchable` that wraps a fxtr scope's
main arena, used as a stand-in for the remote entity store in tests
that need remote-resolution behavior.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, override

import pytest

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.abstract_arena import DeepFetchable, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import define_type
from fxtr2.leases.lease_types import Lease

# =============================================================================
# Test entity types
# =============================================================================

LFS_TEST_NS = uuid.UUID("9f1d4a32-7c8e-4b6d-a1f3-2b5e9d8c7f10")


@dataclasses.dataclass(frozen=True)
class LfsLeaf(DataclassEntity, fxtr_type=define_type(LFS_TEST_NS, "lfs_leaf")):
    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class LfsParent(DataclassEntity, fxtr_type=define_type(LFS_TEST_NS, "lfs_parent")):
    name: str
    child: ChildRef[LfsLeaf]


# =============================================================================
# Stub remote
# =============================================================================


class StubDeepFetchable(DeepFetchable[Any]):
    """In-memory ``DeepFetchable`` backed by a fxtr ``FxtrScope``.

    Delegates ``try_lookup`` to ``scope._deep_fetch(node_id)``, which
    is the scope's canonical "give me an arena tracking this id"
    primitive — it walks the scope's main arena, transitive ancestors,
    and any registered deep-fetch sources, returning an arena branded
    to ``scope``'s token. This matches the spirit of ``HttpDeepFetchable``
    (which is backed by the server's ``_deep_fetch`` on the wire).

    The arena returned by ``try_lookup`` rides on the same lifetime
    token as the scope's main arena, so the caller's ``remote_lease``
    must be a clone of the scope's main lease — use ``lease()`` for
    that. (The scope must be deeply fetchable, which is the default.)
    """

    def __init__(self, scope: FxtrScope):
        self._scope = scope

    def lease(self) -> Lease[Any]:
        return self._scope.clone_authorization()

    @override
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[Any] | None:
        return await self._scope._deep_fetch(node_id)


# =============================================================================
# Pytest config
# =============================================================================


@pytest.fixture
def anyio_backend():
    return "asyncio"
