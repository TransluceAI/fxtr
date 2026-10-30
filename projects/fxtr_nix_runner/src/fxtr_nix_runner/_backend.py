"""The backend interface: ``Backend`` + the per-repo ``Handle``.

A backend is resolved once (from ``FXTR_NIX_BACKEND`` + config — see
:func:`fxtr_nix_runner.resolve_backend`). Per-repo work goes through a
**refcounted async context manager**, ``backend.builder_for(repo)``, which
yields a :class:`Handle`. The nix operations are methods on the handle, so
they all run against the repo's already-ensured builder (on the podman
backend) and lifetime stays structured.

The host backend's ``builder_for`` is a trivial no-op CM whose handle just
shells out to host nix; the podman backend's manages a real builder
container.

Terms used throughout:

- **store path** — a ``/nix/store/<hash>-<name>`` path *as a string*. On the
  host backend it's a real host filesystem path; on the podman backend it's a
  path *inside the builder container's* ``/nix`` volume and is **not**
  readable from the host (which is why ``materialize`` / ``deliver_image``
  exist — they're the only way the host gets at a podman-backend build's
  output).
- **runtime** — a resolved :class:`~fxtr_nix_runner._config.ContainerRuntime`
  (``cmd`` + concrete ``type`` ∈ {``podman``, ``docker_compatible``}). It's the
  runtime whose image store an image is loaded into and that later runs it.
  Runtime *selection* is the caller's (fxtr_sandboxing's) concern — the backend
  is told which one to target.
- **platform** — a docker/OCI platform string ``"<os>/<arch>"`` (e.g.
  ``"linux/amd64"``) suitable for ``<runtime> run --platform``.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from fxtr_nix_runner._config import ContainerRuntime


@dataclass(frozen=True)
class NixResult:
    """The outcome of one nix invocation (see :meth:`Handle.run_nix`).

    Non-raising by contract: a non-zero ``returncode`` is reported here, not
    raised, so each wrapper can map it to its own error type. stdout/stderr
    are already decoded to text.

    Attributes:
        returncode: The ``nix`` process exit code. ``0`` means success;
            non-zero means the eval/build failed (``stderr`` carries why).
        stdout: The full captured standard output, decoded as UTF-8 (never
            ``None``; ``""`` if nix wrote nothing).
        stderr: The full captured standard error, decoded as UTF-8 (the nix
            diagnostic on failure; ``""`` on a clean run).
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DeliveredImage:
    """A nix2container image loaded into a container runtime's image store,
    ready to run. Returned by :meth:`Handle.deliver_image`.

    Attributes:
        image_ref: The reference the *target runtime* now knows the image by —
            pass it directly to ``<runtime> run`` / ``image inspect``. It's
            content-addressed (derived from the manifest's nix store hash) and,
            under podman, ``localhost/``-qualified (e.g.
            ``localhost/fxtr-nix-service:<hash>-image-<service>``) so podman's
            unqualified-name registry resolution can't interfere.
        platform: The ``"<os>/<arch>"`` string (e.g. ``"linux/amd64"``) to run
            the image with as ``<runtime> run --platform <platform>``. Read
            from the image manifest's ``arch``; required because runtimes
            resolve a loaded image index against the *host* platform and won't
            auto-select the sole variant on an emulated arch.
    """

    image_ref: str
    platform: str


@dataclass(frozen=True)
class ExposedEndpoint:
    """A host TCP endpoint made reachable from inside this backend's builds.

    Yielded by :meth:`Handle.expose_host_endpoint`. Carries the URL a build
    (in particular a fixed-output derivation, which is the only build step
    allowed network access) should dial to reach a server the caller is
    running on a host port.

    Attributes:
        url_for_builds: The base URL valid *inside builds* on this backend —
            e.g. the value to deliver as ``FXTR_REMOTE_BASE_URL``. On the host
            backend this is ``http://127.0.0.1:<port>`` (builds share the host
            network namespace; FODs are sandbox-network-exempt). On the podman
            backend it is ``http://host.containers.internal:<port>`` (the
            gvproxy gateway routes it to the host — the host server must bind a
            non-loopback interface, i.e. ``0.0.0.0``).
    """

    url_for_builds: str


