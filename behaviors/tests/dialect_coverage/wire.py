"""Where each top-level request parameter goes on the wire, as the provider's SDK sends it.

A request TypedDict lists every parameter of the SDK's `create` call, but the SDK sends some of
them as headers (Anthropic's `betas` as `anthropic-beta`, `workspace_id` as
`anthropic-workspace-id`), which only the method's code says. Each parameter is sent through the
SDK into a transport that records the request instead of a hand-kept list, so a header parameter
a new SDK version adds is found without anyone noticing it.
"""

import copy
import json
from collections.abc import Callable
from typing import Any, cast

import httpx2

from .audit import SchemaPath, Unconstructible, build_witness
from .schema import JSONSchema

# Sends a request body through the SDK's `create` call, over the given HTTP client.
Send = Callable[[httpx2.Client, dict[str, Any]], object]

# Headers that follow from the body rather than from a parameter.
_DERIVED = frozenset({"content-length"})


def mark_header_parameters(doc: JSONSchema, send: Send) -> JSONSchema:
    """`doc` with each top-level parameter the SDK sends as a header marked `x-header` (the
    header's name), in place."""
    root = cast("JSONSchema", doc["$defs"][cast("str", doc["$ref"]).rsplit("/", 1)[-1]])
    properties = cast("dict[str, JSONSchema]", root.get("properties") or {})
    for key, schema in properties.items():
        try:
            witness = build_witness(doc, SchemaPath(key, (("prop", key),)))
        except Unconstructible:
            continue  # the audit records the path as unprobed
        request = _record(send, witness.body)
        if key in json.loads(request.content):
            continue
        without = copy.deepcopy(witness.body)
        del without[key]
        baseline = _record(send, without)
        headers = sorted(
            name
            for name, value in request.headers.items()
            if name not in _DERIVED and baseline.headers.get(name) != value
        )
        if not headers:
            raise ValueError(f"the SDK sends `{key}` neither in the body nor as a header")
        schema["x-header"] = ", ".join(headers)
    return doc


def _record(send: Send, body: dict[str, Any]) -> httpx2.Request:
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(400, json={"error": {"type": "probe", "message": "probe"}})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as http:
        try:
            send(http, body)
        except Exception:  # the SDK raises on the canned error; the request is what is wanted
            pass
    if not sent:
        raise ValueError(f"the SDK sent no request for {sorted(body)}")
    return sent[-1]
