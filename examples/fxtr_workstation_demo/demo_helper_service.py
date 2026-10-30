"""Host-side ``WorkstationDemoHelper`` service for the self-contained demo driver.

Implements :class:`demo_helper.WorkstationDemoHelperProto` (same shape, by method
name). The helper is a stand-in for "ask an agent to spawn service X and run Y":
its methods do the host-side plumbing a human can't do from the browser —
``spawn``-by-git-ref + ``connect`` (returning a live service ref), plus entity
factories for invoke args. The browser-side driver renderer
(``agent_workdir/renderers/demo_helper/``) calls these, then invokes the
*interesting* methods (``echo`` / ``ask`` / ``run_batched``) directly on the
returned refs and presents every result as a clickable ``RefLink``.

Mount via :func:`mount_demo_helper` (async CM, same shape as
:mod:`report_builder_service`'s ``mount_report_builder``), but constructed inside
``run_demo_workstation``'s ``create_sandbox_environment`` block so it gets the
sandbox ``spawner_ref`` + ``host_scope`` (and the optional ``anthropic-authorizer``
ref). Pair the yielded ``(service_ref, scope)`` with an ``ExternalServiceConfig``.

Spawn lifetime: each spawn ``bridge_from_scope``s the connect **wrapper** into the
helper's long-lived mount scope (``keepalive_via="independent_lease"``). Holding the
wrapper keeps the spawned sandbox alive (see ``fxtr_sandboxing.spawner`` — the
wrapper pins ``raw_guest_ref``'s chain-bridge), so the connected ref returned to the
caller stays valid for later invokes. All spawned sandboxes are released when the
helper unmounts (relaunch the demo to reset).
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types import (
    NixContainerServiceDefn,
    NixWasmServiceDefn,
    SandboxConnectProto,
)

from delayed_echo import DelayedEchoConfig
from demo_helper import DEMO_HELPER_TYPE_ID, WorkstationDemoHelperProto
from presentables import Note
from simple_tasks import PromptTemplate

# Flake attrs the helper spawns (committed git ref = the runtime workdir's HEAD,
# resolved against the spawner's ``git_repo_path``). Same attrs the agent uses.
_DELAYED_ECHO = "fxtr.services.delayed_echo"
_SIMPLE_SCHEDULER = "fxtr.services.simple_scheduler"
_SIMPLE_TASK_LIB = "fxtr.services.simple_task_lib"
_STREAMING_SCHEDULER = "fxtr.services.streaming_scheduler"
_STREAMING_TASK_LIB = "fxtr.services.streaming_task_lib"
_HAIKU_WASM = "fxtr.services.haiku_service_wasm"
_STREAMING_PROMPT_OCI = "fxtr.services.streaming_prompt_task_oci"

# str.format-style template for the vivid-description scenario (substitutes
# {title} / {body} from each input note). Built inline so the host needn't import
# the OCI task package just to reuse its ready-made template.
_VIVID_TEMPLATE = (
    "Write a vivid, sensory description inspired by the note titled '{title}'.\n\n"
    "{body}\n\n"
    "Respond with two or three evocative sentences."
)

_NO_AUTHORIZER_MSG = (
    "no 'anthropic-authorizer' is registered — launch the demo backend with "
    "--allow-llm-apis and ANTHROPIC_API_KEY set to exercise this scenario."
)


class WorkstationDemoHelperImpl(TypedServiceImpl[WorkstationDemoHelperProto]):
    """See module docstring + :class:`demo_helper.WorkstationDemoHelperProto`."""

    def __init__(
        self,
        *,
        workdir: Any,
        head_commit: str,
        spawner_ref: ServiceRef[Any],
        holding_scope: FxtrScope,
        anthropic_authorizer: ServiceRef[Any] | None,
    ) -> None:
        self._workdir = workdir
        self._head = head_commit
        self._spawner_ref = spawner_ref
        # The impl is mounted on this scope; spawned wrappers are retained here so
        # they (and their sandboxes) outlive individual invokes. ``spawner_ref`` +
        # ``anthropic_authorizer`` are bridged in here at mount time so per-invoke
        # call scopes (children of this one) can resolve them.
        self._holding_scope = holding_scope
        self._anthropic = anthropic_authorizer
        self._spawned: list[ServiceRef[Any]] = []

    def _target_type_annotation(self) -> Any:
        return WorkstationDemoHelperProto

    def _service_type_id(self) -> Any:
        return DEMO_HELPER_TYPE_ID

    # ----- introspection -----

    @service_method
    async def workdir_path(self) -> str:
        return str(self._workdir)

    @service_method
    async def current_commit(self) -> str:
        return self._head

    # ----- entity factories (invoke args the browser can't mint) -----

    @service_method
    async def make_note(self, title: str, body: str) -> FxtrServiceData:
        return Note(title=title, body=body).store(get_active_scope())

    @service_method
    async def make_demo_inputs(self) -> FxtrServiceData:
        """A small fixed named set of ``Note``s for batch ``run_batched`` inputs.
        The keys are both the run's input names and the grid's row labels."""
        scope = get_active_scope()
        return {
            "calm sea": Note(title="Calm sea", body="A quiet tide breathing at dawn.").store(scope),
            "city night": Note(title="City night", body="Neon smeared across wet streets.").store(
                scope
            ),
        }

    @service_method
    async def make_inputs(self, notes: FxtrServiceData) -> FxtrServiceData:
        """Build a named inputs dict from a caller-supplied list of
        ``{"title", "body"}`` specs (keyed by title — both the run's input name
        and the grid row label). ``Note`` is content-addressed, so the same
        title/body yields the same ref across calls — which is what makes the
        streaming scheduler's per-cell cache hit on a re-run."""
        scope = get_active_scope()
        out: dict[str, Any] = {}
        for spec in notes:  # type: ignore[union-attr]  # list of {title, body} dicts
            title = str(spec["title"])
            body = str(spec.get("body", ""))
            out[title] = Note(title=title, body=body).store(scope)
        return out

    # ----- spawn + connect (return live connected ref(s)) -----

    async def _spawn_and_connect(
        self, flake_attr: str, config: FxtrServiceData, *, container: bool = False
    ) -> ServiceRef[Any]:
        """Spawn ``flake_attr`` by the workdir's committed HEAD, retain the connect
        wrapper in the mount scope, ``connect(config)``, and return the live ref.

        Uses the untyped-but-concrete ``SandboxConnectProto[FxtrServiceData,
        UnknownService]`` re-type (allowed by the proto) so the helper needn't
        import each service's protocol — the browser dispatches the returned ref by
        its own service metadata type id anyway.
        """
        call_scope = get_active_scope()
        spawner = await self._spawner_ref.resolve()
        defn_cls = NixContainerServiceDefn if container else NixWasmServiceDefn
        defn = defn_cls(git_commit=self._head, flake_attr=flake_attr).store(call_scope)
        wrapper_ref = await spawner.spawn(defn)
        await wrapper_ref.bridge_from_scope(
            call_scope,
            into_scope=self._holding_scope,
            keepalive_via="independent_lease",
        )
        self._spawned.append(wrapper_ref)
        typed_wrapper = wrapper_ref.replace_service_type(
            SandboxConnectProto[FxtrServiceData, UnknownService]
        )
        wrapper = await typed_wrapper.resolve()
        return await wrapper.connect(config)

    @service_method
    async def spawn_delayed_echo(self, label: str) -> FxtrServiceData:
        cfg = DelayedEchoConfig(label=label).store(get_active_scope())
        return await self._spawn_and_connect(_DELAYED_ECHO, cfg)

    @service_method
    async def spawn_simple_workflow(self) -> FxtrServiceData:
        scheduler = await self._spawn_and_connect(_SIMPLE_SCHEDULER, {})
        task = await self._spawn_and_connect(_SIMPLE_TASK_LIB, "note_transform")
        return {"scheduler": scheduler, "task": task}

    @service_method
    async def spawn_streaming_workflow(self) -> FxtrServiceData:
        scheduler = await self._spawn_and_connect(_STREAMING_SCHEDULER, {})
        task = await self._spawn_and_connect(_STREAMING_TASK_LIB, "word_shout")
        return {"scheduler": scheduler, "task": task}

    @service_method
    async def spawn_haiku(self) -> FxtrServiceData:
        if self._anthropic is None:
            raise RuntimeError(_NO_AUTHORIZER_MSG)
        return await self._spawn_and_connect(_HAIKU_WASM, self._anthropic)

    @service_method
    async def spawn_vivid_workflow(self) -> FxtrServiceData:
        if self._anthropic is None:
            raise RuntimeError(_NO_AUTHORIZER_MSG)
        scheduler = await self._spawn_and_connect(_STREAMING_SCHEDULER, {})
        tmpl = PromptTemplate(label="vivid description", template=_VIVID_TEMPLATE).store(
            get_active_scope()
        )
        task = await self._spawn_and_connect(
            _STREAMING_PROMPT_OCI,
            {"authorizer": self._anthropic, "prompt_template": tmpl.entity_id},
            container=True,
        )
        return {"scheduler": scheduler, "task": task}


