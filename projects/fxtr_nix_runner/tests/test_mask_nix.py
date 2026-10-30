"""Tests for the ``run-with-masked-nix`` verification tool.

Pure subprocess tests — no host nix or podman needed (the shim shadows ``nix``
regardless of whether a real one is installed).
"""

from __future__ import annotations

import subprocess
import sys


def _run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fxtr_nix_runner._mask_nix", "--", *args],
        capture_output=True,
        text=True,
    )


def test_passthrough_success_exit_code() -> None:
    # No nix involved → the child's exit code (and output) propagate.
    r = _run_tool(sys.executable, "-c", "print('ok')")
    assert r.returncode == 0
    assert "ok" in r.stdout


def test_passthrough_nonzero_exit_code() -> None:
    r = _run_tool(sys.executable, "-c", "import sys; sys.exit(3)")
    assert r.returncode == 3


def test_masks_nix_and_reports_leak() -> None:
    # The wrapped command calls `nix`, resolving to the failing shim. The inner
    # `sh` swallows nix's non-zero exit (`|| true`), so the child returns 0 —
    # but the tool still fails the run because the tripwire fired, and names the
    # leaked invocation.
    r = _run_tool("sh", "-c", "nix --version || true")
    assert r.returncode != 0
    assert "HOST NIX WAS INVOKED" in r.stderr
    assert "nix --version" in r.stderr


def test_masks_nix_family_alias() -> None:
    # An aliased nix-* command is masked too (one shim, symlinked).
    r = _run_tool("sh", "-c", "nix-build || true")
    assert r.returncode != 0
    assert "nix-build" in r.stderr


def test_no_args_is_usage_error() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "fxtr_nix_runner._mask_nix"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2
    assert "usage:" in r.stderr
