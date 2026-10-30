"""Smoke test for the guest site-packages path under eryx, built through
the git-ref code path (the same mechanism the spawner uses):

1. `prepare_workdir_repo` copies the `nix/sandbox_smoke/` template into a
   fresh tempdir repo: comment-flips its `[tool.uv.sources]` to the pinned
   git source, rewrites the flake's `fxtr` input, `uv lock` / `nix flake
   lock`, and `git init` + commits — capturing HEAD.
2. `nix build --no-update-lock-file "git+file://<workdir>?rev=<HEAD>#fxtr.services.smoke.sitePackages"`
   builds the service's flat, pure-python site-packages (via `mkWasmService`
   → `_mkGuestSitePackages` → uv2nix). `$out` IS the flat site-packages dir
   (`$out/fxtr2`, ...). `--no-update-lock-file` makes a missing/stale
   committed lock an error rather than silently re-locking in-memory.
3. Bind-mount `$out` into a freshly-built eryx sandbox and run `import
   fxtr2`. Exit code 0 + a stdout marker confirms the
   uv2nix → flat-site-packages → eryx-WASM path works end-to-end.

Dev note: `prepare_workdir_repo` resolves the `fxtr` input via
`resolve_lib_ref`. In a jj workspace it defaults to the workspace's `@`
working-copy commit (snapshotting current edits, transported via the
harness-owned bare mirror), so uncommitted library work is picked up with no
override; `FXTR_TEST_LIB_REV` pins a specific rev instead. In CI (plain git
checkout) it defaults to HEAD, the commit under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import anyio
import eryx
import pytest

from fxtr_workdir_harness import prepare_workdir_repo

_SITE_PACKAGES_GUEST_PATH = "/guest-site-packages"

# nix/py_testing/tests/ -> nix/ -> nix/sandbox_smoke/
_SANDBOX_SMOKE_TEMPLATE = Path(__file__).resolve().parents[2] / "sandbox_smoke"


@pytest.fixture(scope="module", autouse=True)
def _repair_eryx_embedded() -> None:
    """Ensure ``$TMPDIR/eryx-embedded/`` is in a valid state.

    Eryx caches an extracted Python stdlib at
    ``$TMPDIR/eryx-embedded/python-stdlib/``. If macOS prunes the file
    contents but leaves the directory metadata, eryx's pre-initialize
    step crashes with a bare ``InitializationError``. Mirrors
    ``fxtr_sandboxing.runner._check_and_maybe_repair_eryx_embedded_dir``;
    no host-side dep on fxtr_sandboxing needed.
    """
    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir:
        return
    stdlib_dir = Path(tmpdir) / "eryx-embedded" / "python-stdlib"
    canary = stdlib_dir / "encodings" / "__init__.py"
    if stdlib_dir.is_dir() and not canary.exists():
        rotated = stdlib_dir.with_name(f"python-stdlib-old-{uuid.uuid4()}")
        stdlib_dir.rename(rotated)
        shutil.rmtree(rotated)


_PROBE_SCRIPT = f"""
import sys

sys.path.insert(0, "{_SITE_PACKAGES_GUEST_PATH}")

import fxtr2

print("fxtr2 import ok:", fxtr2.__name__)
"""


@pytest.fixture(scope="module")
def site_packages_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build `fxtr.services.smoke.sitePackages` via the git-ref code path."""
    workdir_parent = tmp_path_factory.mktemp("sandbox_smoke")
    # `prepare_workdir_repo` is async (it locks flakes through the runner
    # backend); this fixture is sync, so drive it to completion with anyio.run.
    prepared = anyio.run(prepare_workdir_repo, _SANDBOX_SMOKE_TEMPLATE, workdir_parent / "wd")
    ref = (
        f"git+file://{prepared.path}?rev={prepared.head_commit}" "#fxtr.services.smoke.sitePackages"
    )
    # `--no-update-lock-file` errors rather than silently re-locking if the
    # committed lock is missing/stale.
    out = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths", "--no-update-lock-file", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip().splitlines()[-1])


def test_guest_site_packages_shape(site_packages_path: Path) -> None:
    """`mkWasmService`'s `sitePackages` output is the flat site-packages dir
    itself — `fxtr2` lands at the top level."""
    assert site_packages_path.is_dir()
    assert (site_packages_path / "fxtr2").is_dir()


def test_fxtr2_imports_inside_eryx(site_packages_path: Path) -> None:
    """Bind-mount the flat site-packages into a fresh eryx sandbox; run
    `import fxtr2`. This is the central forcing-function check —
    proves the uv2nix-built pure-python site-packages works under
    eryx's CPython 3.14 WASM."""
    site_packages_host = str(site_packages_path)
    factory = eryx.SandboxFactory()
    sandbox = factory.create_sandbox(
        volumes=[(site_packages_host, _SITE_PACKAGES_GUEST_PATH, True)],
    )
    result = sandbox.execute(_PROBE_SCRIPT)
    assert "fxtr2 import ok:" in result.stdout, f"unexpected stdout from sandbox: {result.stdout!r}"