@dataclass(frozen=True)
class Materialized:
    """A store path (and its closure) made readable from the host filesystem.
    Returned by :meth:`Handle.materialize`.

    Attributes:
        host_path: A real host filesystem path at which the materialized
            store path's contents can be read directly (no nix needed on the
            host). On the host backend this *is* the original store path; on
            the podman backend it's the path's location inside the host
            bind-mount store (``<materialize_root>/nix/store/<base>``).
        remap: ``None`` when the materialized contents already use host-valid
            paths (always the case on the host backend). Otherwise the
            ``(old_prefix, new_prefix)`` pair the v2 layered-store reader needs
            (as a ``ParentPathRemap``) to rewrite the *absolute parent-layer
            paths recorded inside a materialized layer closure* — its
            ``parents.json`` holds the in-container ``/nix/store`` paths, which
            don't exist on the host, so ``old_prefix=/nix/store`` is swapped
            for ``new_prefix=<materialize_root>/nix/store``. Consumers that
            open a ``LayeredFileStoreReader`` over ``host_path`` thread this
            into its ``parent_path_remap`` (a no-op when ``None``).
    """

    host_path: Path
    remap: tuple[Path, Path] | None


def impure_env_nix_flags(extra_build_env: Mapping[str, str] | None) -> list[str]:
    """Translate an env mapping into the nix flags that deliver it to FOD builds.

    A **pure helper** (not used by the transport tier): the fxtr2-aware caller
    that owns the build-time rehost policy (the manager's ``BuildSpan``) calls
    this and **prepends** the result as *global* flags before a ``run_nix``
    argv — ``nix <these flags> <subcommand> …``. They're global options, so they
    sit before the subcommand and need no knowledge of the argv's shape (no
    positional splicing). Returns ``[]`` when there's nothing to deliver.

    Fixed-output derivations read declared ``impureEnvVars`` from the build
    environment; the cross-backend channel for setting them *per build* is the
    ``impure-env`` setting, gated behind the ``configurable-impure-env``
    experimental feature — hence the pair
    ``--extra-experimental-features configurable-impure-env --option impure-env
    "K1=V1 K2=V2"``.

    The ``impure-env`` value is a space-separated ``K=V`` list, so neither keys
    nor values may contain spaces (raises if they do). Our values — a base URL
    and a hex bearer token — are space-free; the guard catches misuse.

    Note (multi-user daemons): ``configurable-impure-env`` must also be in the
    *daemon's* ``experimental-features`` (a client flag does not propagate), and
    the invoking user must be trusted. The podman backend owns its builder
    daemon's config; the host backend documents these as prerequisites.
    """
    if not extra_build_env:
        return []
    bad = [k for k, v in extra_build_env.items() if " " in k or " " in v]
    if bad:
        raise ValueError(f"impure-env keys/values must not contain spaces (offending keys: {bad})")
    env_str = " ".join(f"{k}={v}" for k, v in extra_build_env.items())
    return [
        "--extra-experimental-features",
        "configurable-impure-env",
        "--option",
        "impure-env",
        env_str,
    ]


