"""The host backend: nix runs on the host against the real ``/nix/store``.

Trivial passthrough — the default backend. ``flake_ref_root`` is the
resolved path, ``run_nix`` shells out to host ``nix``, and ``materialize``
is identity (store paths are already host-readable). ``builder_for`` is a
no-op CM (there's no container).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Sequence

import anyio
from fxtr_nix_runner import _delivery
from fxtr_nix_runner._backend import DeliveredImage, ExposedEndpoint, Materialized, NixResult
from fxtr_nix_runner._config import ContainerRuntime
from fxtr_nix_runner.perf import perf_span, proc_label, record


class HostHandle:
    """A :class:`~fxtr_nix_runner._backend.Handle` over host nix.

    ``target`` is the path the builder was scoped to (the ``repo`` passed to
    ``builder_for``) — what ``flake_ref_root`` reports.
    """

    def __init__(self, target: Path):
        self._target = target

    def flake_ref_root(self) -> str:
        return str(Path(self._target).resolve())

    @asynccontextmanager
    async def expose_host_endpoint(self, host_port: int) -> AsyncIterator[ExposedEndpoint]:
        # Host nix builds share the host network namespace and FODs are
        # network-exempt, so loopback reaches a host-bound server directly.
        # Nothing to set up or tear down.
        yield ExposedEndpoint(url_for_builds=f"http://127.0.0.1:{host_port}")

    async def run_nix(self, argv: Sequence[str]) -> NixResult:
        async with perf_span(proc_label(["nix", *argv])):
            result = await anyio.run_process(["nix", *argv], check=False)
        return NixResult(
            returncode=result.returncode,
            stdout=result.stdout.decode(),
            stderr=result.stderr.decode(),
        )

    async def lock(self) -> None:
        # `nix flake lock <workdir>` writes flake.lock in place (the host
        # workdir is the real, writable dir).
        result = await self.run_nix(["flake", "lock", str(Path(self._target).resolve())])
        if result.returncode != 0:
            raise RuntimeError(f"nix flake lock failed:\n{result.stderr}")

    async def materialize(self, store_path: str, *, closure: bool) -> Materialized:
        # Already host-readable; nothing to copy, no remap needed. `closure` is
        # part of the Handle interface (the podman backend's concern) — discard.
        del closure
        return Materialized(host_path=Path(store_path), remap=None)

    async def deliver_image(self, image_json: str, *, runtime: ContainerRuntime) -> DeliveredImage:
        # image_json is host-readable on the host backend: read its name for
        # the ref and its manifest for the platform.
        async with perf_span("deliver.image"):
            image_ref = _delivery.image_ref_for(Path(image_json).name, runtime)
            platform = _delivery.platform_from_manifest(Path(image_json).read_text())
            if await _delivery.image_in_runtime(runtime, image_ref):
                record("deliver.cache_hit")
                return DeliveredImage(image_ref=image_ref, platform=platform)
            record("deliver.cache_miss")
            async with perf_span("deliver.build_skopeo"):
                skopeo = await self._build_skopeo()
            async with perf_span("deliver.host_load"):
                await _delivery.host_load_into_runtime(skopeo, image_json, image_ref, runtime)
        return DeliveredImage(image_ref=image_ref, platform=platform)

    async def _build_skopeo(self) -> str:
        """Build the fxtr-pinned nix2container skopeo on host nix; return the
        binary path."""
        result = await self.run_nix(
            ["build", "--no-link", "--print-out-paths", f"{_delivery.SKOPEO_FLAKE_REF}^out"]
        )
        if result.returncode != 0:
            raise RuntimeError(f"building skopeo failed:\n{result.stderr}")
        return f"{result.stdout.strip().splitlines()[-1]}/bin/skopeo"


class HostBackend:
    """A :class:`~fxtr_nix_runner._backend.Backend` using host nix."""

    @asynccontextmanager
    async def builder_for(self, repo: Path) -> AsyncIterator[HostHandle]:
        # No container, no lifecycle — the host store is always "up".
        yield HostHandle(repo)

    @asynccontextmanager
    async def keep_builder_warm(self, repo: Path) -> AsyncIterator[None]:
        # No container to keep warm — nothing to do.
        del repo
        yield
