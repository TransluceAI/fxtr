"""Test-harness workstation backend for the dynamic-dispatcher's
hot-reload integration tests.

Boots a :class:`WorkstationServer`, mounts
:class:`BasicDynamicDispatchImpl` as the ``renderer-dispatcher``
external service against a fixed renderer-template tree, seeds one
demo entity per type, and reads commands from ``stdin`` so the
caller (a TS test) can deterministically trigger reload notifications.

**No mtime polling.** This script is deliberately separate from the
demo backend (``dynamic_dispatch_demo_workstation.py``), which runs
the mtime-based polling loop for human iteration. Tests don't want
to rely on timing; they send ``TRIGGER_RELOAD`` over stdin instead.

**stdin protocol** — each command terminated by ``\\n``:

  ``TRIGGER_RELOAD`` → calls ``impl.trigger_reload()`` then writes
                       ``OK TRIGGER_RELOAD\\n`` to stdout.

**stdout protocol**:

  ``READY <ws-url>``                 — workstation listening.
  ``RENDERER_DISPATCHER_NAME <name>``— external name of the dispatcher service.
  ``TYPE_IDS {json}``                — type-id-string map (note/photo).
  ``ENTITY_NAMES {json}``            — externally-exposed entity names.
  ``OK <command>``                   — reply to a stdin command.

Closes on SIGTERM.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import signal
import sys
import tempfile
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr_workstation import ExternalServiceConfig, WorkstationServer, expose_entity
from fxtr_workstation.dynamic_dispatch import BasicDynamicDispatchImpl, WorkstationDispatcherProto
from fxtr_workstation.protocols import WorkstationServiceProto

# Stable namespace for deterministic type-id strings across runs.
_HARNESS_NS = uuid.UUID("9c8f4d3a-7b2e-4a1d-9e3c-5f6a7b8c9d0e")

_NOTE_TYPE_ID: FxtrID = system_entities.define_type(_HARNESS_NS, "fxtr_ui_reload_harness_note")
_PHOTO_TYPE_ID: FxtrID = system_entities.define_type(_HARNESS_NS, "fxtr_ui_reload_harness_photo")

_RENDERER_DISPATCHER_NAME = "renderer-dispatcher"


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    title: str


@dataclasses.dataclass(frozen=True)
class _Photo(DataclassEntity, fxtr_type=_PHOTO_TYPE_ID):
    caption: str


# Each renderer template directory contains a minimal `index.tsx`
# whose body references the type label so test code can tell which
# renderer's bytes arrived (the integration test matches against
# "NoteCard" / "PhotoCard" substrings).
_NOTE_TEMPLATE = """\
// reload harness — note renderer
const CARD_KEY_UUID = "00000000-0000-4000-8000-000000020001";
function NoteCard() { return null; }
export default { default: { [CARD_KEY_UUID]: NoteCard } };
"""

_PHOTO_TEMPLATE = """\
// reload harness — photo renderer
const CARD_KEY_UUID = "00000000-0000-4000-8000-000000020001";
function PhotoCard() { return null; }
export default { default: { [CARD_KEY_UUID]: PhotoCard } };
"""

_RENDERER_TEMPLATES: dict[str, tuple[FxtrID, str]] = {
    "note": (_NOTE_TYPE_ID, _NOTE_TEMPLATE),
    "photo": (_PHOTO_TYPE_ID, _PHOTO_TEMPLATE),
}


def _stage_renderer_tree(dest_root: Path) -> None:
    """Stage one renderer subdirectory per renderer template under
    ``dest_root/<type_id_string>/index.tsx``.
    """
    for type_id, template in _RENDERER_TEMPLATES.values():
        subdir = dest_root / type_id.id_string()
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "index.tsx").write_text(template)


async def _stdin_command_loop(
    impl: BasicDynamicDispatchImpl,
    *,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Read commands from stdin (one per line). On ``TRIGGER_RELOAD``,
    call :meth:`BasicDynamicDispatchImpl.trigger_reload` and echo
    ``OK TRIGGER_RELOAD`` to stdout.

    Unknown commands write ``ERR <line>`` to stdout but don't stop
    the loop. EOF terminates.
    """
    task_status.started()
    stdin = sys.stdin
    while True:
        line = await anyio.to_thread.run_sync(stdin.readline)
        if line == "":  # EOF
            return
        command = line.strip()
        if command == "":
            continue
        if command == "TRIGGER_RELOAD":
            impl.trigger_reload()
            print("OK TRIGGER_RELOAD", flush=True)
        else:
            print(f"ERR unknown command: {command!r}", flush=True)


