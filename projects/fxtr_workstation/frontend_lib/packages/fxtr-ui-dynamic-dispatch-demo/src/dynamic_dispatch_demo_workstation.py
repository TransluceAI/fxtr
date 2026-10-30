"""Backend for `@fxtr-ui/dynamic-dispatch-demo`.

Boots a :class:`WorkstationServer`, registers two entity types
(notes + photos), mounts a :class:`BasicDynamicDispatchImpl` as an
external service named ``renderer-dispatcher`` via the
:func:`mount_basic_dynamic_dispatch` CM, runs an mtime-based
hot-reload poller, and prints the type-id strings + the dispatcher
service's external name to stdout so the TS orchestrator can plumb
them through Vite.

The renderer source TSX files live in the demo's source tree under
``src/renderers/<type>/index.tsx`` (template name, not the actual
type-id-string). At startup this script materialises a temporary
renderer-dir tree where each subdirectory is named after the
content-addressed type-id-string of the matching entity type, then
hands that tree to ``mount_basic_dynamic_dispatch``. The impl walks
the tree at construction time, esbuild-compiles each subdirectory's
``index.tsx``, and creates one :class:`RendererEntity` per type.

The mtime poller runs every ``--reload-interval-seconds`` (default
15.0; settable via CLI flag or the
``FXTR_DEMO_RELOAD_INTERVAL_SECONDS`` env var). It calls
:meth:`BasicDynamicDispatchImpl.recompile_changed_and_reload`,
which is a no-op when nothing changed and broadcasts a reload
notification to subscribed frontends when an ``index.tsx``'s mtime
advances.

stdout protocol:

    TYPE_IDS {...json...}
    RENDERER_DISPATCHER_NAME renderer-dispatcher
    READY ws://...
    DEBUG_UI http://...

The TYPE_IDS line is a JSON dict keyed by an externally-stable
label (``note``, ``photo``) → the type entity's id-string. The TS
side uses these strings as the dispatcher cache keys.

``RENDERER_DISPATCHER_NAME`` is the external name the workstation
registers the impl under. The TS side resolves the service via
``workstation.get_service_by_external_name(...)``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any  # noqa: F401  (kept for future renderer-data fields)

import anyio
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr_workstation import ExternalServiceConfig, WorkstationServer
from fxtr_workstation.dynamic_dispatch import BasicDynamicDispatchImpl, mount_basic_dynamic_dispatch

_log = logging.getLogger(__name__)

# Stable namespace so the type-id strings are deterministic
# across runs — useful for debugging and for the TS side that
# treats them as opaque cache keys.
_DEMO_NS = uuid.UUID("8f3a2c7e-1b4d-4e2a-9c5f-d6e7a8b9c0d2")

_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "fxtr_ui_dynamic_dispatch_demo_note")
_PHOTO_TYPE_ID: FxtrID = system_entities.define_type(
    _DEMO_NS, "fxtr_ui_dynamic_dispatch_demo_photo"
)


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """Demo note entity. The TS dispatcher uses `$fxtr.type` to
    look up the matching renderer."""

    title: str
    body: str


@dataclasses.dataclass(frozen=True)
class _Photo(DataclassEntity, fxtr_type=_PHOTO_TYPE_ID):
    caption: str
    url: str


# The external name the dispatcher service is registered under;
# the TS client uses this string to look up the ref via
# `workstation.get_service_by_external_name`.
_RENDERER_DISPATCHER_NAME = "renderer-dispatcher"

# Convention: each entity type's renderer template lives at
# `src/renderers/<label>/index.tsx`. At startup we stage them
# into a temp directory keyed by the matching type-id-string.
_RENDERER_TEMPLATES: dict[str, FxtrID] = {
    "note": _NOTE_TYPE_ID,
    "photo": _PHOTO_TYPE_ID,
}

# Default mtime-poll interval in seconds. Override via CLI
# `--reload-interval-seconds` or env `FXTR_DEMO_RELOAD_INTERVAL_SECONDS`.
_DEFAULT_RELOAD_INTERVAL_S = 15.0


def _stage_renderer_tree(templates_root: Path, dest_root: Path) -> None:
    """Copy each `<templates_root>/<label>/` into
    `<dest_root>/<type_id_string>/`. The `BasicDynamicDispatchImpl`
    walks `dest_root` and treats each subdirectory name as a
    type-id-string bucket.

    Files inside each subdir are copied with `shutil.copy2` so the
    source mtime is preserved — without it the freshly-staged copy
    would always have a "fresh" mtime, defeating the mtime-based
    reload-on-change detection.
    """
    for label, type_id in _RENDERER_TEMPLATES.items():
        src = templates_root / label
        dst = dest_root / type_id.id_string()
        if not src.is_dir():
            raise RuntimeError(f"renderer template missing: {src}")
        shutil.copytree(src, dst, copy_function=shutil.copy2)


def _resolve_reload_interval(cli_value: float | None) -> float:
    """CLI takes priority over env var; env var falls back to the
    module-level default."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get("FXTR_DEMO_RELOAD_INTERVAL_SECONDS")
    if env is None or env == "":
        return _DEFAULT_RELOAD_INTERVAL_S
    try:
        return float(env)
    except ValueError:
        _log.warning(
            "FXTR_DEMO_RELOAD_INTERVAL_SECONDS=%r is not a float; falling back to %s",
            env,
            _DEFAULT_RELOAD_INTERVAL_S,
        )
        return _DEFAULT_RELOAD_INTERVAL_S


