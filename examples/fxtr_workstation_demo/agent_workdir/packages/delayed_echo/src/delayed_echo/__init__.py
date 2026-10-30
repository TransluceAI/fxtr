"""Demo sandboxed service for the assembly-stage demo.

A trivial delayed-echo service: takes a payload + a delay, sleeps
for ``delay`` seconds, then returns the payload unchanged. The flake
builds this directory as ``fxtr.services.delayed_echo`` (via
``mkWasmService``); the agent spawns it **by committed git reference** —
``spawner.spawn(NixWasmServiceDefn(git_commit=current_commit(),
flake_attr="fxtr.services.delayed_echo"))`` — then ``.connect(config)``s
the result to get the typed service.

The agent's full round-trip exercises the chain end-to-end:

- ``schedule_invoke`` to spawn → ``InvokeResultLogEvent`` in the
  chain when the service ref lands.
- ``schedule_invoke`` to call ``echo(value, delay)`` →
  another ``InvokeResultLogEvent`` after ``delay`` seconds.

Both events render in the frontend's stream view; the second is
the natural demo signal for "the agent did something async that
took a little while."

Config: :class:`DelayedEchoConfig` carries a ``label`` so the
agent can identify multiple spawned instances. The host stores it
as a fxtr2 entity, passes an :class:`EntityRef[DelayedEchoConfig]`
as the ``connect`` arg; the adapter decodes it against ``run``'s
annotation before invoking us.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

#: Namespace UUID stable across host + sandbox so the wire-form
#: ``fxtr_type`` FxtrID values match across processes.
DELAYED_ECHO_NS = uuid.UUID("5a8d2f17-9c41-4b6e-83f0-7e1d2c4b5a09")

DELAYED_ECHO_TYPE_ID = system_entities.define_type(DELAYED_ECHO_NS, "delayed_echo")


@dataclasses.dataclass(frozen=True)
class DelayedEchoConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(DELAYED_ECHO_NS, "delayed_echo_config"),
):
    """Per-spawn config: a free-form label the agent picks. Lets a
    single agent maintain multiple spawned instances and tell them
    apart in its own bookkeeping."""

    label: str


@service_protocol
class DelayedEchoProto(Protocol):
    """Echoes ``value`` back after ``delay`` seconds. ``value`` is
    untyped — the agent can pass primitives, dicts, refs, etc. The
    workstation's invoke-result event delivery brings it back
    intact (with ref leaves preserved as entity / service refs)."""

    async def echo(self, value: FxtrServiceData, delay: float) -> FxtrServiceData: ...


class _Impl(TypedServiceImpl[DelayedEchoProto]):
    def __init__(self, cfg: DelayedEchoConfig) -> None:
        self._cfg = cfg

    def _target_type_annotation(self) -> Any:
        return DelayedEchoProto

    def _service_type_id(self) -> FxtrID:
        return DELAYED_ECHO_TYPE_ID

    @service_method
    async def echo(self, value: FxtrServiceData, delay: float) -> FxtrServiceData:
        if delay > 0:
            await anyio.sleep(delay)
        return value


async def run(config: EntityRef[DelayedEchoConfig]) -> ServiceRef[DelayedEchoProto]:
    """``fxtr.service`` entry point.

    The active scope at call time is the ``connect`` dispatch
    scope — the config ref is already bridged in. We load the
    config, mount our impl on the scope, and return the ref; the
    framework chain-bridges it back to the caller (the
    workstation's invoke worker, for our use case).
    """
    cfg = await config.load()
    return await get_active_scope().mount(_Impl(cfg))