@contextlib.asynccontextmanager
async def _seeded_entities(rpc_url: str) -> AsyncGenerator[dict[str, str]]:
    """Expose one Note + one Photo under stable external names so
    the TS test can dispatch them. Returns the external-name map.

    Connects a peer-RPC to the workstation, stores each entity in
    the peer's scope (so the FxtrID is reachable when
    ``copy_entity`` lifts it), and registers via
    :func:`expose_entity` for the lifetime of the workstation.
    """
    import httpx_ws
    from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
    from fxtr2.rpc.scope_bridge import open_rpc_scope
    from fxtr_sandbox_types.json_object_stream import JSONObjectStream

    async def _make_stream(
        ws: httpx_ws.AsyncWebSocketSession,
    ) -> anyio.abc.ObjectStream[JSONTypes]:
        async def send(item: JSONTypes) -> None:
            try:
                await ws.send_text(json.dumps(item))
            except httpx_ws.HTTPXWSException as e:
                raise anyio.BrokenResourceError from e

        async def receive() -> JSONTypes:
            try:
                text = await ws.receive_text()
            except httpx_ws.HTTPXWSException:
                raise anyio.EndOfStream from None
            return json.loads(text)

        return JSONObjectStream(send, receive)

    async with httpx_ws.aconnect_ws(rpc_url) as ws:
        stream = await _make_stream(ws)
        try:
            transport = wrap_json_stream(stream)
            async with open_rpc_scope(
                transport,
                our_main_service=None,
                remote_service_type=WorkstationServiceProto,
                name="reload-harness/peer-rpc",
            ) as peer:
                # Store entities into the peer-rpc's scope — that's
                # what `copy_entity` (called by `expose_entity`)
                # lifts from. Storing them into the harness's
                # user_scope wouldn't be reachable across the wire.
                note_ref = _Note(title="reload-harness-note").store(peer.scope)
                photo_ref = _Photo(caption="reload-harness-photo").store(peer.scope)

                async with contextlib.AsyncExitStack() as stack:
                    await stack.enter_async_context(
                        expose_entity(
                            peer.remote_service, "harness-note", note_ref.entity_id, peer.scope
                        )
                    )
                    await stack.enter_async_context(
                        expose_entity(
                            peer.remote_service, "harness-photo", photo_ref.entity_id, peer.scope
                        )
                    )
                    yield {"harness-note": "note", "harness-photo": "photo"}
        finally:
            await stream.aclose()


async def _amain() -> None:
    type_ids: dict[str, str] = {
        "note": _NOTE_TYPE_ID.id_string(),
        "photo": _PHOTO_TYPE_ID.id_string(),
    }

    with tempfile.TemporaryDirectory(prefix="fxtr-ui-reload-harness-renderers-") as tmp_str:
        renderer_root = Path(tmp_str) / "renderers"
        renderer_root.mkdir()
        _stage_renderer_tree(renderer_root)

        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="reload_harness_owner",
        ) as user_scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=user_scope,
                    renderer_dirs=[renderer_root],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await user_scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                cfg = ExternalServiceConfig(
                    external_name=_RENDERER_DISPATCHER_NAME,
                    service=dispatcher_ref,
                    scope=user_scope,
                )

                async with WorkstationServer(external_services=[cfg]) as ws:
                    async with _seeded_entities(ws.rpc_url) as entity_names:
                        # Emit the protocol lines BEFORE the stdin
                        # loop starts so the TS harness sees them
                        # promptly.
                        print(f"TYPE_IDS {json.dumps(type_ids)}", flush=True)
                        print(
                            f"RENDERER_DISPATCHER_NAME {_RENDERER_DISPATCHER_NAME}",
                            flush=True,
                        )
                        print(f"ENTITY_NAMES {json.dumps(entity_names)}", flush=True)
                        print(f"READY {ws.bound_url}", flush=True)

                        # Run the stdin command loop concurrently
                        # with the signal-receiver park. Either an
                        # EOF on stdin OR a SIGTERM/SIGINT tears
                        # down the workstation.
                        async with anyio.create_task_group() as outer_tg:

                            async def _await_signal() -> None:
                                with anyio.open_signal_receiver(
                                    signal.SIGTERM, signal.SIGINT
                                ) as signals:
                                    async for _ in signals:
                                        break
                                outer_tg.cancel_scope.cancel()

                            async def _await_stdin() -> None:
                                await _stdin_command_loop(impl)
                                outer_tg.cancel_scope.cancel()

                            outer_tg.start_soon(_await_signal)
                            outer_tg.start_soon(_await_stdin)

                # Shutdown — cancel listener workers + drain impl_tg
                # before user_scope exits.
                impl_tg.cancel_scope.cancel()

    # Suppress unused-warning on `Any` import.
    _ = Any


def main() -> None:
    try:
        anyio.run(_amain)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
