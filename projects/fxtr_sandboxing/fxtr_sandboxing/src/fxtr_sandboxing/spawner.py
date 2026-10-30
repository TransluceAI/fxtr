"""Host-side sandbox-spawner service + ``create_sandbox_environment`` async CM.

Replaces the deleted ``rpc_runner.py``. Callers no longer drive a "run this
bundle" CM directly — instead they open a sandbox environment, get a
``ServiceRef[SandboxSpawnerProto]``, and use ``spawn(bundle)`` then
``replace_service_type(SandboxConnectProto[...])`` (see
:class:`~fxtr_sandbox_types.protocols.SandboxSpawnerProto`) to launch
sandboxes through the same fxtr2 RPC plumbing the rest of the system uses.

The spawner shape mirrors :mod:`v2.examples.service_spawner.service_spawner_example`
but runs against real eryx (and real containers) instead of an in-process
memory-stream pair. ``spawn`` dispatches on bundle type: the live git-ref
defns (``NixWasmServiceDefn`` → eryx via :mod:`fxtr_sandboxing.runner`,
``NixContainerServiceDefn`` → :mod:`fxtr_sandboxing.nix_container_runner`)
plus the deprecated in-band-bytes ``TaskBundle`` (WASM only). The
kind-agnostic ``NixServiceDefn`` auto-dispatches: the spawner evals the
attr's ``kind`` (cached per env per triple) and runs the matching arm.

The :class:`SandboxEnvironment` returned by ``create_sandbox_environment``
also exposes ``add_transport`` / ``add_raw_transport`` (stage 3): each
mounts a :class:`NetworkHandleAuthorizerImpl` on the env's host scope and
returns a ``ServiceRef[NetworkHandleAuthorizerProto]`` that can be bridged
into sandboxes to give them controlled network access through the
env-owned :class:`TransportConnectionServer`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import aiohttp
import anyio
import anyio.abc
import eryx
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef, get_active_scope
from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_nix_runner import Backend as NixBackend
from fxtr_nix_runner import BuilderTransportError, DeliveredImage
from fxtr_nix_runner import Handle as NixHandle
from fxtr_nix_runner import resolve_backend
from fxtr_nix_runner.perf import perf_span, record
from fxtr_sandbox_types import (
    JSONObjectStream,
    NetworkHandleAuthorizerProto,
    NixContainerServiceDefn,
    NixPyodideServiceDefn,
    NixServiceDefn,
    NixWasmServiceDefn,
    SandboxConnectProto,
    SandboxGuestImplProto,
    SandboxSpawnerProto,
    SocketJSONStream,
    TaskBundle,
)
from fxtr_sandbox_types.protocols import SANDBOX_CONNECT_TYPE_ID, SANDBOX_SPAWNER_TYPE_ID

from . import nix
from ._container_common import get_container_runtime
from .network_authorizer import NetworkHandleAuthorizerImpl
from .nix_container_runner import _run_nix_container_internal
from .proxy import PassthroughHostsConfig, TransportRoute
from .pyodide_runner import _run_pyodide_internal
from .runner import _check_and_maybe_repair_eryx_embedded_dir
from .runner import run as _run_wasm
from .runner import run_in_factory
from .transport_server import (
    TransportConnectionServer,
    TransportHandler,
    make_http_route_handler,
    make_passthrough_http_handler,
)

logger = logging.getLogger(__name__)

#: Result type of a build closure passed to ``_build_with_recycle``.
_BuildT = TypeVar("_BuildT")

#: What a ``_spawn_runner_*`` task reports via ``task_status.started``: the ref
#: for the guest's main service plus the RPC session's mountable scope.
_SpawnStartedValue = tuple[
    ServiceRef[SandboxGuestImplProto[FxtrServiceData, UnknownService]], FxtrScope
]

#: A git-ref defn asserting a concrete runtime kind.
_ConcreteNixDefn = NixWasmServiceDefn | NixContainerServiceDefn | NixPyodideServiceDefn

#: Any git-ref defn shape (the concrete kinds + the kind-agnostic auto form) —
#: they share the (git_commit, flake_attr, flake_path) triple.
_AnyNixDefn = _ConcreteNixDefn | NixServiceDefn

#: The `kind` discriminator values and the concrete defn each dispatches as.
_DEFN_CLASS_FOR_KIND: dict[str, type[_ConcreteNixDefn]] = {
    "wasm": NixWasmServiceDefn,
    "container": NixContainerServiceDefn,
    "pyodide": NixPyodideServiceDefn,
}


def _concrete_defn_for_kind(defn: NixServiceDefn, kind: str) -> _ConcreteNixDefn:
    """The concrete defn a kind-agnostic reference dispatches as.

    Raises:
        RuntimeError: ``kind`` is not a known discriminator value (a flake
            newer than this spawner, or a hand-built attr with a typo'd
            ``kind``).
    """
    defn_cls = _DEFN_CLASS_FOR_KIND.get(kind)
    if defn_cls is None:
        raise RuntimeError(
            f"spawn: service attr {defn.flake_attr!r} at commit "
            f"{defn.git_commit} declares unknown kind {kind!r}; known kinds: "
            f"{', '.join(sorted(_DEFN_CLASS_FOR_KIND))}."
        )
    return defn_cls(
        git_commit=defn.git_commit, flake_attr=defn.flake_attr, flake_path=defn.flake_path
    )


class _SpawnRunnerStatus:
    """Wrapper around a runner's ``task_status`` recording whether ``started``
    has been reported.

    That's the containment boundary used by
    :func:`_contain_failures_after_started`: before ``started``, a failure
    belongs to the ``spawn()`` call (``env_tg.start`` re-raises it as the
    spawn invoke's error); after ``started``, it belongs to the running spawn.
    """

    def __init__(self, inner: anyio.abc.TaskStatus[_SpawnStartedValue]) -> None:
        self._inner = inner
        self.was_started = False

    def started(self, value: _SpawnStartedValue) -> None:
        self._inner.started(value)
        self.was_started = True


@contextlib.asynccontextmanager
async def _contain_failures_after_started(
    spawn_desc: str, task_status: anyio.abc.TaskStatus[_SpawnStartedValue]
) -> AsyncIterator[_SpawnRunnerStatus]:
    """Blast-radius containment around a ``_spawn_runner_*`` body.

    One spawned sandbox failing while it runs — its guest aborting the RPC
    connection, a transport error mid-drain, the sandbox process dying under
    the exit path — must terminate *that spawn*, not escape into the
    environment task group, which would tear down every sibling spawn and,
    in an embedding server, the whole process lifespan.

    So: any ``Exception`` raised out of the body after ``started`` has been
    reported is logged and swallowed; the spawn's holders observe the death
    request-scoped, through their refs into the now-gone guest. Failures
    before ``started`` propagate unchanged — ``spawn()`` awaits
    ``env_tg.start(...)``, so e.g. nix build/eval errors still ride back as
    the spawn invoke's error. ``BaseException``s (cancellation) always
    propagate.
    """
    status = _SpawnRunnerStatus(task_status)
    try:
        yield status
    except Exception as exc:
        # Plain ``except Exception`` (not ``except*``): a bare ``raise`` inside
        # an ``except*`` clause re-raises the synthesized ``ExceptionGroup``,
        # not the original leaf, collapsing an informative build/eval error
        # (raised before ``started``) into an empty-messaged group whose
        # ``str`` is ``' (1 sub-exception)'``. Re-raising the original here
        # keeps the nix stderr intact as the spawn invoke's error. This still
        # catches ``ExceptionGroup``s for the after-``started`` swallow path
        # (they're ordinary exceptions); ``BaseException`` / cancellation
        # always propagates.
        if not status.was_started:
            raise
        logger.warning(
            "Spawned sandbox (%s) failed after startup; containing the failure to this spawn.",
            spawn_desc,
            exc_info=exc,
        )


async def _serve_spawn_rpc(
    rpc_message_stream: anyio.abc.ObjectStream[RPCMessage],
    teardown_event: anyio.Event,
    status: _SpawnRunnerStatus,
) -> None:
    """Shared tail of the ``_spawn_runner_*`` arms: open the host-side RPC
    connection, report ``(remote_service, scope)``, and park on
    ``teardown_event``. On event-set the ``open_rpc_scope`` CM exits and
    drains; the conn drain blocks on whatever bridges the foreground
    installed in the call_scope, so the sandbox stays alive until the host
    caller releases the chain-bridged ref.
    """
    # Isolated: this task inherits the spawn *dispatch*'s context, and a
    # non-isolated rpc scope would silently parent onto that ephemeral
    # per-call scope (v2/notes/scope_discipline.md). The session's
    # lifetime coupling to consumers is carried entirely by the explicit
    # ``raw_guest_ref`` bridge ``spawn`` installs in its call scope —
    # no behavior needs a parent edge here.
    async with open_rpc_scope(
        rpc_message_stream,
        remote_service_type=SandboxGuestImplProto[FxtrServiceData, UnknownService],
        name="host_rpc",
        isolated=True,
    ) as rpc_session:
        status.started((rpc_session.remote_service, rpc_session.scope))
        await teardown_event.wait()


class SandboxSpawnerImpl(TypedServiceImpl[SandboxSpawnerProto]):
    """Mountable spawner. Owns an env-level task group on which ``spawn``
    starts background runner tasks, plus the wheel cache, the env's
    transport-connection server, and any other runtime config the
    launchers need.

    Dispatches on bundle type: the live git-ref defns
    (``NixWasmServiceDefn`` → eryx, ``NixContainerServiceDefn`` →
    container runtime, and the kind-agnostic ``NixServiceDefn``, which
    dispatches as whatever ``kind`` its attr declares) plus the
    deprecated in-band ``TaskBundle`` (WASM).

    Holds the env's :class:`TransportConnectionServer` so the
    low-level launchers can wire the side-channel callbacks /
    accept-loop against it.
    """

    def __init__(
        self,
        *,
        cache_dir: Path,
        env_tg: anyio.abc.TaskGroup,
        transport_server: TransportConnectionServer,
        git_repo_path: Path | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._env_tg = env_tg
        self._transport_server = transport_server
        # The one git repo `NixWasmServiceDefn` spawns resolve against
        # (`?rev=<commit>` is looked up here). Locally the agent's workdir
        # repo; remotely a (possibly bare) repo the agent pushed to. `None`
        # → the nix-wasm arm is unavailable (raises on use).
        self._git_repo_path = git_repo_path
        # The nix backend (host nix, or in-podman builds — FXTR_NIX_BACKEND +
        # ~/.config/fxtr/nix_runtime.toml). Resolved **lazily** on first nix
        # spawn (see `_backend`), so an env that never builds a nix entity needs
        # no backend config. One instance per spawner, so its per-repo builders
        # are refcount-shared across this env's concurrent spawns.
        self._nix_backend: NixBackend | None = None
        # Keep the builder warm across this env's spawns: on the first nix spawn
        # we open one `keep_builder_warm` pin (into `_warm_stack`) so the builder
        # container is started once and reused, instead of being torn down + re-
        # created every spawn. Lazy — an env with no nix spawn pins nothing.
        # `create_sandbox_environment` closes `_warm_stack` at env teardown.
        self._warm_stack = contextlib.AsyncExitStack()
        self._warm_lock = anyio.Lock()
        self._warm_pinned = False
        # Build-pipeline caches keyed by the immutable, content-addressed
        # (git_commit, flake_attr, flake_path): repeat spawns of the same service
        # (a worker fan-out) skip the eval/build/deliver entirely. Valid for the
        # env's life — the built store paths (shared /nix volume) + delivered
        # images (runtime image store) persist across builder recycles.
        self._container_cache: dict[tuple[str, str, str | None], DeliveredImage] = {}
        self._wasm_cache: dict[tuple[str, str, str | None], tuple[str, Path]] = {}
        self._pyodide_cache: dict[tuple[str, str, str | None], tuple[str, Path, Path]] = {}
        # The attr's `kind` discriminator per triple — same validity argument
        # as the build caches above (the triple is immutable, the flake is
        # pinned by commit). Shared by the kind *checks* the concrete arms
        # run and the kind *dispatch* the auto (`NixServiceDefn`) arm does,
        # so an auto spawn evals the kind exactly once per env per triple.
        self._kind_cache: dict[tuple[str, str, str | None], str] = {}

    def _target_type_annotation(self) -> Any:
        return SandboxSpawnerProto

    def _service_type_id(self) -> FxtrID:
        return SANDBOX_SPAWNER_TYPE_ID

    def _backend(self) -> NixBackend:
        """The nix backend, resolved lazily on first use (cached). Resolution
        requires a configured backend, so it only fails for an env that
        actually performs a nix spawn on an unconfigured machine."""
        if self._nix_backend is None:
            self._nix_backend = resolve_backend()
        return self._nix_backend

    async def _ensure_builder_warm(self, repo: Path) -> None:
        """Open the env-lifetime `keep_builder_warm` pin on first nix spawn.

        Idempotent + concurrency-safe (guarded so two concurrent first-spawns
        open exactly one pin). After this, the per-spawn `builder_for(repo)`
        borrows are cheap shared borrows and the builder isn't torn down between
        spawns — it's torn down when `aclose_warm_builder` runs at env teardown.
        """
        async with self._warm_lock:
            if not self._warm_pinned:
                await self._warm_stack.enter_async_context(self._backend().keep_builder_warm(repo))
                self._warm_pinned = True

    async def aclose_warm_builder(self) -> None:
        """Release the warm-builder pin (env teardown). Tears the builder down
        if no borrow is still live. No-op if no nix spawn ever pinned one."""
        await self._warm_stack.aclose()

    async def _build_with_recycle(
        self, repo: Path, build_fn: Callable[[NixHandle], Awaitable[_BuildT]]
    ) -> _BuildT:
        """Run ``build_fn`` against a fresh ``builder_for`` handle, retrying once
        if the warm builder/deliverer died mid-build.

        A dead warm container surfaces as ``BuilderTransportError`` (the podman
        backend has already invalidated it — the builder's registry entry or the
        deliverer id — so re-opening ``builder_for`` rebuilds/recreates what's
        needed). Redoing the whole eval→build→deliver closure recovers from a
        death at any point in it. One retry, then propagate.
        """
        for attempt in range(2):
            try:
                async with (
                    perf_span("spawn.build_and_deliver"),
                    self._backend().builder_for(repo) as h,
                ):
                    return await build_fn(h)
            except BuilderTransportError:
                if attempt == 1:
                    raise
                record("builder.recycled")
        raise AssertionError("unreachable: the loop returns or raises")

    async def _spawn_runner_wasm(
        self,
        bundle: TaskBundle,
        teardown_event: anyio.Event,
        *,
        task_status: anyio.abc.TaskStatus[_SpawnStartedValue],
    ) -> None:
        """Background runner: launch a WASM sandbox + open the host-side RPC conn.

        Deprecated in-band-bytes bundle path — prefer ``NixWasmServiceDefn``
        + :meth:`_spawn_runner_nix_wasm` (git-referenced code the spawner
        builds). Kept working for the still-live ``prepare_package_bundle``.

        Reports ``(sandbox_connect_ref, session.scope)`` and parks on
        ``teardown_event`` (see :func:`_serve_spawn_rpc`); failures after
        ``started`` are contained to this spawn (see
        :func:`_contain_failures_after_started`).
        """
        async with _contain_failures_after_started(
            f"wasm bundle {bundle.entry_point}", task_status
        ) as status:
            async with _run_wasm(
                bundle, self._cache_dir, transport_server=self._transport_server
            ) as session:
                json_stream = JSONObjectStream(session.send, session.receive)
                await _serve_spawn_rpc(wrap_json_stream(json_stream), teardown_event, status)

    def _require_git_repo(self, defn_kind: str) -> Path:
        """The configured repo git-ref defns resolve against, or raise."""
        if self._git_repo_path is None:
            raise RuntimeError(
                f"spawn({defn_kind}): this spawner was constructed without "
                "a git_repo_path, so git-referenced services can't be resolved. "
                "Pass git_repo_path=... to create_sandbox_environment."
            )
        return self._git_repo_path

    async def _eval_kind_cached(
        self, h: NixHandle, git_commit: str, flake_attr: str, flake_path: str | None
    ) -> str | None:
        """The attr's ``kind`` discriminator, via the per-env kind cache.

        A declared kind is cached per triple (immutable inputs — see the
        cache comment in ``__init__``); a missing kind (``None``) is not
        cached, since every caller turns it into an error anyway.
        """
        key = (git_commit, flake_attr, flake_path)
        kind = self._kind_cache.get(key)
        if kind is not None:
            record("spawn.kind_cache_hit")
            return kind
        kind = await nix.eval_service_kind(h, git_commit, flake_attr, flake_path)
        if kind is not None:
            self._kind_cache[key] = kind
        return kind

    @staticmethod
    def _missing_kind_error(defn: "_AnyNixDefn") -> RuntimeError:
        """The refuse-to-guess error for an attr declaring no ``kind`` (a
        flake built against a fxtr lib predating the discriminator, or a
        hand-built attr missing it): the kinds dispatch to structurally
        different arms, so we never guess."""
        return RuntimeError(
            f"spawn: service attr {defn.flake_attr!r} at commit "
            f"{defn.git_commit} declares no `kind`. Rebuild the flake "
            "against a current fxtr lib (mkWasmService/mkContainerService/"
            "mkPyodideService "
            'emit it), or add `kind = "wasm"`/`kind = "container"` to a '
            "hand-built attr."
        )

    async def _check_service_kind(
        self,
        h: NixHandle,
        defn: NixWasmServiceDefn | NixContainerServiceDefn | NixPyodideServiceDefn,
        expected: str,
    ) -> None:
        """Eval the service attr's ``kind`` discriminator (cached); raise on
        mismatch or on a missing ``kind`` (see :meth:`_missing_kind_error`).
        """
        kind = await self._eval_kind_cached(h, defn.git_commit, defn.flake_attr, defn.flake_path)
        if kind is None:
            raise self._missing_kind_error(defn)
        if kind != expected:
            other_defn = {
                "container": "NixContainerServiceDefn",
                "pyodide": "NixPyodideServiceDefn",
            }.get(kind, "NixWasmServiceDefn")
            raise RuntimeError(
                f"spawn: service attr {defn.flake_attr!r} at commit "
                f"{defn.git_commit} has kind {kind!r}, but this spawn "
                f"reference expects {expected!r}. Spawn it with "
                f"{other_defn} instead."
            )

    async def _spawn_runner_nix_wasm(
        self,
        defn: NixWasmServiceDefn,
        teardown_event: anyio.Event,
        *,
        task_status: anyio.abc.TaskStatus[_SpawnStartedValue],
    ) -> None:
        """Background runner: build a git-referenced service + launch it WASM.

        Same shape as :meth:`_spawn_runner_wasm`, but the site-packages come
        from a nix git-flake-ref build instead of a materialized
        :class:`TaskBundle`: :func:`nix.eval_entry_point` +
        :func:`nix.build_site_packages` resolve ``defn`` against the spawner's
        configured repo, then the realized store path is mounted read-only
        into a bare eryx factory (via ``volumes`` + ``sys_path_additions`` —
        the store path is immutable, so we never copy it). Build/eval failures
        raise out of here and ride back as the ``spawn`` invoke's error;
        failures after ``started`` are contained to this spawn (see
        :func:`_contain_failures_after_started`).
        """
        async with _contain_failures_after_started(
            f"nix-wasm {defn.flake_attr} @ {defn.git_commit}", task_status
        ) as status:
            repo = self._require_git_repo("NixWasmServiceDefn")
            cache_key = (defn.git_commit, defn.flake_attr, defn.flake_path)
            cached = self._wasm_cache.get(cache_key)
            if cached is None:
                await self._ensure_builder_warm(repo)

                async def _eval_and_build(h: NixHandle) -> tuple[str, Path]:
                    # One builder for the whole eval+build sequence; on the podman
                    # backend the container persists across the env's spawns via
                    # the warm-builder pin, and the materialized site-packages are
                    # host-readable afterward, so the builder isn't needed once built.
                    await self._check_service_kind(h, defn, "wasm")
                    entry_point = await nix.eval_entry_point(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    site_packages = await nix.build_site_packages(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    return entry_point, site_packages

                # Retries once if the warm builder died mid-build.
                cached = await self._build_with_recycle(repo, _eval_and_build)
                self._wasm_cache[cache_key] = cached
            else:
                # Repeat spawn of this service — skip eval/build entirely.
                record("spawn.cache_hit")
            entry_point, site_packages = cached

            # Mount the (immutable) store path read-only and add it to the
            # guest's sys.path — no per-spawn copy needed.
            guest_site_packages = "/nix-site-packages"
            _check_and_maybe_repair_eryx_embedded_dir()
            factory = eryx.SandboxFactory()
            async with run_in_factory(
                factory,
                entry_point=entry_point,
                volumes=[(str(site_packages), guest_site_packages, True)],
                sys_path_additions=[guest_site_packages],
                transport_server=self._transport_server,
            ) as session:
                json_stream = JSONObjectStream(session.send, session.receive)
                await _serve_spawn_rpc(wrap_json_stream(json_stream), teardown_event, status)

    async def _spawn_runner_nix_container(
        self,
        defn: NixContainerServiceDefn,
        teardown_event: anyio.Event,
        *,
        task_status: anyio.abc.TaskStatus[_SpawnStartedValue],
    ) -> None:
        """Background runner: build a git-referenced container image + run it.

        The container sibling of :meth:`_spawn_runner_nix_wasm`:
        :func:`nix.build_image` resolves ``defn`` against the spawner's
        configured repo into a nix2container JSON manifest, then
        ``backend.deliver_image`` loads it into the container runtime and
        returns the ref + ``--platform`` to run it with;
        :func:`_run_nix_container_internal` launches it (entry point baked
        into the image's ``Cmd`` — nothing to eval). Uses the UDS-backed
        :class:`SocketJSONStream` like the other container arms.
        Build/delivery failures raise out of here and ride back as the
        ``spawn`` invoke's error; failures after ``started`` are contained
        to this spawn (see :func:`_contain_failures_after_started`).
        """
        async with _contain_failures_after_started(
            f"nix-container {defn.flake_attr} @ {defn.git_commit}", task_status
        ) as status:
            repo = self._require_git_repo("NixContainerServiceDefn")
            cache_key = (defn.git_commit, defn.flake_attr, defn.flake_path)
            delivered = self._container_cache.get(cache_key)
            if delivered is None:
                await self._ensure_builder_warm(repo)

                async def _build_and_deliver(h: NixHandle) -> DeliveredImage:
                    await self._check_service_kind(h, defn, "container")
                    image_json = await nix.build_image(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    # Delivery (build skopeo + load the image into the runtime) is
                    # the backend's job; it returns what we need to run. Done
                    # inside the builder context so the podman deliverer is available.
                    return await h.deliver_image(str(image_json), runtime=get_container_runtime())

                # Retries once if the warm builder/deliverer died mid-build.
                delivered = await self._build_with_recycle(repo, _build_and_deliver)
                self._container_cache[cache_key] = delivered
            else:
                # Repeat spawn of this service — skip eval/build/deliver entirely.
                record("spawn.cache_hit")

            async with _run_nix_container_internal(
                delivered.image_ref,
                delivered.platform,
                transport_server=self._transport_server,
            ) as socket_stream:
                json_stream = SocketJSONStream(socket_stream)
                await _serve_spawn_rpc(wrap_json_stream(json_stream), teardown_event, status)

    async def _spawn_runner_nix_pyodide(
        self,
        defn: NixPyodideServiceDefn,
        teardown_event: anyio.Event,
        *,
        task_status: anyio.abc.TaskStatus[_SpawnStartedValue],
    ) -> None:
        """Background runner: build a git-referenced pyodide service + run it
        inside a permission-locked deno subprocess.

        The pyodide sibling of :meth:`_spawn_runner_nix_wasm`:
        :func:`nix.eval_entry_point` + :func:`nix.build_site_packages` +
        :func:`nix.build_runtime_bundle` resolve ``defn`` against the
        spawner's configured repo (both build outputs materialized so the
        host deno process can read them), then
        :func:`_run_pyodide_internal` launches the host-trusted driver and
        yields the accepted task-socket stream. Uses the UDS-backed
        :class:`SocketJSONStream` like the container arm. Build/eval
        failures raise out of here and ride back as the ``spawn`` invoke's
        error; failures after ``started`` are contained to this spawn (see
        :func:`_contain_failures_after_started`).
        """
        async with _contain_failures_after_started(
            f"nix-pyodide {defn.flake_attr} @ {defn.git_commit}", task_status
        ) as status:
            repo = self._require_git_repo("NixPyodideServiceDefn")
            cache_key = (defn.git_commit, defn.flake_attr, defn.flake_path)
            cached = self._pyodide_cache.get(cache_key)
            if cached is None:
                await self._ensure_builder_warm(repo)

                async def _eval_and_build(h: NixHandle) -> tuple[str, Path, Path]:
                    await self._check_service_kind(h, defn, "pyodide")
                    entry_point = await nix.eval_entry_point(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    site_packages = await nix.build_site_packages(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    runtime_bundle = await nix.build_runtime_bundle(
                        h, defn.git_commit, defn.flake_attr, defn.flake_path
                    )
                    return entry_point, site_packages, runtime_bundle

                # Retries once if the warm builder died mid-build.
                cached = await self._build_with_recycle(repo, _eval_and_build)
                self._pyodide_cache[cache_key] = cached
            else:
                # Repeat spawn of this service — skip eval/build entirely.
                record("spawn.cache_hit")
            entry_point, site_packages, runtime_bundle = cached

            async with _run_pyodide_internal(
                entry_point=entry_point,
                site_packages=site_packages,
                runtime_bundle=runtime_bundle,
                transport_server=self._transport_server,
            ) as socket_stream:
                json_stream = SocketJSONStream(socket_stream)
                await _serve_spawn_rpc(wrap_json_stream(json_stream), teardown_event, status)

    async def _resolve_auto_defn(self, defn: NixServiceDefn) -> _ConcreteNixDefn:
        """Resolve a kind-agnostic defn to the concrete kind its attr declares.

        Evals the attr's ``kind`` through the per-env kind cache (so the
        arm's own :meth:`_check_service_kind` after dispatch re-uses the
        result instead of re-evaling). Missing kind → the refuse-to-guess
        error; unknown kind → an error naming the known kinds.
        """
        repo = self._require_git_repo("NixServiceDefn")
        key = (defn.git_commit, defn.flake_attr, defn.flake_path)
        kind = self._kind_cache.get(key)
        if kind is None:
            await self._ensure_builder_warm(repo)

            async def _eval(h: NixHandle) -> str | None:
                return await self._eval_kind_cached(
                    h, defn.git_commit, defn.flake_attr, defn.flake_path
                )

            kind = await self._build_with_recycle(repo, _eval)
            if kind is None:
                raise self._missing_kind_error(defn)
        return _concrete_defn_for_kind(defn, kind)

    @service_method
    async def spawn(
        self,
        bundle: EntityRef[
            TaskBundle
            | NixWasmServiceDefn
            | NixContainerServiceDefn
            | NixPyodideServiceDefn
            | NixServiceDefn
        ],
    ) -> ServiceRef[SandboxConnectProto[OverrideThisTypeBeforeUsing, OverrideThisTypeBeforeUsing]]:
        loaded = await bundle.load()

        if isinstance(loaded, NixServiceDefn):
            # The kind-agnostic auto form: dispatch as whatever kind the
            # attr declares. `bundle` (and hence spawn provenance) stays
            # the caller's NixServiceDefn ref; only the runner arm sees
            # the concrete defn.
            loaded = await self._resolve_auto_defn(loaded)

        if isinstance(loaded, TaskBundle):
            runner = self._spawn_runner_wasm
        elif isinstance(loaded, NixWasmServiceDefn):
            runner = self._spawn_runner_nix_wasm
        elif isinstance(loaded, NixContainerServiceDefn):
            runner = self._spawn_runner_nix_container
        elif isinstance(loaded, NixPyodideServiceDefn):
            runner = self._spawn_runner_nix_pyodide
        else:
            raise TypeError(
                "spawn: bundle resolves to unexpected entity type "
                f"{type(loaded).__name__}; expected NixWasmServiceDefn, "
                "NixContainerServiceDefn, NixPyodideServiceDefn, "
                "NixServiceDefn, or TaskBundle."
            )

        teardown_event = anyio.Event()
        # ``raw_guest_ref`` speaks SandboxGuestImplProto, not the
        # user-facing SandboxConnectProto — its wire method takes
        # the bundle ref as a second arg. The wrapper below adapts
        # this surface to the user-facing SandboxConnectProto.connect.
        raw_guest_ref, session_scope = await self._env_tg.start(runner, loaded, teardown_event)
        call_scope = get_active_scope()
        await raw_guest_ref.bridge_from_scope(
            session_scope, into_scope=call_scope, keepalive_via="independent_lease"
        )
        teardown_event.set()

        # Mount the wrapper on call_scope. The wrapper captures
        # raw_guest_ref + bundle (the EntityRef the user passed) and
        # forwards each connect() to the underlying guest with bundle
        # threaded in. The wrapper's lifetime tracks call_scope; when
        # the user releases the returned ref, call_scope drains, the
        # wrapper unmounts, raw_guest_ref's chain-bridge releases, the
        # RPC connection drains, the guest tears down.
        bundle_unk_ref: EntityRef[UnknownEntity] = EntityRef(
            entity_id=bundle.entity_id, target_type=UnknownEntity
        )
        wrapper = _SpawnConnectWrapperImpl(raw_guest_ref=raw_guest_ref, bundle=bundle_unk_ref)
        wrapper_ref = await call_scope.mount(wrapper)
        return wrapper_ref.replace_service_type(
            SandboxConnectProto[OverrideThisTypeBeforeUsing, OverrideThisTypeBeforeUsing]
        )


class _SpawnConnectWrapperImpl(
    TypedServiceImpl[SandboxConnectProto[FxtrServiceData, UnknownService]]
):
    """Host-side adapter from :class:`SandboxConnectProto` (user-facing)
    to :class:`SandboxGuestImplProto` (wire-internal, takes the bundle
    as a second arg).

    Mounted on the spawn-dispatch call_scope. Holds a ServiceRef to
    the raw guest impl + the EntityRef to the bundle. Each user
    ``connect(config)`` resolves the raw guest, calls
    ``connect_with_bundle_provenance(config, bundle)`` on it, and
    returns the resulting ServiceRef back through the chain.

    This is the only place the bundle is supplied — user code can't
    forge it because the bundle isn't part of the ``connect`` wire
    payload at this surface.
    """

    def __init__(
        self,
        *,
        raw_guest_ref: ServiceRef[SandboxGuestImplProto[FxtrServiceData, UnknownService]],
        bundle: EntityRef[UnknownEntity],
    ) -> None:
        self._raw_guest_ref = raw_guest_ref
        self._bundle = bundle

    def _target_type_annotation(self) -> Any:
        return SandboxConnectProto[FxtrServiceData, UnknownService]

    def _service_type_id(self) -> FxtrID:
        return SANDBOX_CONNECT_TYPE_ID

    @service_method
    async def connect(self, config: FxtrServiceData) -> ServiceRef[Any]:
        proxy = await self._raw_guest_ref.resolve()
        return await proxy.connect_with_bundle_provenance(config, self._bundle)


class SandboxEnvironment:
    """Result of :func:`create_sandbox_environment`.

    Carries a ``spawner_ref`` for callers to launch sandboxes, plus
    ``add_transport`` / ``add_raw_transport`` (stage 3) for registering
    network-handle authorizers with the env's
    :class:`TransportConnectionServer`. Each authorizer impl is mounted
    on the env's host scope and exposed as a
    ``ServiceRef[NetworkHandleAuthorizerProto]`` that callers can bridge
    into sandboxes.

    Construction is private — use :func:`create_sandbox_environment`.
    """

    def __init__(
        self,
        *,
        spawner_ref: ServiceRef[SandboxSpawnerProto],
        host_scope: FxtrScope,
        env_tg: anyio.abc.TaskGroup,
        transport_server: TransportConnectionServer,
        exit_stack: contextlib.AsyncExitStack,
    ) -> None:
        self.spawner_ref = spawner_ref
        self.host_scope = host_scope
        """The scope ``spawner_ref`` is mounted on. Public so callers
        can pass ``(spawner_ref, host_scope)`` as a pair to APIs that
        need to bridge the ref out — e.g. the workstation server's
        ``external_services`` config."""
        self._env_tg = env_tg
        self._transport_server = transport_server
        self._exit_stack = exit_stack

    async def add_transport(
        self, route: TransportRoute
    ) -> ServiceRef[NetworkHandleAuthorizerProto]:
        """Register an HTTP transport route; return its authorizer ref.

        Spins up an :class:`aiohttp.ClientSession` for the upstream
        connection pool and registers its ``aclose`` on the env's exit
        stack so it's closed deterministically at env teardown. The
        returned ref can be bridged into sandboxes; sandboxes that hold
        it can call ``get_transport_token()`` to mint single-use tokens
        for one connection's worth of access through this route.

        ``auto_decompress=False`` is load-bearing: we forward the
        upstream's ``Content-Length`` and body bytes verbatim, so if
        aiohttp silently gunzipped the body the byte count would no
        longer match the header and h11 would raise
        ``LocalProtocolError("Too much data for declared Content-
        Length")`` mid-response. The sandboxed client's HTTP library
        handles ``Content-Encoding`` itself.
        """
        session = aiohttp.ClientSession(auto_decompress=False)
        self._exit_stack.push_async_callback(session.close)
        handler = make_http_route_handler(route, session, self._env_tg)
        return await self._mount_authorizer(handler)

    async def add_passthrough_transport(
        self,
        *,
        allowed_hosts: list[str] | None = None,
        blocked_hosts: list[str] | None = None,
    ) -> ServiceRef[NetworkHandleAuthorizerProto]:
        """Register a host-agnostic HTTPS passthrough authorizer.

        Unlike :meth:`add_transport` (which pins one upstream per
        authorizer), this returns one authorizer whose proxy picks
        the upstream from each request's ``Host:`` header. All
        upstream calls go out as HTTPS.

        Filter the set of reachable hosts via at most one of:

        - ``allowed_hosts``: if set, only requests whose ``Host:``
          hostname (port stripped, case-folded) matches one of
          these are forwarded. Everything else gets a 403.
        - ``blocked_hosts``: the opposite — listed hosts are
          refused.

        Both ``None`` (default) means *no filtering*: any host the
        agent asks for is forwarded. Useful for "let the agent
        browse the internet" demos; risky if the host machine is
        on a network where the LAN matters (the agent can probe
        internal addresses). Set ``blocked_hosts`` to a list of
        private-IP / loopback aliases if that's a concern.

        Spins up one :class:`aiohttp.ClientSession` for connection
        pooling; its ``close`` is registered on the env's exit
        stack so it tears down deterministically with the env.
        ``auto_decompress=False`` for the same reason as
        :meth:`add_transport`: we want to forward bytes + ``Content-
        Length`` verbatim and let the sandboxed client decode any
        ``Content-Encoding`` itself.
        """
        config = PassthroughHostsConfig(
            allowed_hosts=tuple(allowed_hosts) if allowed_hosts is not None else None,
            blocked_hosts=tuple(blocked_hosts) if blocked_hosts is not None else None,
        )
        session = aiohttp.ClientSession(auto_decompress=False)
        self._exit_stack.push_async_callback(session.close)
        handler = make_passthrough_http_handler(config, session, self._env_tg)
        return await self._mount_authorizer(handler)

    async def add_raw_transport(
        self, handler: TransportHandler
    ) -> ServiceRef[NetworkHandleAuthorizerProto]:
        """Register a user-supplied raw byte-stream handler; return its
        authorizer ref.

        ``handler`` is a sync callable that takes ownership of an
        ``anyio.abc.ByteStream`` when a sandbox redeems a token. It must
        close the stream itself; if it needs async work, it should
        ``start_soon`` on a task group of its own choosing — the env
        does not provide one for it. Useful for non-HTTP transports
        (raw byte protocols, future sandbox-to-sandbox links) and for
        testing the token-table machinery without an HTTP upstream.
        """
        return await self._mount_authorizer(handler)

    async def _mount_authorizer(
        self, handler: TransportHandler
    ) -> ServiceRef[NetworkHandleAuthorizerProto]:
        impl = NetworkHandleAuthorizerImpl(handler, self._transport_server)
        return await self.host_scope.mount(impl)


@contextlib.asynccontextmanager
async def create_sandbox_environment(
    *,
    cache_dir: Path,
    git_repo_path: Path | None = None,
) -> AsyncIterator[SandboxEnvironment]:
    """Open a sandbox environment.

    Outer task group sits *outside* the host scope so guest runner tasks
    outlive the spawner mount and finish cleanly during teardown. The host
    scope is **isolated** (parents: ``GLOBAL_SCOPE`` only) and is *not*
    made active for the CM body: the env is long-lived library state, and
    inheriting the caller's ambient scope as a structural parent would
    let an ephemeral caller scope become unable to end while the env
    lives (v2/notes/scope_discipline.md). Consumers reach it explicitly —
    resolve ``env.spawner_ref`` against ``env.host_scope``, or open a
    bridging scope that includes both your own scope and
    ``env.host_scope`` (e.g. ``async with create_sandbox_environment(...)
    as env, FxtrScope(include=[env.host_scope]):``) so pre-stored
    defn/config entities stay reachable from proxies resolved there.

    Args:
        cache_dir: Wheel cache for WASM bundles. Container spawns ignore
            this (their image cache is the container runtime's image
            store).
        git_repo_path: The one git repo that ``NixWasmServiceDefn`` /
            ``NixContainerServiceDefn`` spawns resolve ``?rev=<commit>``
            against (locally the agent's workdir repo). ``None`` (default)
            leaves the git-ref arms unavailable — spawning a defn then
            raises. Ignored by ``TaskBundle`` spawns.

    On CM exit:
    1. host_scope drains (waits for any chain-bridges to call_scopes to
       release, which propagates teardown into all in-flight guests),
    2. env-level exit stack runs (closes any
       :class:`aiohttp.ClientSession` registered by ``add_transport``),
    3. env_tg exits (waits for the spawn-runner tasks + any in-flight
       transport handler tasks to return).
    """
    async with anyio.create_task_group() as env_tg:
        async with contextlib.AsyncExitStack() as exit_stack:
            transport_server = TransportConnectionServer()
            # Isolated: the env is long-lived library state, and a
            # non-isolated scope here would adopt whatever scope is
            # ambient at the caller as a structural parent — if that is
            # an ephemeral scope (e.g. a service dispatch's per-call
            # scope), it can then never end while the env lives, which
            # deadlocks teardown (v2/notes/scope_discipline.md).
            # GLOBAL_SCOPE covers the system type entities the mounts
            # need; consumers reach the scope explicitly via
            # ``env.host_scope``.
            async with FxtrScope(
                name="sandbox_env", isolated=True, include=[GLOBAL_SCOPE]
            ) as host_scope:
                impl = SandboxSpawnerImpl(
                    cache_dir=cache_dir,
                    env_tg=env_tg,
                    transport_server=transport_server,
                    git_repo_path=git_repo_path,
                )
                # Release the warm-builder pin at env teardown (step 2 below —
                # after host_scope drains guests, before env_tg exits). Safe
                # there: each spawn's build+deliver completes before its spawn()
                # returns, so no build is in flight at teardown. No-op if no nix
                # spawn ever pinned a builder.
                exit_stack.push_async_callback(impl.aclose_warm_builder)
                spawner_ref = await host_scope.mount(impl)
                yield SandboxEnvironment(
                    spawner_ref=spawner_ref,
                    host_scope=host_scope,
                    env_tg=env_tg,
                    transport_server=transport_server,
                    exit_stack=exit_stack,
                )
