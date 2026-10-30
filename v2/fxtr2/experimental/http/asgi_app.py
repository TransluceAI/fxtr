"""Starlette ASGI app for serving fxtr entities over HTTP.

Provides routes for:
- GET /preview/{id_string} — Human-readable pretty-printed entity view
- GET /data/{id_string} — Entity bytes + provenance precomp map (CBOR)
- GET /pin/{name:path} — Look up a pin by name (CBOR with id + precomp)
- GET /pins?prefix=... — List pins matching a prefix (JSON)
"""

from __future__ import annotations

import json
import pprint
from typing import TYPE_CHECKING

import cbor2
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fxtr2.data.arena_util import PrecompMap, compute_precomp_closure
from fxtr2.data.entity_core import FxtrID, fxtr_cbor_loads
from fxtr2.experimental.http.service_codec import encode_pin_value

if TYPE_CHECKING:
    from fxtr2.data.entity_ref_scope import FxtrScope


def create_fxtr_app(
    scope: FxtrScope, pins: dict[str, FxtrID], *, provenanced: bool = False
) -> Starlette:
    """Create a Starlette ASGI app for serving fxtr entities.

    Args:
        scope: The FxtrScope to serve entities from. Must be deeply fetchable.
        pins: Dict of pin names to FxtrIDs. Read on each request (can be mutated externally).
        provenanced: When ``True``, layered pins expose their provenance — ``/pins``
            and ``/pin`` emit the layered ``[data, prov]`` form and ``/pin`` bundles
            the prov node's precomp closure. When ``False`` (default) every pin emits
            its bare data id, byte-identical to the bare wire (provenance is
            a server-side opt-in, so a server never leaks provenance unintentionally).
    """

    async def _check_scope(request: Request) -> Response | None:
        if not scope.is_open():
            return Response("Scope is closed", status_code=503)
        return None

    async def preview(request: Request) -> Response:
        if err := await _check_scope(request):
            return err

        id_string = request.path_params["id_string"]
        try:
            fxtr_id = FxtrID.from_id_string(id_string, allow_provenance=False)
        except (ValueError, KeyError):
            return Response("Invalid ID", status_code=400)

        arena = await scope._deep_fetch(fxtr_id)
        if arena is None:
            return Response("Not found", status_code=404)

        try:
            flat = await arena.load_flat(scope._current_auth_lease().clone(), fxtr_id)
        finally:
            arena.close()

        if flat.entity_bytes is None:
            tag = fxtr_id.get_tag()
            return Response(
                f"Derived node (tag={tag}, no content)\nDeps: {flat.deps}",
                media_type="text/plain",
            )

        decoded = fxtr_cbor_loads(flat.entity_bytes)
        text = pprint.pformat(decoded, width=100)
        return Response(text, media_type="text/plain")

    async def data(request: Request) -> Response:
        if err := await _check_scope(request):
            return err

        id_string = request.path_params["id_string"]
        try:
            fxtr_id = FxtrID.from_id_string(id_string, allow_provenance=False)
        except (ValueError, KeyError):
            return Response("Invalid ID", status_code=400)

        arena = await scope._deep_fetch(fxtr_id)
        if arena is None:
            return Response("Not found", status_code=404)

        try:
            with scope._current_auth_lease().clone() as lv:
                flat = await arena.load_flat(lv.clone(), fxtr_id)

                # Compute precomp closure for all deps + the node itself
                dep_ids = [dep_id for dep_id, _kind in flat.deps]
                dep_ids.append(fxtr_id)
                precomp = compute_precomp_closure(arena, lv.clone(), dep_ids)
        finally:
            arena.close()

        result: dict = {}
        if flat.entity_bytes is not None:
            result["bytes"] = flat.entity_bytes
        if precomp:
            result["precomp"] = precomp
        return Response(cbor2.dumps(result), media_type="application/cbor")

    async def pin(request: Request) -> Response:
        if err := await _check_scope(request):
            return err

        name = request.path_params["name"]
        fxtr_id = pins.get(name)
        if fxtr_id is None:
            return Response("Pin not found", status_code=404)

        # The precomp seed is the prov node for a layered pin (its closure is a
        # superset of the data node's — the data id falls out via `unwraps_to`),
        # the data node for a bare one. Never the data node for a layered pin.
        prov = fxtr_id._provenance_parent
        layered = provenanced and prov is not None
        seed = prov if layered else fxtr_id
        assert seed is not None

        # Fetch-or-fail: a pin name that resolves but whose entity the scope
        # can't serve is a server inconsistency, not a success — never a bare
        # 200 with no precomp (distinct from the 404 for an unknown pin name).
        arena = await scope._deep_fetch(seed)
        if arena is None:
            return Response("Pinned entity not fetchable", status_code=500)
        try:
            precomp: PrecompMap = compute_precomp_closure(
                arena, scope._current_auth_lease().clone(), [seed]
            )
        finally:
            arena.close()

        result: dict = {"id": encode_pin_value(fxtr_id, provenanced=provenanced)}
        if precomp:  # omitted only for a genuinely edge-less closure
            result["precomp"] = precomp
        return Response(cbor2.dumps(result), media_type="application/cbor")

    async def pins_list(request: Request) -> Response:
        if err := await _check_scope(request):
            return err

        prefix = request.query_params.get("prefix", "")
        filtered = {
            name: encode_pin_value(fid, provenanced=provenanced)
            for name, fid in pins.items()
            if name.startswith(prefix)
        }
        return Response(
            json.dumps(filtered),
            media_type="application/json",
        )

    routes = [
        Route("/preview/{id_string:path}", preview),
        Route("/data/{id_string:path}", data),
        Route("/pin/{name:path}", pin),
        Route("/pins", pins_list),
    ]

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]

    return Starlette(routes=routes, middleware=middleware)
