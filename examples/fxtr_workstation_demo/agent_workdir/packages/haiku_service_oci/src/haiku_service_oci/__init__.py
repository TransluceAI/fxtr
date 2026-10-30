"""Sandboxed Claude Haiku chat service (container/OCI flavor).

The same ``ask(message) -> str`` surface as
``packages/haiku_service_wasm/``, but packaged as a **container**:
the flake builds this directory as ``fxtr.services.haiku_service_oci``
via ``mkContainerService`` (a nix2container image whose ``Cmd``
launches the fxtr service runner), and the agent spawns it with
``NixContainerServiceDefn`` instead of ``NixWasmServiceDefn``.

Why a container flavor exists: the image runs **real Linux CPython**,
so native-extension dependencies are ordinary deps. This service
depends on the actual ``anthropic`` SDK (jiter / pydantic-core and
all — impossible in the wasm flavor, which has to hand-roll the API
call with httpx) and uses it directly.

Network access is the same capability story as the wasm flavor: the
container runs with no network of its own, and every connection
flows through the ``connect``-time authorizer ref — the
workstation's ``anthropic-authorizer``, which pins upstream to
``https://api.anthropic.com`` and host-side injects the real
``x-api-key`` (overwriting whatever the SDK set), so this code never
sees the API key. The SDK just rides a custom httpx client
(``fxtr_sandbox_rpc_adapter.get_client``) whose connections tunnel
through the sandbox transport.

Agent-side usage (from ``run_script``, after ``git add -A && git
commit``) — identical to the wasm flavor except for the defn type
and flake attr:

.. code-block:: python

    from _fxtr_mcp_builtin import (
        NixContainerServiceDefn, current_commit,
        lookup_service_by_name, schedule_invoke,
    )

    spawner = await lookup_service_by_name("sandbox-spawner")
    defn = NixContainerServiceDefn(
        git_commit=await current_commit(),
        flake_attr="fxtr.services.haiku_service_oci",
    ).store()
    await schedule_invoke("haiku_running", spawner, lambda s: s.spawn(defn))
    # wait for haiku_running (first spawn builds + loads the image — slow) ...

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

import anthropic
import fxtr_sandbox_rpc_adapter
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto

#: Namespace UUID stable across host + sandbox so the wire-form
#: ``fxtr_type`` FxtrID matches across processes.
HAIKU_SERVICE_OCI_NS = uuid.UUID("4c8e2d9a-6b31-4f7e-9a2d-8c5f0e1b7a43")

HAIKU_SERVICE_OCI_TYPE_ID = system_entities.define_type(HAIKU_SERVICE_OCI_NS, "haiku_service_oci")

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
        return HAIKU_SERVICE_OCI_TYPE_ID

    @service_method
    async def ask(self, message: str) -> str:
        # The SDK rides a custom httpx client whose every connection
        # tunnels through the authorizer's transport side-channel.
        # Routing is decided at token-mint time on the host (the
        # authorizer pins the upstream), so the base_url's host part
        # is cosmetic — but it must be plain http: TLS to the real
        # upstream is terminated host-side, the in-sandbox leg is the
        # raw tunneled byte stream. The api_key is a placeholder: the
        # host proxy overwrites `x-api-key` with the real key.
        http_client = fxtr_sandbox_rpc_adapter.get_client(
            self._anthropic_authorizer,
            base_url="http://api.anthropic.com",
        )
        async with http_client:
            sdk = anthropic.AsyncAnthropic(
                api_key="key-injected-by-host-authorizer",
                base_url="http://api.anthropic.com",
                http_client=http_client,
            )
            resp = await sdk.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=_DEFAULT_MAX_TOKENS,
                messages=[{"role": "user", "content": message}],
            )
        # Join the text blocks (a simple single-turn reply is one
        # text block).
        parts = [b.text for b in resp.content if b.type == "text"]
        return "\n".join(parts).strip()


async def run(
    config: ServiceRef[NetworkHandleAuthorizerProto],
) -> ServiceRef[HaikuServiceProto]:
    """``fxtr.service`` entry point.

    The config is the anthropic authorizer ref itself (same shape as
    the wasm flavor). We mount the impl on the active (connect) scope
    and return the ref; the framework bridges it back to the caller.
    """
    return await get_active_scope().mount(_Impl(anthropic_authorizer=config))
