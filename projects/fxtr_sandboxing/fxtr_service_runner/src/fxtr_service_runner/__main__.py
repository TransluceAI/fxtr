"""Entry point for ``python -m fxtr_service_runner``.

Thin shim. Connects to the host's per-spawn UDS at ``SOCKET_PATH``,
builds a `ContainerServiceContext` over the resulting
`anyio.SocketStream`, and hands off to
`fxtr_sandbox_rpc_adapter.main(ctx, entry_point=...)`.

Entry-point resolution: prefers a CLI argument
(``python -m fxtr_service_runner my_pkg:run``) when given, otherwise
discovers a single ``fxtr.service`` entry point from installed packages.
The CLI form is what ``cmd_override`` in ``fxtr_container_task.toml``
should use when an image bakes in multiple services and the operator
needs to pin one explicitly.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points

import anyio
import fxtr_sandbox_rpc_adapter

from .service_context import SOCKET_PATH, ContainerServiceContext


def _discover_entry_point() -> str:
    """Find the single ``fxtr.service`` entry point in installed packages.

    Returns the entry point value as ``"module:function"``. Raises
    ``RuntimeError`` if zero or more than one is registered — both shapes
    are operator errors at image-build time.
    """
    eps = entry_points(group="fxtr.service")
    ep_list = list(eps)
    if len(ep_list) == 0:
        raise RuntimeError(
            "No fxtr.service entry point found in installed packages. "
            'Make sure your service package has [project.entry-points."fxtr.service"] '
            "in its pyproject.toml and is installed."
        )
    if len(ep_list) > 1:
        names = [ep.name for ep in ep_list]
        raise RuntimeError(
            f"Multiple fxtr.service entry points found: {names}. "
            "Pass one explicitly as a CLI argument, e.g. "
            "`python -m fxtr_service_runner pkg.module:function`."
        )
    return f"{ep_list[0].module}:{ep_list[0].attr}"


def _resolve_entry_point(cli_arg: str | None) -> str:
    if cli_arg is not None:
        return cli_arg
    return _discover_entry_point()


async def _run(entry_point: str) -> None:
    """Async body: open the host UDS, build the context, run the adapter."""
    socket_stream = await anyio.connect_unix(SOCKET_PATH)
    try:
        ctx = ContainerServiceContext(socket_stream)
        await fxtr_sandbox_rpc_adapter.main(ctx, entry_point=entry_point)
    finally:
        # send_eof / aclose may already have run inside the adapter via the
        # RPC drain; aclose is idempotent for anyio sockets.
        await socket_stream.aclose()


def main() -> None:
    """Sync CLI entry point — invoked as ``python -m fxtr_service_runner``."""
    parser = argparse.ArgumentParser(prog="fxtr_service_runner")
    parser.add_argument(
        "entry_point",
        nargs="?",
        default=None,
        help=(
            "Optional 'module:function' to run. If omitted, discovered from the "
            "single fxtr.service entry point in installed packages."
        ),
    )
    args = parser.parse_args()

    try:
        entry_point = _resolve_entry_point(args.entry_point)
    except RuntimeError as e:
        print(f"fxtr_service_runner error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        anyio.run(_run, entry_point, backend="asyncio")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"fxtr_service_runner error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
