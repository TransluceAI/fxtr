"""``fxtr_nix_runner clear`` — cold-reset the podman backend's state.

Normal teardown is the refcounted context manager; this CLI is for
crash-debris recovery + a clean slate. It label-sweeps any leftover
builder/deliverer containers, removes the shared store + eval-cache
volumes, and wipes the materialize scratch dir. The next build cold-starts
(volume copy-up re-initializes ``/nix``).
"""

from __future__ import annotations

import shutil

import anyio
from fxtr_nix_runner._config import PodmanConfig, load_config
from fxtr_nix_runner._podman import CACHE_VOLUME, PODMAN, STORE_VOLUME, _run

# Builder + deliverer containers carry these label keys; sweep both so a
# clear is a label query, not name parsing.
_CONTAINER_LABELS = ("fxtr-nix-builder", "fxtr-nix-deliverer")


async def clear(config: PodmanConfig | None = None) -> list[str]:
    """Cold-reset the podman backend: remove its containers, volumes + scratch.

    Label-sweeps any leftover ``fxtr-nix-builder`` / ``fxtr-nix-deliverer``
    containers (force-removed), removes the shared ``fxtr-nix`` +
    ``fxtr-nix-cache`` volumes, and deletes the materialize scratch dir.
    Tolerant of absent/in-use objects (it skips what it can't remove). Has no
    effect on the host backend (there are no such objects).

    Args:
        config: The podman config (only ``materialize_root`` is read, to know
            which scratch dir to wipe). Defaults to ``load_config().podman``.

    Returns:
        Human-readable lines naming each object actually removed (containers,
        volumes, scratch dir) — empty if there was nothing to remove.
    """
    if config is None:
        config = load_config().podman
    removed: list[str] = []

    for label in _CONTAINER_LABELS:
        rc, out, _ = await _run([PODMAN, "ps", "-aq", "--filter", f"label={label}"])
        if rc != 0:
            continue
        for cid in out.split():
            await _run([PODMAN, "rm", "-f", cid])
            removed.append(f"container {cid} (label {label})")

    for vol in (STORE_VOLUME, CACHE_VOLUME):
        rc, _, _ = await _run([PODMAN, "volume", "rm", "-f", vol])
        if rc == 0:
            removed.append(f"volume {vol}")

    root = config.materialize_root
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        removed.append(f"materialize scratch {root}")

    return removed


def main() -> None:
    removed = anyio.run(clear)
    if removed:
        print("Removed:")
        for line in removed:
            print(f"  {line}")
    else:
        print("Nothing to remove.")
