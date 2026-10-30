"""The podman backend: nix runs inside a per-repo builder container.

Each git-repo-root gets one builder container per Python process (shared
in-process via the refcounted registry, torn down last-out). All builders
share two named volumes: ``fxtr-nix`` (the ``/nix`` store, copy-up from the
image) and ``fxtr-nix-cache`` (the flake eval cache, mounted as
``XDG_CACHE_HOME`` so it's shared while ``/nix`` stays pristine). The repo
is mounted read-only at ``/workdir``; the materialize scratch dir is bound
in RW so ``nix copy`` can deposit host-readable outputs.

The builder runs at the moderate rung (`SYS_ADMIN + label=disable +
unmask=/proc/*`) with `sandbox = true`, `sandbox-fallback = false`,
arch-resolved `filter-syscalls`, and (default) multi-user nix-daemon +
``nixbld`` build users. The security rationale for the rung lives in the
plan's Security posture:
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage9_nix_runner.md``.
"""

from __future__ import annotations

import hashlib
import socket
import threading
import uuid
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Sequence

import anyio
from fxtr_nix_runner import _delivery
from fxtr_nix_runner._backend import DeliveredImage, ExposedEndpoint, Materialized, NixResult
from fxtr_nix_runner._config import ContainerRuntime, PodmanConfig
from fxtr_nix_runner._lifecycle import RefcountedResourceRegistry
from fxtr_nix_runner.perf import perf_span, proc_label, record

PODMAN = "podman"

# Shared named volumes (constants — every builder, every process shares them).
STORE_VOLUME = "fxtr-nix"
CACHE_VOLUME = "fxtr-nix-cache"

# In-container mount points.
WORKDIR_MOUNT = "/workdir"
CACHE_MOUNT = "/fxtr-cache"
MATERIALIZE_MOUNT = "/materialize"
DELIVERER_SOCKET_MOUNT = "/run/podman.sock"

# Label keys for the clear-CLI sweep (value = repo hash, informational).
BUILDER_LABEL = "fxtr-nix-builder"
DELIVERER_LABEL = "fxtr-nix-deliverer"

DAEMON_SOCKET = "/nix/var/nix/daemon-socket/socket"

# The gate-determined moderate rung (see Security posture). label=disable
# clears SELinux; unmask=/proc/* unlocks namespace creation; SYS_ADMIN does
# sethostname + in-sandbox mounts. Tighter than --privileged.
_SANDBOX_RUN_ARGS = [
    "--cap-add",
    "SYS_ADMIN",
    "--security-opt",
    "label=disable",
    "--security-opt",
    "unmask=/proc/*",
]

_NIXBLD_GID = 30000
_N_BUILD_USERS = 8


