"""Shared helpers for the experimental HTTP tests.

Currently: scaffolding to mint a c-node *provenance context* so a test can
store a genuinely-provenanced (``n``-node) entity — the shape the serve +
client provenance paths must round-trip. Imported explicitly (the
established pattern, e.g. ``tests.test_layered_file_store.conftest``), not
just auto-discovered.
"""

from __future__ import annotations

import fxtr2.data.system_entities  # noqa: F401 — registers the `ctx` builtin handler
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

assert fxtr2.data.system_entities is not None

GROUND = FxtrID.from_tag_and_payload(".", b"")


def make_c_node(scope: FxtrScope, *, ctx_key: str = "test_http_prov") -> FxtrID:
    """Mint a c-node provenance context in ``scope``.

    Open a child scope with ``provenance_context=<this c-node>`` and stored
    entities whose children carry provenance take the store's prov path,
    gaining an ``n``-node provenance parent.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope._main_arena.create_c_node(scope.clone_authorization(), data)
