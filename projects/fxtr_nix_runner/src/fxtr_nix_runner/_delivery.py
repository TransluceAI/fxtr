"""Store-side image-delivery mechanics, shared by the host + podman backends.

Anyio-only. Delivering a nix2container image means: read the manifest (for
its `arch` → run `--platform`), build the fxtr-pinned skopeo, and load the
image into a container runtime's image store under a content-addressed ref.
The *runtime selection* is the caller's (`fxtr_sandboxing`) — it passes the
runtime in; this module owns the store-side machinery.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Sequence

import anyio
from fxtr_nix_runner._config import ContainerRuntime
from fxtr_nix_runner.perf import perf_span, proc_label

# The nix2container rev the patched skopeo (the `nix:` transport that reads
# image layers straight out of the store) is built from. The image-JSON
# format is stable, so this can drift from a repo flake's own pin.
NIX2CONTAINER_REV = "76be9608a7f4d6c985d28b0e7be903ae2547df3e"
SKOPEO_FLAKE_REF = f"github:nlewo/nix2container/{NIX2CONTAINER_REV}#skopeo-nix2container"

# Repository part of the content-addressed ref images load under. The tag is
# the image manifest's nix store basename, so identical builds collide onto
# the same ref (= cache hit in the runtime's image store) and different builds
# never alias.
IMAGE_REPO = "fxtr-nix-service"


def image_ref_for(image_json_name: str, runtime: ContainerRuntime) -> str:
    """Content-addressed runtime ref for a built image manifest, from its
    store-path basename (``<storehash>-image-<service>.json``).

    Derived from the *name* (a host-visible string even when the file itself
    isn't host-readable, e.g. an in-container store path on the podman
    backend). Characters outside the docker tag alphabet map to ``_`` and the
    tag is capped at 128 chars; both leave the leading store hash (which
    carries the content-addressing) intact. ``localhost/``-qualified under a
    podman-type runtime so its unqualified-name registry resolution can't
    interfere.
    """
    stem = image_json_name.removesuffix(".json")
    tag = re.sub(r"[^a-zA-Z0-9._-]", "_", stem)[:128]
    ref = f"{IMAGE_REPO}:{tag}"
    return f"localhost/{ref}" if runtime.type == "podman" else ref


def platform_from_manifest(manifest_text: str) -> str:
    """The ``--platform`` string for a nix2container manifest's ``arch``
    (OCI-style, e.g. ``"amd64"``); the OS is always linux for our images."""
    manifest = json.loads(manifest_text)
    return f"linux/{manifest['arch']}"


async def _run(argv: Sequence[str]) -> tuple[int, str, str]:
    async with perf_span(proc_label(argv)):
        result = await anyio.run_process(list(argv), check=False)
    return result.returncode, result.stdout.decode(), result.stderr.decode()


async def _run_checked(argv: Sequence[str], *, what: str) -> str:
    rc, out, err = await _run(argv)
    if rc != 0:
        raise RuntimeError(f"{what} failed (rc={rc}):\n{out}\n{err}")
    return out


async def resolve_podman_socket(cmd: str) -> str:
    """The host-side podman API socket, as a ``unix://`` URL.

    ``cmd`` is the podman executable (``ContainerRuntime.cmd``). Discovery
    order: ``podman machine inspect`` (macOS / machine VMs — the host-forwarded
    socket), then ``podman info .Host.RemoteSocket.Path`` (native Linux; on
    macOS this reports the socket *inside the VM*, so it's checked second).
    ``CONTAINER_HOST`` / ``CONTAINERS_*`` env overrides are honored implicitly
    (they redirect the podman CLI, and both commands report the redirected
    daemon's socket).
    """
    rc, out, _ = await _run(
        [cmd, "machine", "inspect", "--format", "{{.ConnectionInfo.PodmanSocket.Path}}"]
    )
    if rc == 0 and out.strip():
        return f"unix://{out.strip()}"

    path = (
        await _run_checked(
            [cmd, "info", "--format", "{{.Host.RemoteSocket.Path}}"],
            what="podman API-socket discovery (podman info)",
        )
    ).strip()
    if not path:
        raise RuntimeError(
            "podman API-socket discovery: no machine socket and `podman info` reports "
            "no remote socket path. Is the podman socket enabled (`podman machine start` "
            "on macOS, `systemctl --user start podman.socket` on Linux)?"
        )
    return path if path.startswith("unix://") else f"unix://{path}"


async def image_in_runtime(runtime: ContainerRuntime, image_ref: str) -> bool:
    """Whether ``image_ref`` is already in ``runtime``'s image store (the
    delivery cache — a hit skips the copy)."""
    rc, _, _ = await _run([runtime.cmd, "image", "inspect", image_ref])
    return rc == 0


async def host_load_into_runtime(
    skopeo: str, image_json: str, image_ref: str, runtime: ContainerRuntime
) -> None:
    """Load a (host-readable) image manifest into ``runtime`` via host skopeo.

    podman-type → tarless ``skopeo copy nix:<json> docker-daemon:<ref>`` against
    the API socket; ``docker_compatible`` → transient ``oci-archive`` + ``image
    load``. ``--insecure-policy``: the ``nix:`` transport has no signature
    policy — the trust boundary is the nix store itself.
    """
    if runtime.type == "podman":
        socket = await resolve_podman_socket(runtime.cmd)
        await _run_checked(
            [
                skopeo,
                "--insecure-policy",
                "copy",
                "--dest-daemon-host",
                socket,
                f"nix:{image_json}",
                f"docker-daemon:{image_ref}",
            ],
            what=f"skopeo copy of {image_json} into podman",
        )
        return

    tar_dir = tempfile.mkdtemp(prefix="fxtr_oci_")
    tar_path = os.path.join(tar_dir, "image.tar")
    try:
        await _run_checked(
            [
                skopeo,
                "--insecure-policy",
                "copy",
                f"nix:{image_json}",
                f"oci-archive:{tar_path}:{image_ref}",
            ],
            what=f"skopeo copy of {image_json}",
        )
        await _run_checked(
            [runtime.cmd, "image", "load", "-i", tar_path],
            what=f"{runtime.cmd} image load of {image_ref}",
        )
    finally:
        shutil.rmtree(tar_dir, ignore_errors=True)