class LivenessBeacon:
    """A host TCP liveness socket that container health checks dial.

    Holds a listening socket on ``0.0.0.0:<ephemeral>`` whose lifetime is this
    *process's*: a daemon thread accepts + immediately closes connections while
    alive, and the OS closes the socket when the process dies (incl. SIGKILL).
    So a container that health-checks ``host.containers.internal:<port>``
    succeeds iff this process is alive — pair it with
    ``--health-on-failure=stop`` + ``--rm`` and an orphaned container tears
    itself down ~one health window after the owner vanishes. Reachability of
    ``host.containers.internal`` is the same assumption the podman backend
    already makes for build-time FOD fetches (see ``expose_host_endpoint``).
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", 0))
        self._sock.listen(64)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(
            target=self._serve, name="fxtr-liveness-beacon", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed → stop the loop
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        """Close the beacon socket (the accept loop then exits). For tests /
        explicit teardown; the socket also dies with the process."""
        try:
            self._sock.close()
        except OSError:
            pass


# Health-check cadence for the self-teardown feature. Detection latency is
# ~interval × retries after the beacon dies; the start period tolerates a slow
# container/tunnel bring-up without a false unhealthy.
_HEALTH_INTERVAL = "5s"
_HEALTH_TIMEOUT = "2s"
_HEALTH_RETRIES = "2"


def _beacon_health_args(port: int, *, start_period: str = "10s") -> list[str]:
    """`podman run` args making a networked container self-stop when the host
    beacon at ``port`` is gone.

    The check dials ``host.containers.internal:<port>`` via bash's ``/dev/tcp``
    (the ``nixos/nix`` builder image's ``/bin/sh`` *is* bash, and ``bash`` is on
    PATH). Requires the container to also run with ``--rm`` + ``--stop-timeout
    0`` so the stop is a prompt removal (``sleep infinity`` ignores SIGTERM)."""
    return [
        "--health-cmd",
        f'bash -c "echo > /dev/tcp/host.containers.internal/{port}"',
        "--health-interval",
        _HEALTH_INTERVAL,
        "--health-timeout",
        _HEALTH_TIMEOUT,
        "--health-retries",
        _HEALTH_RETRIES,
        "--health-start-period",
        start_period,
        "--health-on-failure",
        "stop",
    ]


class PodmanError(RuntimeError):
    """A podman CLI invocation (other than ``nix`` itself) failed."""


# `podman exec` returns this when it can't *start* the exec session — the target
# container is gone / not running — as opposed to the exec running and its
# command exiting non-zero. nix never uses 125, so it's a reliable
# transport-failure signal for "the warm container died" without parsing stderr.
_EXEC_TRANSPORT_FAILURE_RC = 125


class BuilderTransportError(PodmanError):
    """A ``podman exec`` against a warm builder/deliverer container failed
    because the container is gone (rc 125), not because the command it ran
    failed. Distinct from a nix build error: it signals the caller to recycle
    the dead container and retry. The failing exec has already invalidated the
    right thing (the builder's registry entry, or the deliverer id) before this
    is raised."""

    def __init__(self, repo_root: Path, stderr: str) -> None:
        super().__init__(
            f"podman exec transport to the builder for {repo_root} failed "
            f"(container gone):\n{stderr}"
        )
        self.repo_root = repo_root


async def _run(argv: Sequence[str]) -> tuple[int, str, str]:
    async with perf_span(proc_label(argv)):
        result = await anyio.run_process(list(argv), check=False)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


async def _run_checked(argv: Sequence[str], *, what: str) -> str:
    rc, out, err = await _run(argv)
    if rc != 0:
        raise PodmanError(f"{what} failed (rc={rc}):\n{out}\n{err}")
    return out


def git_repo_root(path: Path) -> Path:
    """The git repo root at/above ``path`` (matching nix's bare-path
    auto-git walk-up), or ``path`` itself for the no-repo ``path:`` case."""
    resolved = Path(path).resolve()
    for candidate in [resolved, *resolved.parents]:
        if (candidate / ".git").exists():
            return candidate
    return resolved


@dataclass
class _Builder:
    """A live builder container for one repo root.

    The deliverer is a lazily-created sibling that **shares this builder's
    lifetime**: the first ``deliver_image`` starts it and stashes its id here;
    teardown removes both. ``deliverer_lock`` makes that lazy start once-only
    under concurrent deliveries.
    """

    container_id: str
    repo_root: Path
    multi_user: bool
    deliverer_id: str | None = None
    deliverer_lock: anyio.Lock = field(default_factory=anyio.Lock)


class PodmanBackend:
    """A :class:`~fxtr_nix_runner._backend.Backend` running nix in podman."""

    def __init__(self, config: PodmanConfig):
        self._config = config
        self._registry: RefcountedResourceRegistry[Path, _Builder] = RefcountedResourceRegistry(
            self._start_builder, self._stop_builder
        )
        self._filter_syscalls: bool | None = None
        # Host liveness beacon for builder/deliverer self-teardown (started
        # lazily on first builder start; lives for the process's lifetime).
        self._beacon: LivenessBeacon | None = None
        self._beacon_lock = anyio.Lock()

    async def _ensure_beacon(self) -> int:
        """The host liveness beacon's port, creating the beacon on first use."""
        async with self._beacon_lock:
            if self._beacon is None:
                self._beacon = LivenessBeacon()
            return self._beacon.port

    def builder_for(self, repo: Path) -> AbstractAsyncContextManager[PodmanHandle]:
        repo_root = git_repo_root(repo)

        @asynccontextmanager
        async def cm() -> AsyncIterator[PodmanHandle]:
            async with self._registry.borrow(repo_root) as builder:
                yield PodmanHandle(
                    builder,
                    self._config,
                    target=Path(repo).resolve(),
                    backend=self,
                    repo_root=repo_root,
                )

        return cm()

    def keep_builder_warm(self, repo: Path) -> AbstractAsyncContextManager[None]:
        # Hold a registry pin on the repo's builder: the first builder_for below
        # starts it, and the pin keeps it alive between borrows until this block
        # exits (see RefcountedResourceRegistry.pin). Same key as builder_for.
        return self._registry.pin(git_repo_root(repo))

    async def _invalidate_builder(self, repo_root: Path, builder: _Builder) -> None:
        """Drop a warm builder believed dead so the next borrow rebuilds it.

        Called by a handle when a `podman exec` against the builder returns the
        transport-failure code (the container is gone). Delegates to the
        registry's identity-checked `invalidate`, so a pin held on the key
        survives and the rebuild re-warms it."""
        await self._registry.invalidate(repo_root, builder)

    # ------------------------------------------------------------
    # Lifecycle (registry factory / teardown)
    # ------------------------------------------------------------

    async def _resolve_filter_syscalls(self) -> bool:
        """``filter-syscalls`` for the builder. "auto" => on only for a
        native-amd64 machine (it can't load under amd64/Rosetta emulation)."""
        if self._config.filter_syscalls != "auto":
            return bool(self._config.filter_syscalls)
        if self._filter_syscalls is None:
            out = await _run_checked(
                [PODMAN, "info", "--format", "{{.Host.Arch}}"], what="podman info"
            )
            # Builder is always linux/amd64: native iff the machine is amd64.
            self._filter_syscalls = out.strip() == "amd64"
        return self._filter_syscalls

    async def _ensure_volumes(self) -> None:
        for vol in (STORE_VOLUME, CACHE_VOLUME):
            rc, _, _ = await _run([PODMAN, "volume", "exists", vol])
            if rc != 0:
                await _run_checked([PODMAN, "volume", "create", vol], what=f"volume create {vol}")

    def _nix_conf(self, filter_syscalls: bool) -> str:
        lines = [
            # `configurable-impure-env` is required *daemon-side* for `--option
            # impure-env` (the per-build FOD env channel) to take effect — a
            # client flag does not propagate. The builder daemon is ours, so we
            # set it here.
            "experimental-features = nix-command flakes configurable-impure-env",
            f"sandbox = {'true' if self._config.sandbox else 'false'}",
            # Never silently fall back to an unsandboxed build (footgun).
            "sandbox-fallback = false",
            f"filter-syscalls = {'true' if filter_syscalls else 'false'}",
            "max-jobs = auto",
        ]
        if self._config.multi_user:
            lines.append("build-users-group = nixbld")
            lines.append("trusted-users = root")
        return "\n".join(lines) + "\n"

    def _bootstrap_script(self, filter_syscalls: bool) -> str:
        """Shell run inside the fresh container: write nix.conf, and (multi-
        user) create the ``nixbld`` build users. No ``shadow`` tools in the
        image, so we append to /etc/passwd + /etc/group directly."""
        conf = self._nix_conf(filter_syscalls)
        script = f"set -e\nmkdir -p /etc/nix /var/empty\ncat > /etc/nix/nix.conf <<'NIXCONF'\n{conf}NIXCONF\n"
        if self._config.multi_user:
            members = ",".join(f"nixbld{i}" for i in range(1, _N_BUILD_USERS + 1))
            script += (
                f"grep -q '^nixbld:' /etc/group || "
                f"echo 'nixbld:x:{_NIXBLD_GID}:{members}' >> /etc/group\n"
                f"i=1\nwhile [ $i -le {_N_BUILD_USERS} ]; do\n"
                f'  grep -q "^nixbld$i:" /etc/passwd || '
                f'echo "nixbld$i:x:$(({_NIXBLD_GID}+i)):{_NIXBLD_GID}:Nix build user $i:'
                f'/var/empty:/sbin/nologin" >> /etc/passwd\n'
                f"  i=$((i+1))\ndone\n"
            )
        script += "echo BOOTSTRAP_OK\n"
        return script

    def _mountable_path_args(self) -> list[str]:
        """`-v <dir>:<dir>:ro` for each configured ``mountable_paths`` dir that
        exists — so a flake input under it resolves at its original abspath
        in-container. Missing dirs are skipped (a stale config entry shouldn't
        block all builds)."""
        args: list[str] = []
        for path in self._config.mountable_paths:
            resolved = Path(path).expanduser()
            if resolved.exists():
                args += ["-v", f"{resolved}:{resolved}:ro"]
        return args

    async def _start_builder(self, repo_root: Path) -> _Builder:
        async with perf_span("builder.start", repo=repo_root.name):
            return await self._start_builder_inner(repo_root)

    async def _start_builder_inner(self, repo_root: Path) -> _Builder:
        await self._ensure_volumes()
        filter_syscalls = await self._resolve_filter_syscalls()
        materialize_root = self._config.materialize_root
        materialize_root.mkdir(parents=True, exist_ok=True)

        beacon_port = await self._ensure_beacon()
        repo_hash = _hash12(repo_root)
        name = f"{BUILDER_LABEL}-{repo_hash}-{uuid.uuid4().hex[:8]}"
        run_args = [
            PODMAN,
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"{BUILDER_LABEL}={repo_hash}",
            "--platform",
            "linux/amd64",
            # PID 1 is `sleep infinity`, which ignores SIGTERM, so a graceful
            # stop would always burn the 10s stop timeout before SIGKILL. This
            # box holds no unflushed state (the store is on the shared volume;
            # builds are transactional and never in flight at teardown), so
            # declare it kill-on-stop — every removal path (`_stop_builder`,
            # bootstrap-failure cleanup, `fxtr_nix clear`) gets it for free.
            "--stop-timeout",
            "0",
            # Self-teardown if the owning host process dies: the health check
            # dials the host beacon; when it's gone the builder stops (+ `--rm`
            # removes it), so a crashed spawner doesn't leak a running builder.
            "--rm",
            *_beacon_health_args(beacon_port),
            *_SANDBOX_RUN_ARGS,
            "-v",
            f"{STORE_VOLUME}:/nix",
            "-v",
            f"{CACHE_VOLUME}:{CACHE_MOUNT}",
            # /workdir is read-write so `nix flake lock` can write flake.lock
            # back to the host workdir (parity with the host backend; the
            # untrusted build runs sandboxed off a store copy and never sees
            # this mount, so only nix-the-tool writes here).
            "-v",
            f"{repo_root}:{WORKDIR_MOUNT}",
            "-v",
            f"{materialize_root}:{MATERIALIZE_MOUNT}",
            *self._mountable_path_args(),
            "-e",
            f"XDG_CACHE_HOME={CACHE_MOUNT}",
            self._config.image,
            "sleep",
            "infinity",
        ]
        out = await _run_checked(run_args, what=f"builder run for {repo_root}")
        container_id = out.strip()

        try:
            boot = await _run_checked(
                [PODMAN, "exec", container_id, "sh", "-c", self._bootstrap_script(filter_syscalls)],
                what="builder bootstrap",
            )
            if "BOOTSTRAP_OK" not in boot:
                raise PodmanError(f"builder bootstrap incomplete: {boot}")
            if self._config.multi_user:
                await self._start_daemon(container_id)
        except BaseException:
            # Don't leak a half-built container if bootstrap fails (created with
            # `--stop-timeout=0`, so `rm -f` kills immediately).
            await _run([PODMAN, "rm", "-f", container_id])
            raise

        return _Builder(
            container_id=container_id, repo_root=repo_root, multi_user=self._config.multi_user
        )

    async def _start_daemon(self, container_id: str) -> None:
        async with perf_span("builder.daemon"):
            await _run_checked(
                [
                    PODMAN,
                    "exec",
                    "-d",
                    container_id,
                    "sh",
                    "-c",
                    "nix-daemon > /tmp/daemon.log 2>&1",
                ],
                what="nix-daemon launch",
            )
            # Wait for the daemon socket to appear.
            for poll in range(40):
                _, out, _ = await _run(
                    [
                        PODMAN,
                        "exec",
                        container_id,
                        "sh",
                        "-c",
                        f"test -S {DAEMON_SOCKET} && echo UP",
                    ]
                )
                if "UP" in out:
                    record("builder.daemon.ready", polls=poll + 1)
                    return
                await anyio.sleep(0.25)
        log = await _run([PODMAN, "exec", container_id, "cat", "/tmp/daemon.log"])
        raise PodmanError(f"nix-daemon did not come up:\n{log[1]}{log[2]}")

    async def _stop_builder(self, builder: _Builder) -> None:
        # Tear down the lazily-created deliverer alongside the builder (shared
        # lifetime), then the builder itself. Both are created with
        # `--stop-timeout=0` (see `_start_builder` / `_start_deliverer`), so
        # `rm -f` SIGKILLs immediately instead of waiting the 10s stop timeout
        # on `sleep infinity` (which ignores SIGTERM).
        async with perf_span("builder.stop"):
            if builder.deliverer_id is not None:
                await _run([PODMAN, "rm", "-f", builder.deliverer_id])
            await _run([PODMAN, "rm", "-f", builder.container_id])


class PodmanHandle:
    """A :class:`~fxtr_nix_runner._backend.Handle` over a builder container.

    ``target`` is the path the builder was scoped to (the ``repo`` passed to
    ``builder_for``); ``flake_ref_root`` maps it into the container relative
    to the (mounted) repo root. The underlying ``builder`` is shared across
    borrows of the same repo root; ``target`` is per-borrow.
    """

    def __init__(
        self,
        builder: _Builder,
        config: PodmanConfig,
        target: Path,
        *,
        backend: PodmanBackend,
        repo_root: Path,
    ):
        self._builder = builder
        self._config = config
        self._target = target
        # For transport-failure recycle: the backend + registry key the handle
        # tells to invalidate this builder when a `podman exec` reports it gone.
        self._backend = backend
        self._repo_root = repo_root

    def flake_ref_root(self) -> str:
        target = self._target
        root = self._builder.repo_root
        if target == root:
            return WORKDIR_MOUNT
        if target.is_relative_to(root):
            return f"{WORKDIR_MOUNT}/{target.relative_to(root)}"
        # The builder is keyed/mounted on `root`; only the repo it was scoped
        # to is visible in the container. A target outside it can't be a valid
        # in-container path — fail loud rather than hand back a path that won't
        # exist in the build.
        raise PodmanError(
            f"flake_ref_root target {target} is not under the builder's repo "
            f"root {root}; the builder can only build paths within the repo it "
            "was scoped to."
        )

    async def _exec(
        self,
        argv: Sequence[str],
        *,
        container_id: str | None = None,
        nix_remote: bool = False,
        on_dead: Callable[[], Awaitable[None]] | None = None,
    ) -> NixResult:
        """Run ``podman exec <container> <argv>`` and return its (non-raising)
        result — **except** on the exec-transport-failure code (the container is
        gone), where it runs ``on_dead`` (to recycle the dead container) and
        raises :class:`BuilderTransportError`.

        ``container_id`` defaults to the builder; pass the deliverer's id for a
        deliverer exec. ``nix_remote`` adds ``NIX_REMOTE=daemon`` (multi-user
        builder daemon) for nix commands.
        """
        cid = container_id if container_id is not None else self._builder.container_id
        cmd = [PODMAN, "exec"]
        if nix_remote and self._builder.multi_user:
            cmd += ["-e", "NIX_REMOTE=daemon"]
        cmd.append(cid)
        cmd += list(argv)
        rc, out, err = await _run(cmd)
        if rc == _EXEC_TRANSPORT_FAILURE_RC:
            if on_dead is not None:
                await on_dead()
            raise BuilderTransportError(self._repo_root, err)
        return NixResult(returncode=rc, stdout=out, stderr=err)

    async def _invalidate_self(self) -> None:
        """`on_dead` for builder execs: drop this warm builder so the next
        borrow rebuilds it."""
        await self._backend._invalidate_builder(self._repo_root, self._builder)

    async def _recycle_deliverer(self) -> None:
        """`on_dead` for the deliverer exec: clear the cached (dead) deliverer id
        so the next ``_ensure_deliverer`` recreates it; the warm builder is left
        alone. Best-effort ``rm -f`` of the corpse."""
        builder = self._builder
        async with builder.deliverer_lock:
            dead, builder.deliverer_id = builder.deliverer_id, None
        if dead is not None:
            await _run([PODMAN, "rm", "-f", dead])

    @asynccontextmanager
    async def expose_host_endpoint(self, host_port: int) -> AsyncIterator[ExposedEndpoint]:
        # The builder container reaches the host via `host.containers.internal`
        # (the gvproxy gateway), so a
        # host-bound server is reachable with no tunnel — provided it binds a
        # non-loopback interface (0.0.0.0); the caller owns that binding choice.
        yield ExposedEndpoint(url_for_builds=f"http://host.containers.internal:{host_port}")

    async def run_nix(self, argv: Sequence[str]) -> NixResult:
        # Pure transport for nix outcomes (returns NixResult incl. nonzero nix
        # exits). Build-time env delivery (impure-env flags) is prepended by the
        # manager's BuildSpan; the builder daemon's nix.conf carries
        # `configurable-impure-env` so those flags take effect when they arrive.
        # Raises BuilderTransportError only if the builder container itself is
        # gone (rc 125), after invalidating it so the next borrow rebuilds.
        return await self._exec(["nix", *argv], nix_remote=True, on_dead=self._invalidate_self)

    async def lock(self) -> None:
        # `nix flake lock <flake dir>` writes flake.lock into the RW-mounted
        # workdir, which lands on the host workdir via the bind mount. Use the
        # subpath-aware in-container path (not the bare repo mount) so a flake
        # at a repo *subdir* locks the right flake: when the workdir is a git
        # repo the builder is keyed/mounted on the repo root, and the flake we
        # want may live under it (`/workdir/<subdir>`).
        result = await self.run_nix(["flake", "lock", self.flake_ref_root()])
        if result.returncode != 0:
            raise PodmanError(f"nix flake lock failed:\n{result.stderr}")

    async def materialize(self, store_path: str, *, closure: bool) -> Materialized:
        """Copy ``store_path`` (closure-aware) into the host-readable store.

        Limitation — case-insensitive host store: ``nix copy`` reproduces the
        store path's tree verbatim, so a closure containing case-colliding
        names (the classic offender is linux ``ncurses`` terminfo, e.g.
        ``2621a`` vs ``2621A``) fails with "File exists" when ``materialize_root``
        is on a case-insensitive filesystem (default macOS APFS). That's almost
        always a sign of materializing more than the host needs — e.g. a full
        linux runtime closure where only pure-python files are wanted (see
        ``nix/lib/_mkGuestSitePackages.nix``, which strips it to a
        zero-dependency closure). If a genuinely case-colliding closure must be
        materialized, point ``materialize_root`` at a case-sensitive volume.
        """
        # `nix copy` is always closure-aware; the `closure` flag is part of
        # the interface for the host backend's benefit and a no-op here.
        del closure
        result = await self._exec(
            [
                "nix",
                "copy",
                "--no-check-sigs",
                "--to",
                f"local?root={MATERIALIZE_MOUNT}",
                store_path,
            ],
            nix_remote=True,
            on_dead=self._invalidate_self,
        )
        if result.returncode != 0:
            raise PodmanError(
                f"nix copy of {store_path} failed (rc={result.returncode}):\n{result.stderr}"
            )
        base = Path(store_path).name
        root = self._config.materialize_root
        host_path = root / "nix" / "store" / base
        # The reader remap: in-container `/nix/store/...` -> the host store.
        remap = (Path("/nix/store"), root / "nix" / "store")
        return Materialized(host_path=host_path, remap=remap)

    async def deliver_image(self, image_json: str, *, runtime: ContainerRuntime) -> DeliveredImage:
        if runtime.type != "podman":
            raise PodmanError(
                f"the podman nix backend delivers only to a podman-type runtime, not "
                f"{runtime.cmd!r} (type {runtime.type!r}); use FXTR_NIX_BACKEND=host to "
                "deliver a host-built image to a non-podman runtime."
            )
        async with perf_span("deliver.image"):
            image_ref = _delivery.image_ref_for(Path(image_json).name, runtime)
            platform = await self._read_platform(image_json)
            if await _delivery.image_in_runtime(runtime, image_ref):
                record("deliver.cache_hit")
                return DeliveredImage(image_ref=image_ref, platform=platform)
            record("deliver.cache_miss")
            async with perf_span("deliver.build_skopeo"):
                skopeo = await self._build_skopeo()
            async with perf_span("deliver.ensure_deliverer"):
                deliverer = await self._ensure_deliverer()
            # The deliverer holds the socket + runs the pre-built skopeo (from
            # the shared /nix volume) — it never runs an untrusted build. Routed
            # through _exec against the *deliverer*: if that container is gone
            # (rc 125), recycle just the deliverer id (leave the warm builder)
            # and raise so the caller retries the delivery.
            async with perf_span("deliver.skopeo_copy"):
                result = await self._exec(
                    [
                        skopeo,
                        "--insecure-policy",
                        "copy",
                        f"nix:{image_json}",
                        f"docker-daemon:{image_ref}",
                        "--dest-daemon-host",
                        f"unix://{DELIVERER_SOCKET_MOUNT}",
                    ],
                    container_id=deliverer,
                    on_dead=self._recycle_deliverer,
                )
                if result.returncode != 0:
                    raise PodmanError(
                        f"in-container skopeo delivery of {image_json} failed "
                        f"(rc={result.returncode}):\n{result.stderr}"
                    )
        return DeliveredImage(image_ref=image_ref, platform=platform)

    async def _read_platform(self, image_json: str) -> str:
        """Read the manifest's platform from inside the builder (the image
        JSON lives in the `/nix` volume, not on the host fs)."""
        result = await self._exec(["cat", image_json], on_dead=self._invalidate_self)
        if result.returncode != 0:
            raise PodmanError(
                f"reading image manifest {image_json} failed "
                f"(rc={result.returncode}):\n{result.stderr}"
            )
        return _delivery.platform_from_manifest(result.stdout)

    async def _build_skopeo(self) -> str:
        """Build the fxtr-pinned skopeo in the builder (sandbox=true protects
        its store-path integrity); return the in-container binary path."""
        result = await self.run_nix(
            ["build", "--no-link", "--print-out-paths", f"{_delivery.SKOPEO_FLAKE_REF}^out"]
        )
        if result.returncode != 0:
            raise PodmanError(f"building skopeo in the builder failed:\n{result.stderr}")
        return f"{result.stdout.strip().splitlines()[-1]}/bin/skopeo"

    async def _ensure_deliverer(self) -> str:
        """Lazily start the deliverer (shares the builder's lifetime); return
        its container id. Once-only under concurrent deliveries."""
        builder = self._builder
        async with builder.deliverer_lock:
            if builder.deliverer_id is None:
                builder.deliverer_id = await self._start_deliverer()
            return builder.deliverer_id

    async def _start_deliverer(self) -> str:
        # The VM-side socket path is the `-v` source to mount into the
        # deliverer (sources resolve VM-side); `podman info` reports it,
        # sometimes with a `unix://` scheme prefix that a bind source must not
        # carry (else podman misparses the `src:dst` colons).
        vm_socket = (
            await _run_checked(
                [PODMAN, "info", "--format", "{{.Host.RemoteSocket.Path}}"],
                what="podman VM-socket discovery",
            )
        ).strip()
        vm_socket = vm_socket.removeprefix("unix://")
        beacon_port = await self._backend._ensure_beacon()
        repo_hash = _hash12(self._builder.repo_root)
        name = f"{DELIVERER_LABEL}-{repo_hash}-{uuid.uuid4().hex[:8]}"
        out = await _run_checked(
            [
                PODMAN,
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"{DELIVERER_LABEL}={repo_hash}",
                "--platform",
                "linux/amd64",
                # Kill-on-stop: PID 1 is `sleep infinity` (ignores SIGTERM) and
                # there's nothing to flush — see the builder run for rationale.
                "--stop-timeout",
                "0",
                # Self-teardown on host death (same beacon health check as the
                # builder), so a crashed spawner doesn't leak the deliverer.
                "--rm",
                *_beacon_health_args(beacon_port),
                # The socket is the security boundary, so label=disable is fine
                # here. No repo, no build — the deliverer only execs the
                # pre-built skopeo.
                "--security-opt",
                "label=disable",
                "-v",
                f"{STORE_VOLUME}:/nix",
                "-v",
                f"{vm_socket}:{DELIVERER_SOCKET_MOUNT}",
                self._config.image,
                "sleep",
                "infinity",
            ],
            what="deliverer run",
        )
        return out.strip()


def _hash12(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:12]
