"""``python -m fxtr_nix_runner <command>`` (also installed as ``fxtr_nix``).

Commands:
    lock [PATH]          Create/refresh PATH's flake.lock via the configured
                         backend (PATH defaults to the cwd). Lets a flake repo
                         lock without host nix — e.g. from a pre-commit hook.
    update-hashes [PATH] (Re)compute PATH's fod_lock.toml: builds newly-declared
                         `fxtr.preloads` slots and records their fixed-output
                         hashes (and drops slots no longer declared). `--all`
                         re-locks every slot, `-k/--key KEY` a chosen subset,
                         `--dry-run` reports the plan without building or writing.
    serve [PATH]         Build PATH's `fxtr.entities.*` + `fxtr.namespaces.*` (+ a
                         `fxtr.uiPresets` renderer dispatcher, if any)
                         and serve them over HTTP (provenance exposed) until
                         SIGINT/SIGTERM. `--port` to bind, `--rev` to read a
                         committed rev (default: live workdir), `--preset` to pick
                         the renderer preset, `--web-dist DIR` to also serve a
                         built static SPA (the viewer) at / on the same origin.
    clear                Remove fxtr-nix builder/deliverer containers + volumes
                         + scratch.
    runtime setup        Detect → explain → write ~/.config/fxtr/nix_runtime.toml
                         (`--detect` prints findings as JSON and stops).
    runtime init-podman  Automate `podman machine init` (with the Rosetta
                         choice); run this first if `setup`'s nudge says podman
                         is missing/uncreated, then re-run `runtime setup`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio
from fxtr_nix_runner import _clear, _wizard, resolve_backend


def _lock(path: Path) -> None:
    backend = resolve_backend()

    async def run() -> None:
        async with backend.builder_for(path) as handle:
            await handle.lock()

    anyio.run(run)
    print(f"locked {path}")


def _update_hashes(path: Path, *, all_: bool, keys: list[str], dry_run: bool) -> None:
    # Lazy import: the engine pulls in fxtr2 (manager tier); keep the other
    # subcommands fxtr2-free.
    from fxtr_nix_runner.update_hashes import Mode, update_hashes

    mode = Mode.ALL if all_ else Mode.KEYS if keys else Mode.DEFAULT
    plan = anyio.run(lambda: update_hashes(path, mode=mode, keys=keys, dry_run=dry_run))

    verb = "would relock" if dry_run else "relocked"
    for key in plan.relock:
        print(f"  {verb} {key} ({plan.reasons.get(key, '')})")
    for key in plan.drop:
        print(f"  {'would drop' if dry_run else 'dropped'} {key} (no longer in fxtr.preloads)")
    if not plan.changed:
        print(f"fod_lock.toml is up to date ({len(plan.unchanged)} slot(s))")
    if dry_run and plan.changed:
        # Non-zero so CI can assert the lock is current.
        sys.exit(1)


def _serve(
    path: Path,
    *,
    port: int,
    rev: str | None,
    host: str,
    preset: str | None,
    web_dist: Path | None,
) -> None:
    # Lazy import: the serving module pulls in fxtr2 + the http substrate (manager
    # tier); keep the other subcommands fxtr2-free.
    from fxtr_nix_runner.serve import run_published_server

    anyio.run(
        lambda: run_published_server(
            path, port=port, rev=rev, host=host, preset=preset, web_dist=web_dist
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="fxtr_nix", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    lock_p = sub.add_parser("lock", help="create/refresh a flake's flake.lock")
    lock_p.add_argument("path", nargs="?", default=".", help="flake repo path (default: cwd)")

    uh_p = sub.add_parser("update-hashes", help="(re)compute a flake's fod_lock.toml")
    uh_p.add_argument("path", nargs="?", default=".", help="flake repo path (default: cwd)")
    uh_p.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan; build/write nothing (exit 1 if stale)",
    )
    uh_target = uh_p.add_mutually_exclusive_group()
    uh_target.add_argument(
        "--all", action="store_true", help="re-lock every fxtr.preloads slot, not just new ones"
    )
    uh_target.add_argument(
        "-k",
        "--key",
        dest="keys",
        action="append",
        default=[],
        metavar="KEY",
        help="relock only this slot (repeatable)",
    )

    serve_p = sub.add_parser("serve", help="serve a flake's entities + namespaces over HTTP")
    serve_p.add_argument("path", nargs="?", default=".", help="flake repo path (default: cwd)")
    serve_p.add_argument("--port", type=int, default=8080, help="port to bind (default: 8080)")
    serve_p.add_argument(
        "--host", default="127.0.0.1", help="interface to bind (default: loopback)"
    )
    serve_p.add_argument(
        "--rev", default=None, help="committed rev to read (default: live workdir)"
    )
    serve_p.add_argument(
        "--preset",
        default=None,
        help="renderer preset to serve (default: the sole/`default` preset, if any)",
    )
    serve_p.add_argument(
        "--web-dist",
        default=None,
        type=Path,
        help="directory of a built static SPA to serve at / (e.g. @fxtr-ui/published-app's dist)",
    )

    sub.add_parser("clear", help="remove fxtr-nix containers + volumes + scratch")

    runtime_p = sub.add_parser("runtime", help="set up the nix backend + container runtime")
    runtime_sub = runtime_p.add_subparsers(dest="runtime_command", required=True)
    setup_p = runtime_sub.add_parser("setup", help="detect + write nix_runtime.toml")
    setup_p.add_argument(
        "--detect", action="store_true", help="print detection findings as JSON and stop"
    )
    init_p = runtime_sub.add_parser("init-podman", help="automate `podman machine init`")
    init_p.add_argument(
        "--no-rosetta",
        dest="rosetta",
        action="store_false",
        help="skip enabling Rosetta (default: enable it for near-native amd64 builds)",
    )

    args = parser.parse_args()

    if args.command == "lock":
        _lock(Path(args.path).resolve())
    elif args.command == "update-hashes":
        _update_hashes(
            Path(args.path).resolve(), all_=args.all, keys=args.keys, dry_run=args.dry_run
        )
    elif args.command == "serve":
        _serve(
            Path(args.path).resolve(),
            port=args.port,
            rev=args.rev,
            host=args.host,
            preset=args.preset,
            web_dist=args.web_dist,
        )
    elif args.command == "clear":
        _clear.main()
    elif args.command == "runtime":
        if args.runtime_command == "setup":
            sys.exit(anyio.run(lambda: _wizard.run_setup(detect_only=args.detect)))
        elif args.runtime_command == "init-podman":
            sys.exit(anyio.run(lambda: _wizard.run_init_podman(rosetta=args.rosetta)))


if __name__ == "__main__":
    main()
