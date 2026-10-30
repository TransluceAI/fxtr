"""Git-flake-ref nix wrapper for the spawner's ``NixWasmServiceDefn`` /
``NixContainerServiceDefn`` arms.

Resolves a sandbox service referenced by ``(commit, attr, flake_path)`` into
its built artifacts — entry point + guest site-packages for the wasm kind;
the nix2container image manifest for the container kind — by forming a nix
git flake ref and running it through a ``fxtr_nix_runner`` backend **handle**
(host nix or in-podman; the caller opens ``builder_for(repo)`` and passes the
handle in). Delivering a built container image (skopeo) is the backend's job
(``Handle.deliver_image``), not this wrapper's.

Ref form (local repo only; a remote ``git+ssh://`` / ``git+https://``
constructor gets added when remote deployment lands)::

    git+file://<h.flake_ref_root()>?rev=<commit>&dir=<flake_path>#<attr>

Builds run ``--no-update-lock-file`` so a missing/stale committed
``flake.lock`` is a hard error rather than being silently re-locked in-memory.

On failure the wrapper raises a plain :class:`RuntimeError` with the nix/git
stderr in the message — the spawner doesn't catch it; it propagates as the
``spawn`` invoke's error with the stderr inline (per
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/11_failure_and_ux.md``).
We deliberately do *not* parse stderr to classify the cause. (If a caller ever
needs to catch + handle these distinctly, reintroduce a typed ``NixError``
then — see ``stage5post2_nix_wrapper_simplify.md``.)
"""

from __future__ import annotations

import json
from pathlib import Path

from fxtr_nix_runner import Handle
from fxtr_nix_runner.perf import perf_span

__all__ = [
    "eval_entry_point",
    "eval_service_kind",
    "build_site_packages",
    "build_image",
]


def _attr_url(h: Handle, commit: str, attr: str, flake_path: str | None) -> str:
    """Form the nix git flake ref for the handle's repo at ``commit``.

    ``h.flake_ref_root()`` is the path string to embed (the host path on
    the host backend, ``/workdir`` inside the builder on podman);
    ``flake_path`` (a repo subpath) is threaded in as ``&dir=``.
    """
    ref = f"git+file://{h.flake_ref_root()}?rev={commit}"
    if flake_path:
        ref += f"&dir={flake_path}"
    return f"{ref}#{attr}"


async def _run_nix(h: Handle, args: list[str], *, ref: str, step: str) -> str:
    """Run ``nix <step> <args>`` through the backend; return stdout, or raise
    ``RuntimeError``."""
    result = await h.run_nix([step, *args])
    if result.returncode != 0:
        raise RuntimeError(
            f"nix {step} failed for {ref} (exit {result.returncode}):\n{result.stderr}"
        )
    return result.stdout


async def eval_entry_point(h: Handle, commit: str, attr: str, flake_path: str | None = None) -> str:
    """`nix eval` the service attr's ``entryPoint`` string (no build forced).

    ``attr`` is the service attr path, e.g. ``"fxtr.services.my_service"``;
    we eval ``<attr>.entryPoint``. Raises ``RuntimeError`` on any eval failure.
    """
    ref = _attr_url(h, commit, f"{attr}.entryPoint", flake_path)
    async with perf_span("nix.eval.entryPoint"):
        out = await _run_nix(h, ["--raw", "--no-update-lock-file", ref], ref=ref, step="eval")
    return out.strip()


async def eval_service_kind(
    h: Handle, commit: str, attr: str, flake_path: str | None = None
) -> str | None:
    """`nix eval` the service attr's ``kind`` discriminator (no build forced).

    Returns ``"wasm"`` / ``"container"`` / ``None`` (attr declares no
    ``kind`` — a flake built against a fxtr lib predating the ``kind``
    discriminator, or a hand-built attr missing it). Uses
    ``--apply 's: s.kind or null'`` so a missing leaf is a clean ``null``
    rather than an eval error; selecting the leaf is lazy, so no buildable
    leaves are forced. Raises ``RuntimeError`` on any eval failure (bad
    rev / missing attr / broken flake).
    """
    ref = _attr_url(h, commit, attr, flake_path)
    async with perf_span("nix.eval.kind"):
        out = await _run_nix(
            h,
            ["--json", "--apply", "s: s.kind or null", "--no-update-lock-file", ref],
            ref=ref,
            step="eval",
        )
    kind = json.loads(out)
    return kind if isinstance(kind, str) else None


async def build_site_packages(
    h: Handle, commit: str, attr: str, flake_path: str | None = None
) -> Path:
    """`nix build` the service attr's ``sitePackages``; return a host-readable
    path to it.

    ``--no-link`` (no ``result`` symlink) + ``--print-out-paths``, then
    ``materialize`` so the result is host-readable (identity on the host
    backend; a ``nix copy`` into the host store on podman). The wasm arm
    mounts the returned path read-only into eryx. Raises ``RuntimeError`` on
    any build failure.
    """
    ref = _attr_url(h, commit, f"{attr}.sitePackages", flake_path)
    async with perf_span("nix.build.sitePackages"):
        out = await _run_nix(
            h,
            ["--no-link", "--print-out-paths", "--no-update-lock-file", ref],
            ref=ref,
            step="build",
        )
    store_path = out.strip().splitlines()[-1]
    async with perf_span("nix.materialize"):
        mat = await h.materialize(store_path, closure=True)
    return mat.host_path


async def build_runtime_bundle(
    h: Handle, commit: str, attr: str, flake_path: str | None = None
) -> Path:
    """`nix build` the pyodide attr's ``runtimeBundle``; return a
    host-readable path to it.

    Same shape as :func:`build_site_packages` — build + ``materialize`` so
    the host deno process can read the pyodide assets + guest bootstrap.
    Raises ``RuntimeError`` on any build failure.
    """
    ref = _attr_url(h, commit, f"{attr}.runtimeBundle", flake_path)
    async with perf_span("nix.build.runtimeBundle"):
        out = await _run_nix(
            h,
            ["--no-link", "--print-out-paths", "--no-update-lock-file", ref],
            ref=ref,
            step="build",
        )
    store_path = out.strip().splitlines()[-1]
    async with perf_span("nix.materialize"):
        mat = await h.materialize(store_path, closure=True)
    return mat.host_path


async def build_image(h: Handle, commit: str, attr: str, flake_path: str | None = None) -> Path:
    """`nix build` the container attr's ``image``; return the manifest store
    path (NOT materialized).

    The output is a nix2container image description: a small JSON file whose
    layers reference store paths directly (no tarball). Container images are
    **never** materialized to the host — they're delivered by skopeo from
    inside the store (host skopeo on the host backend; the deliverer on
    podman), so this returns the store path verbatim (a host path on the host
    backend; an opaque in-container path on podman). Raises ``RuntimeError``
    on any build failure.
    """
    ref = _attr_url(h, commit, f"{attr}.image", flake_path)
    async with perf_span("nix.build.image"):
        out = await _run_nix(
            h,
            ["--no-link", "--print-out-paths", "--no-update-lock-file", ref],
            ref=ref,
            step="build",
        )
    return Path(out.strip().splitlines()[-1])