async def _reload_poller(impl: BasicDynamicDispatchImpl, interval_seconds: float) -> None:
    """Background task: every ``interval_seconds``, ask the impl
    to recompile any renderer whose ``index.tsx`` mtime has
    advanced. The impl broadcasts a reload notification on change,
    no-ops otherwise.

    A negative or zero interval disables polling (useful for tests
    or for headless runs).
    """
    if interval_seconds <= 0:
        _log.info("reload poller disabled (interval=%s)", interval_seconds)
        return
    _log.info("reload poller starting (interval=%.1fs)", interval_seconds)
    while True:
        await anyio.sleep(interval_seconds)
        try:
            changed = await impl.recompile_changed_and_reload()
            if changed:
                _log.info("dynamic dispatch: renderer changes detected; broadcast reload")
        except Exception:
            _log.exception("reload poller iteration failed; continuing")


async def _amain(reload_interval_seconds: float) -> None:
    templates_root = Path(__file__).parent / "renderers"
    type_ids = {
        "note": _NOTE_TYPE_ID.id_string(),
        "photo": _PHOTO_TYPE_ID.id_string(),
    }

    with tempfile.TemporaryDirectory(prefix="fxtr-ui-dyn-dispatch-renderers-") as tmp_str:
        renderer_root = Path(tmp_str) / "renderers"
        renderer_root.mkdir()
        _stage_renderer_tree(templates_root, renderer_root)
        _log.info("Using renderer root: %s", renderer_root)

        # mount_basic_dynamic_dispatch owns the impl scope + task
        # group + compile_and_register + service ref. We then
        # bridge the ref into the workstation via
        # ExternalServiceConfig.
        async with mount_basic_dynamic_dispatch(
            renderer_dirs=[renderer_root],
            scope_name="dynamic_dispatch_demo_owner",
        ) as mounted:
            cfg = ExternalServiceConfig(
                external_name=_RENDERER_DISPATCHER_NAME,
                service=mounted.service_ref,
                scope=mounted.scope,
            )

            async with WorkstationServer(
                debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
                external_services=[cfg],
            ) as ws:
                # Seed a couple of entities under stable external
                # names so the TS demo grid has something to
                # dispatch on. We expose them via the workstation
                # main service through a peer-RPC connection — same
                # mechanism the sibling dispatch-demo uses.
                async with _seed_demo_entities(ws.rpc_url):
                    # Outer task group ties the lifetime of the
                    # mtime poller to the signal-receiver. Either
                    # finishing cancels the other.
                    async with anyio.create_task_group() as outer_tg:

                        async def _await_signal() -> None:
                            with anyio.open_signal_receiver(
                                signal.SIGTERM, signal.SIGINT
                            ) as signals:
                                async for _ in signals:
                                    break
                            outer_tg.cancel_scope.cancel()

                        outer_tg.start_soon(_reload_poller, mounted.impl, reload_interval_seconds)
                        outer_tg.start_soon(_await_signal)

                        print(f"TYPE_IDS {json.dumps(type_ids)}", flush=True)
                        print(
                            f"RENDERER_DISPATCHER_NAME {_RENDERER_DISPATCHER_NAME}",
                            flush=True,
                        )
                        print(f"READY {ws.bound_url}", flush=True)
                        print(f"DEBUG_UI {ws.debug_http_url}", flush=True)


@contextlib.asynccontextmanager
async def _seed_demo_entities(rpc_url: str):
    """Open a peer-RPC connection to the workstation and expose
    a few demo entities under stable external names. Stays
    connected for the lifetime of the workstation so the
    registrations don't drop.
    """
    from fxtr_workstation import expose_entity
    from fxtr_workstation.peer_rpc import open_workstation_rpc

    async with open_workstation_rpc(
        rpc_url, name="fxtr-ui-dynamic-dispatch-demo/peer-rpc"
    ) as session:
        entities = [
            (
                "demo-note-1",
                _Note(title="Hello", body="from the dynamic-dispatch demo"),
            ),
            (
                "demo-note-2",
                _Note(
                    title="A second note",
                    body="that loads independently",
                ),
            ),
            (
                "demo-photo-1",
                _Photo(
                    caption="Placeholder pic",
                    url="https://placekitten.com/400/300",
                ),
            ),
            (
                "demo-photo-2",
                _Photo(
                    caption="Another placeholder",
                    url="https://placekitten.com/300/300",
                ),
            ),
        ]
        refs = [(name, e.store(session.scope)) for (name, e) in entities]
        async with contextlib.AsyncExitStack() as stack:
            for name, ref in refs:
                await stack.enter_async_context(
                    expose_entity(
                        session.remote_service,
                        name,
                        ref.entity_id,
                        session.scope,
                    )
                )
            yield


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backend for the @fxtr-ui/dynamic-dispatch-demo TS package."
    )
    parser.add_argument(
        "--reload-interval-seconds",
        type=float,
        default=None,
        help=(
            "Seconds between mtime checks for renderer hot-reload. "
            "Defaults to the FXTR_DEMO_RELOAD_INTERVAL_SECONDS env var, "
            f"or {_DEFAULT_RELOAD_INTERVAL_S} if unset. Use 0 (or negative) to disable polling."
        ),
    )
    args = parser.parse_args()
    interval = _resolve_reload_interval(args.reload_interval_seconds)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        anyio.run(_amain, interval)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