@runtime_checkable
class Handle(Protocol):
    """Per-repo handle — the seam each nix wrapper calls through.

    Obtained from ``async with backend.builder_for(repo) as handle:`` and
    valid only inside that block. All methods operate against the repo the
    handle was scoped to (on the podman backend, its builder container).
    """

    def flake_ref_root(self) -> str:
        """The path string to embed in a flake ref for this handle's repo.

        Returns the location of the ``repo`` passed to ``builder_for`` *as the
        nix invocation will see it* — drop it into a ``git+file://<root>?rev=``
        ref or a bare ``<root>#<attr>`` ref. Pure (no subprocess); the builder
        is already ensured.

        Returns:
            On the host backend, the resolved host path of the repo. On the
            podman backend, the in-container mount path: ``/workdir`` when the
            repo *is* the builder's git-repo-root, or ``/workdir/<relpath>``
            when it's a subdirectory of that root.
        """
        ...

    def expose_host_endpoint(self, host_port: int) -> AbstractAsyncContextManager[ExposedEndpoint]:
        """Make a server on host ``host_port`` reachable from inside builds.

        Returns an async CM yielding an :class:`ExposedEndpoint` whose
        ``url_for_builds`` is the address a build (a network-exempt FOD) should
        dial to reach that host port. The caller starts its server first, then
        wraps the build steps in this CM and delivers ``url_for_builds`` into the
        build env (the manager builds the ``impure-env`` flags via
        :func:`impure_env_nix_flags` and prepends them to its ``run_nix`` argv).

        No tunnel is needed on either backend, so this is a thin URL
        computation today — but it stays a CM so a backend that needs a real
        forwarder can set one up for the block's duration without changing
        callers.

        Args:
            host_port: The host TCP port the caller's server is bound to. The
                host backend assumes loopback reaches it; the podman backend
                requires the server bound to a non-loopback interface
                (``0.0.0.0``) so ``host.containers.internal`` can route to it.
        """
        ...

    async def run_nix(self, argv: Sequence[str]) -> NixResult:
        """Run one ``nix`` invocation and return its (non-raising) result.

        Pure transport: runs exactly ``nix <argv>`` (host) / ``podman exec …
        nix <argv>`` (podman) — it does **not** interpret or inject anything.
        Build-time env delivery (the rehost's ``FXTR_REMOTE_*`` →
        ``impure-env`` flags) is the manager tier's concern: the manager's
        ``BuildSpan.run_nix`` prepends :func:`impure_env_nix_flags` to the argv
        before calling this. Keeps the transport free of nix policy.

        Args:
            argv: The ``nix`` CLI arguments **including the subcommand** —
                e.g. ``["build", "--no-link", "--print-out-paths", ref]`` or
                ``["eval", "--json", ref, "--apply", fn]``. Do **not** prepend
                ``"nix"``; the backend supplies it (and, on the podman backend,
                the ``podman exec <builder>`` prefix + ``NIX_REMOTE=daemon``).

        Returns:
            A :class:`NixResult`. The call does not raise on a non-zero nix
            exit — inspect ``result.returncode`` and raise your own error.

        Raises:
            A backend transport error **only** when the transport itself is
            unusable, not for nix-level failures (those come back as a non-zero
            ``NixResult``). On the podman backend that is
            ``BuilderTransportError`` — the builder container is gone (a
            ``podman exec`` returned rc 125); the backend has already invalidated
            the warm builder so the next ``builder_for`` rebuilds it, letting a
            caller catch this and retry. The host backend never raises it.
        """
        ...

    async def lock(self) -> None:
        """Create or refresh the workdir's ``flake.lock`` (``nix flake lock``).

        Writes ``flake.lock`` back to the handle's repo on the host. The
        frozen build paths (spawn / set-preset) require a committed lock, and
        an agent that adds a flake input needs to relock — this is how that
        happens without host nix.

        host: ``nix flake lock`` against the workdir in place. podman: run in
        the builder, whose ``/workdir`` is mounted read-write *for this* (the
        only write surface the builder gets — the untrusted build itself runs
        sandboxed off a store copy and never sees ``/workdir``); the new
        ``flake.lock`` lands on the host workdir via the bind mount.

        Needs the flake's inputs reachable (network, or local paths under the
        configured ``mountable_paths``).

        Raises:
            Exception: If locking fails (e.g. an input can't be fetched).
        """
        ...

    async def materialize(self, store_path: str, *, closure: bool) -> Materialized:
        """Make a build output readable from the host filesystem.

        On the host backend this is identity (store paths are already
        host-readable). On the podman backend the path lives in the builder's
        ``/nix`` volume, invisible to the host; this ``nix copy``-es it into a
        persistent host bind-mount store.

        Args:
            store_path: The nix store path to materialize (typically a
                ``run_nix(["build", ...])`` output). Must be a valid path in
                the backend's store.
            closure: Whether the path's full runtime closure (everything it
                references) must also be made host-readable, not just the path
                itself. Pass ``True`` for layer directories, whose
                ``parents.json`` points at *other* store paths that the reader
                will follow; self-contained outputs can pass ``False``.

        Returns:
            A :class:`Materialized` whose ``host_path`` is where to read the
            result, plus the reader ``remap`` (``None`` on the host backend).
        """
        ...

    async def deliver_image(self, image_json: str, *, runtime: ContainerRuntime) -> DeliveredImage:
        """Load a built nix2container image into a runtime's image store.

        Builds the fxtr-pinned ``nix2container`` skopeo, reads the manifest's
        platform, and loads the image under a content-addressed ref: host
        skopeo → the runtime's API socket on the host backend; the deliverer
        container → the podman socket on the podman backend. Idempotent — if
        the ref is already in the runtime's store the copy is skipped.

        Args:
            image_json: The store path (as a string) of the **nix2container
                image manifest** — the output of building a service's
                ``<attr>.image`` (a small JSON file referencing the image's
                layer store paths). On the podman backend this is an
                in-container store path, read where the store lives.
            runtime: The :class:`~fxtr_nix_runner._config.ContainerRuntime` to
                deliver into (see the module "runtime" term). It must be the
                runtime that will run the image. The **podman backend only
                supports a podman-type runtime** — any other ``type`` raises.

        Returns:
            A :class:`DeliveredImage` with the ``image_ref`` to run and the
            ``--platform`` to run it with.

        Raises:
            Exception: If delivery fails, or if this backend can't deliver to
                ``runtime`` (the podman backend rejects a non-podman runtime).
        """
        ...


