"""Sandboxed Claude Haiku chat service (wasm flavor).

A minimal LLM-calling service: ``ask(message)`` sends a single
user message to Claude Haiku and returns the assistant's text as a
plain string.

This is the **python-only-sandbox** half of the demo's
"talk to the network from a sandboxed service" pair (the container
flavor is ``packages/haiku_service_oci/``). The flake builds this
directory as ``fxtr.services.haiku_service_wasm`` (via
``mkWasmService``); the agent spawns it **by committed git
reference** and then ``.connect(...)``s it with the workstation's
``anthropic-authorizer`` — a ``NetworkHandleAuthorizerProto`` ref
that pins upstream to ``https://api.anthropic.com`` and host-side
injects the ``x-api-key`` + ``anthropic-version`` headers, so this
code never sees the API key. (The backend registers it only when
launched with ``--allow-llm-apis`` and ``ANTHROPIC_API_KEY`` set.)

The HTTP call is made with raw ``httpx`` (via
``fxtr_sandbox_rpc_adapter.get_client``) rather than the
``anthropic`` SDK: the SDK pulls in ``jiter`` / ``pydantic-core``,
native Rust extensions with no wasm wheels, so it can't survive the
wasi-restricted lock. The container flavor exists to show the
contrast — there the real SDK is a normal dependency.

Agent-side usage (from ``run_script``, after ``git add -A && git
commit``):

.. code-block:: python

    from _fxtr_mcp_builtin import (
        NixWasmServiceDefn, current_commit,
        lookup_service_by_name, schedule_invoke,
    )

    spawner = await lookup_service_by_name("sandbox-spawner")
    defn = NixWasmServiceDefn(
        git_commit=await current_commit(),
        flake_attr="fxtr.services.haiku_service_wasm",
    ).store()
    await schedule_invoke("haiku_running", spawner, lambda s: s.spawn(defn))
    # wait for haiku_running ...

.. code-block:: python

    from _fxtr_mcp_builtin import (
        lookup_service_by_name, retrieve_result, schedule_invoke,
    )

    wrapper = await retrieve_result("haiku_running")
    anthropic = await lookup_service_by_name("anthropic-authorizer")
    await schedule_invoke("haiku_svc", wrapper, lambda s: s.connect(anthropic))
    # wait for haiku_svc ...

.. code-block:: python

    from _fxtr_mcp_builtin import retrieve_result, schedule_invoke

    svc = await retrieve_result("haiku_svc")
    await schedule_invoke(
        "haiku_answer", svc, lambda s: s.ask("Say hello in one short sentence.")
    )
    # wait for haiku_answer, then retrieve_result("haiku_answer")
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import fxtr_sandbox_rpc_adapter
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto

#: Namespace UUID stable across host + sandbox so the wire-form
#: ``fxtr_type`` FxtrID matches across processes.
HAIKU_SERVICE_WASM_NS = uuid.UUID("9f3a1c7e-2b48-4d6a-9c0f-7e5b1a8d2f64")

HAIKU_SERVICE_WASM_TYPE_ID = system_entities.define_type(
    HAIKU_SERVICE_WASM_NS, "haiku_service_wasm"
)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_TOKENS = 1024


@service_protocol
class HaikuServiceProto(Protocol):
    """Single-turn Claude Haiku chat."""

    async def ask(self, message: str) -> str: ...


class _Impl(TypedServiceImpl[HaikuServiceProto]):
    def __init__(
        self,
        anthropic_authorizer: ServiceRef[NetworkHandleAuthorizerProto],
    ) -> None:
        self._anthropic_authorizer = anthropic_authorizer

    def _target_type_annotation(self) -> Any:
        return HaikuServiceProto

    def _service_type_id(self) -> FxtrID:
        return HAIKU_SERVICE_WASM_TYPE_ID

    @service_method
    async def ask(self, message: str) -> str:
        # Routing is decided at token-mint time on the host (the
        # authorizer pins the upstream), so the base_url's host part
        # is cosmetic — but it must be plain http: TLS to the real
        # upstream is terminated host-side, the in-sandbox leg is
        # the raw tunneled byte stream.
        client = fxtr_sandbox_rpc_adapter.get_client(
            self._anthropic_authorizer,
            base_url="http://api.anthropic.com",
        )
        async with client:
            resp = await client.post(
                "/v1/messages",
                json={
                    "model": _HAIKU_MODEL,
                    "max_tokens": _DEFAULT_MAX_TOKENS,
                    "messages": [{"role": "user", "content": message}],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
        # The Messages API returns a list of content blocks; join the
        # text blocks (a simple single-turn reply is one text block).
        parts = [b["text"] for b in data["content"] if b.get("type") == "text"]
        return "\n".join(parts).strip()


async def run(
    config: ServiceRef[NetworkHandleAuthorizerProto],
) -> ServiceRef[HaikuServiceProto]:
    """``fxtr.service`` entry point.

    The config is the anthropic authorizer ref itself (same shape as
    the sandboxing transporter examples). We mount the impl on the
    active (connect) scope and return the ref; the framework bridges
    it back to the caller.
    """
    return await get_active_scope().mount(_Impl(anthropic_authorizer=config))
