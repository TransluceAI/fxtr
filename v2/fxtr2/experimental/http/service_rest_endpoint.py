"""REST (GET/POST) invocation endpoint for stateless services.

One route per service. Invocation encodings (wire protocol
``projects/fxtr_workstation/plans/phase0/part7a_stateless_ws_server/design/01_wire_protocol.md``):

- ``GET {path}?method=<name>&payload=<urlencoded JSON {args, kwargs}>``
  — CDN-cacheable for deterministic services.
- ``POST {path}`` with JSON body ``{method, args, kwargs}``.

Success → ``200`` with the resolved-mode-escaped result as the JSON
body. Failure → non-2xx with JSON ``{message}`` (no ``error_type`` —
decision ``…/part7a_stateless_ws_server/open_questions.md`` Q-K).
Callback args are legal but inert over http — there is no ``notify`` on
the REST context, so a ``["callback", uuid]`` arg decodes to a
``CallbackAddress`` the impl simply ignores.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .service_codec import ServiceCodecError, escape_value, unescape_args
from .service_dispatch import ServiceImpl, UnknownServiceMethodError, invoke_impl

if TYPE_CHECKING:
    from fxtr2.data.entity_ref_scope import FxtrScope


@dataclass(frozen=True)
class RestServiceContext:
    """``ctx`` handed to a REST service impl. ``scope`` lets the impl
    load entity args; ``request`` exposes headers / query for impls
    that want them. No ``notify`` / ``transient`` (http has no channel
    back)."""

    scope: FxtrScope
    request: Request


class _BadRequest(Exception):
    """Internal: a malformed REST request (→ 400)."""


def _error(status: int, message: str) -> Response:
    return JSONResponse({"message": message}, status_code=status)


async def _parse_request(request: Request) -> tuple[str, Any]:
    """Parse the method + payload from a GET query or POST body.

    Args:
        request: The incoming Starlette request.

    Returns:
        A ``(method, payload)`` tuple where ``payload`` is the
        ``{args, kwargs}`` object the codec unescapes.

    Raises:
        _BadRequest: On a missing method or malformed query/body.
    """
    if request.method == "GET":
        method = request.query_params.get("method")
        if not method:
            raise _BadRequest("missing 'method' query param")
        raw = request.query_params.get("payload")
        if raw is None:
            return method, {}
        try:
            return method, json.loads(raw)
        except json.JSONDecodeError as e:
            raise _BadRequest(f"invalid 'payload' JSON: {e}") from e
    # POST (and any other method routed here).
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001 — any decode failure is a bad request
        raise _BadRequest(f"invalid JSON body: {e}") from e
    if not isinstance(body, Mapping):
        raise _BadRequest("POST body must be a JSON object")
    method = body.get("method")
    if not isinstance(method, str):
        raise _BadRequest("missing/invalid 'method' in body")
    return method, {"args": body.get("args", []), "kwargs": body.get("kwargs", {})}


def make_rest_handler(impl: ServiceImpl, *, scope: FxtrScope, provenanced: bool = False):
    """Build the Starlette endpoint handler for a single REST service.

    Args:
        impl: The service impl (callable or method-map).
        scope: The fxtr scope handed to the impl via ``ctx`` (for
            loading entity args).
        provenanced: When ``True``, layered entity refs in the result emit
            the layered form; when ``False`` (default) they emit bare.

    Returns:
        An async ``handler(request) -> Response`` that parses, invokes,
        escapes the result, and maps failures to non-2xx ``{message}``.
    """

    async def handler(request: Request) -> Response:
        try:
            method, payload = await _parse_request(request)
            args, kwargs = unescape_args(payload)
        except (_BadRequest, ServiceCodecError) as e:
            return _error(400, str(e))

        ctx = RestServiceContext(scope=scope, request=request)
        try:
            result = await invoke_impl(impl, method, args, kwargs, ctx)
        except UnknownServiceMethodError as e:
            return _error(400, str(e))
        except ServiceCodecError as e:
            # An impl that builds an invalid value surfaces as a request
            # error; a genuine impl failure falls through to 500 below.
            return _error(400, str(e))
        except Exception as e:  # noqa: BLE001 — impl failure → 500 {message}
            return _error(500, str(e))

        try:
            # http: no register_transient (transients are ws-only).
            body = escape_value(result, provenanced=provenanced)
        except ServiceCodecError as e:
            return _error(500, f"result encode failed: {e}")
        return JSONResponse(body)

    return handler


def rest_service_route(
    path: str,
    impl: ServiceImpl,
    *,
    scope: FxtrScope,
    method: str = "POST",
    provenanced: bool = False,
) -> Route:
    """Build a ready Starlette ``Route`` for a single REST service.

    Args:
        path: The route path to mount the service at.
        impl: The service impl (callable or method-map).
        scope: The fxtr scope handed to the impl via ``ctx``.
        method: ``"GET"`` or ``"POST"`` (the service is fixed to one).
        provenanced: When ``True``, layered entity refs in results emit the
            layered form; when ``False`` (default) they emit bare.

    Returns:
        A Starlette ``Route`` serving ``method`` at ``path``.
    """
    return Route(
        path, make_rest_handler(impl, scope=scope, provenanced=provenanced), methods=[method]
    )
