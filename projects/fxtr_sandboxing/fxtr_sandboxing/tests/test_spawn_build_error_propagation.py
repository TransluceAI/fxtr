"""Regression: a build/eval failure during a nix spawn must propagate
its real message to the caller, not get collapsed into an opaque
empty-messaged ``ExceptionGroup``.

See ``projects/fxtr_workstation/plans/phase0/part6_adjustments/
stage6_spawn_exception_fix.md`` (Issue 1).

``_spawn_runner_nix_*`` runs the nix build *inside*
``_contain_failures_after_started`` and *before* it reports
``status.started()``. The nix wrapper raises a plain ``RuntimeError``
with the nix/git stderr in the message; that error must ride back
through ``env_tg.start(...)`` as the ``spawn`` invoke's error with the
stderr intact. The blast-radius guard only contains failures that
happen *after* ``started`` (covered by
``test_spawner_blast_radius.py``); a pre-``started`` failure must pass
through unchanged.

This test exercises the guard directly (no nix / no eryx): a body that
raises before ``started`` must surface the original ``RuntimeError``,
not the ``except*``-synthesized group whose ``str`` is the useless
``' (1 sub-exception)'``.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr_sandboxing.spawner import _contain_failures_after_started

# Shaped like the real failure: missing committed uv.lock fails nix eval.
_NIX_STDERR = (
    "nix eval failed for ...#sitePackages (exit 1):\n"
    "error: Path 'packages/foo/uv.lock' does not exist in Git repository"
)


async def test_pre_started_build_error_propagates_original_message() -> None:
    async def runner(*, task_status: anyio.abc.TaskStatus[object]) -> None:
        # Body raises before ever reporting ``started`` — the build-failure
        # shape. The guard must let the original exception through.
        async with _contain_failures_after_started("nix-wasm foo @ deadbeef", task_status):
            raise RuntimeError(_NIX_STDERR)

    async with anyio.create_task_group() as tg:
        # Before the fix this raised a bare ``ExceptionGroup`` (``str`` =
        # ``' (1 sub-exception)'``), so ``pytest.raises(RuntimeError)`` failed
        # on the wrong type and the stderr was unrecoverable from the message.
        with pytest.raises(RuntimeError, match="uv.lock"):
            await tg.start(runner)