@dataclasses.dataclass(frozen=True)
class MountedDemoHelper:
    """Handle yielded by :func:`mount_demo_helper`."""

    scope: FxtrScope
    service_ref: ServiceRef[Any]
    impl: WorkstationDemoHelperImpl


@contextlib.asynccontextmanager
async def mount_demo_helper(
    *,
    workdir: Any,
    head_commit: str,
    spawner_ref: ServiceRef[Any],
    host_scope: FxtrScope,
    anthropic_authorizer: ServiceRef[Any] | None = None,
    scope_name: str = "demo_helper",
) -> AsyncGenerator[MountedDemoHelper, None]:
    """Open a scope, bridge the spawner (+ optional anthropic authorizer) in, mount
    a :class:`WorkstationDemoHelperImpl`, and yield the handle. Pair the
    ``(service_ref, scope)`` with an :class:`ExternalServiceConfig`.

    ``spawner_ref`` / ``anthropic_authorizer`` live on ``host_scope`` (the sandbox
    env's host scope); they're bridged into the helper's mount scope so per-invoke
    call scopes can resolve them, and so spawn defns/configs created in those call
    scopes can reach them.
    """
    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name=scope_name) as scope:
        await spawner_ref.bridge_from_scope(
            host_scope, into_scope=scope, keepalive_via="independent_lease"
        )
        if anthropic_authorizer is not None:
            await anthropic_authorizer.bridge_from_scope(
                host_scope, into_scope=scope, keepalive_via="independent_lease"
            )
        async with anyio.create_task_group() as impl_tg:
            del impl_tg  # parity with the other mount_* helpers; reserved
            impl = WorkstationDemoHelperImpl(
                workdir=workdir,
                head_commit=head_commit,
                spawner_ref=spawner_ref,
                holding_scope=scope,
                anthropic_authorizer=anthropic_authorizer,
            )
            service_ref = (await scope.mount(impl)).replace_service_type(WorkstationDemoHelperProto)
            yield MountedDemoHelper(scope=scope, service_ref=service_ref, impl=impl)


__all__ = [
    "MountedDemoHelper",
    "WorkstationDemoHelperImpl",
    "mount_demo_helper",
]
