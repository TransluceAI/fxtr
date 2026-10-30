"""``run-with-masked-nix`` — run a command with host nix masked + tripwired.

A verification tool, not part of the normal test path: it runs an arbitrary
command (e.g. ``pytest …``) with **host nix shadowed by failing shims**, so any
accidental host-nix invocation fails loudly *and* is recorded. Use it to prove
a podman-backed run is genuinely host-nix-free (the dev box / CI generally have
host nix on ``PATH``, so "it passed" alone doesn't prove independence).

How it works:

- A temp dir of executable shims for ``nix`` + the ``nix-*`` family is
  **prepended to ``PATH``**. Each shim appends its argv to a tripwire file and
  exits non-zero. So a host-nix call both breaks the caller (non-zero exit) and
  leaves a record (caught even if the caller swallows the exit code).
- The wrapped command runs with that ``PATH``; the temp dir is removed in a
  ``finally`` (so a crash/Ctrl-C still cleans up — hence ``subprocess.run``,
  never ``exec``).
- Afterwards: if the tripwire fired, print the leaked invocations and exit
  non-zero regardless of the child's own exit code; otherwise propagate it.

Only the *host* ``nix`` CLI is masked. A podman-backed run reaches nix via
``podman exec`` inside the builder container — untouched — which is the point.

Usage::

    run-with-masked-nix -- env FXTR_NIX_RUNNER_CONFIG=<toml> pytest tests/...
    python -m fxtr_nix_runner._mask_nix -- pytest -k something
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# nix CLIs to shadow. We only ever call `nix`, but masking the classic family
# makes "fully isolated" runs airtight against any tool that reaches for them.
_NIX_COMMANDS = (
    "nix",
    "nix-build",
    "nix-store",
    "nix-instantiate",
    "nix-shell",
    "nix-env",
    "nix-channel",
    "nix-collect-garbage",
    "nix-copy-closure",
    "nix-prefetch-url",
)


def _write_shims(shim_dir: Path, tripwire: Path) -> None:
    """Write a failing, tripwire-logging ``nix`` shim (+ symlinked aliases)."""
    body = "\n".join(
        [
            "#!/bin/sh",
            # Record the invocation (by command name) before failing.
            f'printf "%s\\n" "$(basename "$0") $*" >> "{tripwire}"',
            'echo "run_with_masked_nix: host nix [$(basename "$0")] was invoked; '
            'this run is supposed to be host-nix-free" >&2',
            "exit 127",
            "",
        ]
    )
    real = shim_dir / _NIX_COMMANDS[0]
    real.write_text(body)
    real.chmod(0o755)
    for name in _NIX_COMMANDS[1:]:
        # `$(basename "$0")` resolves to the alias name, so one script suffices.
        (shim_dir / name).symlink_to(real)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(
            "usage: run-with-masked-nix [--] <command> [args...]\n"
            "       run a command with host nix masked (fails + records any nix call)",
            file=sys.stderr,
        )
        sys.exit(2)

    with tempfile.TemporaryDirectory(prefix="fxtr-masked-nix-") as td:
        shim_dir = Path(td)
        tripwire = shim_dir / "tripwire.log"
        tripwire.touch()
        _write_shims(shim_dir, tripwire)

        env = dict(os.environ)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"

        result = subprocess.run(argv, env=env)
        leaked = tripwire.read_text()

    if leaked.strip():
        print(
            "\nrun_with_masked_nix: HOST NIX WAS INVOKED during a masked run "
            "(this command is supposed to be host-nix-free):",
            file=sys.stderr,
        )
        for line in leaked.splitlines():
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
