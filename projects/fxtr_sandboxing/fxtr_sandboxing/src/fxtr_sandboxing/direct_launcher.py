"""Direct (no-spawner) sandbox launcher.

Lower-level alternative to :func:`create_sandbox_environment` +
:class:`SandboxSpawnerProto`. Skips the bundle-prep + spawner-RPC
indirection so callers can:

- Build a long-lived :class:`eryx.SandboxFactory` themselves (typically
  ``packages=[stable wheels]`` + ``imports=[modules to pre-import]``
  for the warm-spawn case — pay the ~3 s factory cost once, then
  ~10–20 ms per ``create_sandbox``).
- Mount fresh volumes per spawn for content that changes (local
  packages, the user's source tree).
- Drive entry points by ``module:function`` string the same way
  :class:`TaskBundle.entry_point` does.

Used by the workstation MCP plugin spike to run user-supplied scripts
inside the sandbox without going through :func:`prepare` /
``SandboxSpawnerImpl``. Not a replacement for the spawner — anything
that needs the bundle/spawner ergonomics (cross-process bundle entities,
nested spawn through a sandbox-bridged spawner ref) should keep using
:func:`create_sandbox_environment`.

Lifetime nesting (mirrors :func:`create_sandbox_environment` minus the
transport server and the spawner indirection):

    caller scope
      └─ create_direct_sandbox_environment (just owns the factory ref)
           └─ open_sandbox CM
                └─ per-sandbox task group
                     └─ _runner task
                          └─ run_in_factory CM (eryx sandbox + JSON callbacks)
                               └─ open_rpc_scope conn to one guest process
                                    └─ guest's GuestMain mounted on guest_host
                yields ServiceRef[SandboxConnectProto[FxtrServiceData,
                                                      UnknownService]]
                bridged into a per-call FxtrScope.

Per-call scope choice: each ``open_sandbox`` opens its own
:class:`FxtrScope` for the bridged ref, so the sandbox is guaranteed to
fully tear down at CM exit regardless of what the caller does with the
ref. This is a stronger lifetime contract than ``SandboxSpawnerProto.spawn``
(which bridges into the active scope and lets the caller's ref lifetime
control teardown).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING

import anyio
import anyio.abc
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr_sandbox_types import (
    DirectLauncherSentinel,
    JSONObjectStream,
    SandboxConnectProto,
    SandboxGuestImplProto,
)

from .runner import run_in_factory
from .spawner import _SpawnConnectWrapperImpl

if TYPE_CHECKING:
    import eryx


class DirectSandboxEnvironment:
    """Returned by :func:`create_direct_sandbox_environment`.

    Owns a long-lived :class:`eryx.SandboxFactory`. Each
    :meth:`open_sandbox` call spawns a fresh sandbox off it.

    Construction is private — use :func:`create_direct_sandbox_environment`.
    """

    def __init__(self, *, factory: eryx.SandboxFactory) -> None:
        self._factory = factory

    @contextlib.asynccontextmanager
    async def open_sandbox(
        self,
        *,
        entry_point: str,
        volumes: Sequence[tuple[str, str, bool]] = (),
        sys_path_additions: Sequence[str] = (),
    ) -> AsyncIterator[ServiceRef[SandboxConnectProto[FxtrServiceData, UnknownService]]]:
        """Open one sandbox; yield its connect ref bridged into a per-call scope.

        The yielded ref is the same shape ``SandboxSpawnerProto.spawn``
        returns, so callers ``replace_service_type(SandboxConnectProto[Cfg,
        Proto])`` + ``resolve()`` + ``connect(cfg)`` exactly the same way.

        Args:
            entry_point: ``module:function`` string. The module must be
                importable from the sandbox's ``sys.path`` (either baked
                into the factory snapshot or reachable through one of
                ``volumes`` + ``sys_path_additions``).
            volumes: Extra host dirs to mount into the sandbox.
                Each entry is ``(host_path, guest_path, read_only)``:
                ``host_path`` is an absolute path on the host (a string
                — eryx does not accept ``Path`` objects here);
                ``guest_path`` is where the dir appears inside the
                sandbox's WASI filesystem (e.g. ``"/local-pkgs"``);
                ``read_only`` is a bool. Passed to
                :func:`~fxtr_sandboxing.runner.run_in_factory`, which
                calls ``factory.create_sandbox`` with ``volumes=`` and the same
                ``resource_limits`` policy as :func:`run_in_factory` — see the eryx
                ``VolumeMount`` docs for the underlying cap-std
                semantics.
            sys_path_additions: Guest paths (typically the
                ``guest_path`` of one of ``volumes``) inserted at the
                front of ``sys.path`` before the adapter runs. Use this
                when a volume-mounted dir contains importable packages
                that ``entry_point`` needs to reach.

        Sandbox lifetime is bounded by this CM: at exit, the per-call
        :class:`FxtrScope` drains, which releases the bridged ref, which
        drains the rpc scope, which exits :func:`run_in_factory`, which
        tears down the eryx sandbox process.
        """
        teardown_event = anyio.Event()

        async def _runner(
            *,
            task_status: anyio.abc.TaskStatus[
                tuple[
                    ServiceRef[SandboxGuestImplProto[FxtrServiceData, UnknownService]],
                    FxtrScope,
                ]
            ],
        ) -> None:
            async with run_in_factory(
                self._factory,
                entry_point=entry_point,
                volumes=list(volumes),
                sys_path_additions=list(sys_path_additions),
            ) as session:
                json_stream = JSONObjectStream(session.send, session.receive)
                rpc_message_stream = wrap_json_stream(json_stream)
                async with open_rpc_scope(
                    rpc_message_stream,
                    remote_service_type=SandboxGuestImplProto[FxtrServiceData, UnknownService],
                    name="direct_sandbox_rpc",
                ) as rpc_session:
                    task_status.started((rpc_session.remote_service, rpc_session.scope))
                    await teardown_event.wait()

        async with anyio.create_task_group() as sandbox_tg:
            raw_guest_ref, session_scope = await sandbox_tg.start(_runner)
            try:
                async with FxtrScope(
                    name="direct_sandbox_call",
                    # GLOBAL_SCOPE makes the system-entity type-descriptor
                    # for ``DirectLauncherSentinel`` reachable when we
                    # ``.store()`` the sentinel below. With ``isolated=False``
                    # the currently-active scope is *also* an implicit
                    # parent, so callers whose own scope chain already
                    # reaches GLOBAL_SCOPE see no behavior change — the
                    # extra include is harmless. Callers whose active
                    # scope chain is isolated-from-GLOBAL_SCOPE (e.g. the
                    # workstation MCP plugin's ``script_scope``) used to
                    # crash here at sentinel-store time before this was
                    # included.
                    include=[GLOBAL_SCOPE],
                ) as call_scope:
                    # ``bridge_from_scope`` used to return a helper object
                    # exposing a ``.ref``; current fxtr2 has it install the
                    # bridge in-place and return ``None``. The raw guest
                    # ref is now valid in ``call_scope``.
                    await raw_guest_ref.bridge_from_scope(
                        session_scope, into_scope=call_scope, keepalive_via="independent_lease"
                    )
                    # direct_launcher has no real bundle entity, so it
                    # stores a sentinel and threads that as the bundle
                    # for the guest's per-call provenance c-node. The
                    # resulting marker says "spawned via
                    # direct_launcher" rather than pointing at a real
                    # bundle.
                    sentinel_ref = DirectLauncherSentinel().store(call_scope)
                    bundle_unk_ref: EntityRef[UnknownEntity] = EntityRef(
                        entity_id=sentinel_ref.entity_id, target_type=UnknownEntity
                    )
                    wrapper = _SpawnConnectWrapperImpl(
                        raw_guest_ref=raw_guest_ref, bundle=bundle_unk_ref
                    )
                    wrapper_ref = await call_scope.mount(wrapper)
                    yield wrapper_ref.replace_service_type(
                        SandboxConnectProto[FxtrServiceData, UnknownService]
                    )
                # call_scope drained → wrapper unmounted → raw_guest_ref
                # bridge released → rpc_session.scope can drain. The
                # runner task is still parked on teardown_event; release
                # it so it can exit the rpc scope CM (which then drains
                # cleanly since no bridges remain) and tear down the
                # sandbox.
            finally:
                teardown_event.set()
        # sandbox_tg exit waits for _runner to finish → sandbox process down.


@contextlib.asynccontextmanager
async def create_direct_sandbox_environment(
    *,
    factory: eryx.SandboxFactory,
) -> AsyncIterator[DirectSandboxEnvironment]:
    """Open a direct-launcher environment around a pre-built factory.

    The factory is owned by the caller — the env wraps it for the
    duration of the CM but does no cleanup on it (a
    :class:`eryx.SandboxFactory` is just a Python object holding the
    precompiled bytes; nothing async to release). Callers can hold one
    factory across many envs if they like.

    Right now this CM is essentially a typed wrapper — it exists so we
    can grow env-level state (transport server, exit stack, background
    task group) the same way :func:`create_sandbox_environment` does
    without breaking the API later.
    """
    yield DirectSandboxEnvironment(factory=factory)