@runtime_checkable
class Backend(Protocol):
    """A resolved nix-runner backend (host or podman).

    Stateful and reusable: a long-lived consumer holds one instance so that
    concurrent ``builder_for`` calls for the same repo refcount-share a single
    builder container; a one-shot caller can resolve one per operation.
    """

    def builder_for(self, repo: Path) -> AbstractAsyncContextManager[Handle]:
        """Open a per-repo :class:`Handle` for the duration of an ``async
        with`` block.

        Args:
            repo: A local filesystem path identifying the flake to build from.
                Its **git repo root** (found by walking up to a ``.git``, or
                the path itself if none) is what keys + mounts the builder on
                the podman backend; the path *itself* is the ref target that
                the handle's :meth:`Handle.flake_ref_root` reports (so a
                workdir subdirectory maps to ``/workdir/<relpath>``).

        Returns:
            An async context manager yielding a :class:`Handle`. On the podman
            backend, entering it refcount-ensures the repo's builder container
            (lazy ``run -d`` on first entrant, ``rm -f`` when the last
            concurrent borrower exits); on the host backend it's a no-op CM.
            The handle is only valid inside the ``with`` block.
        """
        ...

    def keep_builder_warm(self, repo: Path) -> AbstractAsyncContextManager[None]:
        """Keep ``repo``'s builder warm for the duration of an ``async with``
        block, so repeated :meth:`builder_for` borrows inside reuse one builder
        instead of paying its start/stop each time.

        Lazy and non-instantiating: entering does **not** start a builder (an
        env that never builds still starts nothing). The first ``builder_for``
        inside starts it; it then survives the borrow-count returning to 0
        (instead of being torn down) until this block exits, at which point —
        if no borrow is still live — it is torn down. Reentrant: nested warms of
        the same repo share.

        Yields ``None``: callers keep calling ``builder_for`` exactly as
        normal; this only extends the underlying builder's lifetime. On the host
        backend it is a no-op CM (there is no container to keep warm).

        Args:
            repo: A path whose **git repo root** keys the builder (same keying
                as :meth:`builder_for`), so the warm builder serves every
                ``builder_for`` for that repo.
        """
        ...
