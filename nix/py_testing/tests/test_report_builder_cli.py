"""Smoke test for `lib.fxtr.reportBuilderCli` (the pnpm-in-nix MDX→FxtrCbor CLI).

The `report-builder-cli-smoke` flake target runs the built CLI against the README
envelope *inside* its derivation (the bundle is x86_64-linux, so it executes on
the build sandbox, not a mac dev host) and asserts `{ok:true}`; `$out` is the
captured response. Building it here means pnpm.fetchDeps + esbuild-in-derivation
+ makeWrapper all worked and the CLI responds. We also read the response back to
double-check.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def test_report_builder_cli_smoke(nix_build: Callable[[str], str]) -> None:
    out = nix_build("packages.x86_64-linux.report-builder-cli-smoke")
    response = Path(out).read_text()
    assert '"ok":true' in response.replace(" ", ""), response
