"""Resolve how to invoke the MDX→FxtrCbor TS subprocess at runtime."""

from __future__ import annotations

import os
import pathlib

#: Env var naming a self-contained report-builder CLI executable (e.g. the
#: nix-built ``lib.fxtr.reportBuilderCli`` wrapper). Generic wiring — whoever
#: runs the builder sets it; nix has no report-builder-specific code.
CLI_ENV_VAR = "FXTR_REPORT_BUILDER_CLI"


class MdxSubprocessError(RuntimeError):
    """The TS subprocess itself misbehaved.

    Distinct from :class:`MdxBuildError` (typed user-level failures
    reported through the response envelope's ``{ok: false, ...}``
    path). This is for infra-level failures: missing dist file,
    non-zero exit, malformed stdout, etc.

    Carries the captured stderr (if any) and exit code for
    debugging.
    """

    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


_THIS_FILE = pathlib.Path(__file__).resolve()
_PACKAGE_ROOT = _THIS_FILE.parent.parent.parent  # src/<pkg>/file -> project root
_DIST_REL = pathlib.Path("ts", "packages", "cli", "dist", "main.cjs")


def resolve_ts_dist_path() -> pathlib.Path:
    """Return the path to the bundled ``main.cjs``.

    Raises :class:`MdxSubprocessError` if the file is missing,
    instructing the caller to run the TS build step.
    """
    candidate = _PACKAGE_ROOT / _DIST_REL
    if not candidate.is_file():
        cli_dir = _PACKAGE_ROOT / "ts" / "packages" / "cli"
        raise MdxSubprocessError(
            f"TS subprocess bundle missing: {candidate}. "
            f"Run `pnpm install --ignore-workspace && pnpm build` in {cli_dir} "
            "to build it (the CLI is a standalone package, NOT in the repo pnpm "
            "workspace — `--ignore-workspace` makes pnpm use its own lockfile). "
            f"Or wire a prebuilt CLI via the {CLI_ENV_VAR} env var."
        )
    return candidate


def resolve_report_cli_command(cli_path: str | pathlib.Path | None = None) -> list[str]:
    """Return the argv used to launch the MDX→FxtrCbor CLI.

    Resolution order:

    1. explicit ``cli_path`` — a **self-contained executable** (e.g. the
       nix-built ``reportBuilderCli`` wrapper, which bakes in ``node`` + the
       bundle) → ``[str(cli_path)]``.
    2. the ``FXTR_REPORT_BUILDER_CLI`` env var (same: a self-contained
       executable) → ``[env]``.
    3. dev fallback — the in-tree ``dist/main.cjs`` run with node →
       ``["node", str(resolve_ts_dist_path())]`` (raises
       :class:`MdxSubprocessError` if the bundle isn't built).

    Cases 1/2 are executables so ``node`` is **not** prepended — the wrapper
    takes the place of ``node`` + the bundle path; only the dev fallback runs the
    raw ``.cjs`` through ``node``. The result is a full argv the caller runs
    verbatim.
    """
    if cli_path is not None:
        return [str(cli_path)]
    env_cli = os.environ.get(CLI_ENV_VAR)
    if env_cli:
        return [env_cli]
    return ["node", str(resolve_ts_dist_path())]
