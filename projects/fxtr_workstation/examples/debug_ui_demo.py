#!/usr/bin/env python3
"""Interactive demo of the workstation + debug UI.

Walks through a small scenario one step at a time, pausing for
user input after each — open the debug UI in a browser and watch
the tables update as you advance.

Run:

    cd projects/fxtr_workstation
    uv run python examples/debug_ui_demo.py

Requires the debug UI to have been built first:

    cd fxtr_workstation/debug_ui
    pnpm install && pnpm build
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

import anyio.to_thread
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.peer_rpc import open_workstation_rpc

# --- Inline fixtures (duplicated from tests/_fixtures.py to keep the
#     example self-contained) -----------------------------------------

_ECHO_NS = uuid.UUID("11111111-2222-3333-4444-555555555555")
ECHO_TYPE_ID = system_entities.define_type(_ECHO_NS, "echo")

_NOTE_NS = uuid.UUID("22222222-3333-4444-5555-666666666666")
NOTE_TYPE_ID = system_entities.define_type(_NOTE_NS, "note")


@service_protocol
class EchoProto(Protocol):
    async def echo(self, msg: str) -> str: ...


class EchoImpl(TypedServiceImpl[EchoProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return EchoProto

    def _service_type_id(self) -> FxtrID:
        return ECHO_TYPE_ID

    @service_method
    async def echo(self, msg: str) -> str:
        return f"echo:{msg}"


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=NOTE_TYPE_ID):
    title: str
    body: str


# --- Prompt helper -------------------------------------------------


async def prompt(message: str) -> None:
    """Print ``message`` and wait for the user to hit enter."""
    print()
    print(f">>> {message}")

    def _wait() -> None:
        try:
            input("    [press enter to continue, ctrl-C to abort] ")
        except EOFError:
            pass

    await anyio.to_thread.run_sync(_wait)


# --- The demo ------------------------------------------------------


async def main() -> None:
    bundled = WorkstationServer.bundled_debug_ui_dist_path()
    if not (bundled / "index.html").is_file():
        print(
            "ERROR: the debug UI hasn't been built yet.\n"
            "Run `cd fxtr_workstation/debug_ui && pnpm install && pnpm build` "
            "and try again."
        )
        return

    async with WorkstationServer(debug_ui_dist_path=bundled) as ws:
        print(f"\nWorkstation server up:")
        print(f"  /rpc:    {ws.rpc_url}")
        print(f"  /client: {ws.client_url}")
        print(f"  /debug:  {ws.debug_http_url}/")

        await prompt(
            "Open the debug UI URL in your browser. The tables should "
            "all be empty. Press enter to continue."
        )

        # --- Connection A ---------------------------------------
        async with open_workstation_rpc(ws.rpc_url, name="alice-rpc") as session_a:
            workstation_a_ref = session_a.remote_service
            workstation_a = await workstation_a_ref.resolve(session_a.scope)
            await workstation_a.name_connection("alice")

            await prompt(
                "Connection A is up and named 'alice'. The "
                "RPC connections table should show one row."
            )

            echo_ref = await session_a.scope.mount(EchoImpl())
            note = Note(title="hello", body="world").store(session_a.scope)

            async with (
                expose_service(
                    workstation_a_ref, "echo-svc", echo_ref, session_a.scope
                ) as svc_short_id,
                expose_entity(
                    workstation_a_ref, "demo-note", note.entity_id, session_a.scope
                ) as ent_short_id,
            ):
                print(f"\n  alice exposed:")
                print(f"    service short_id: {svc_short_id}  (name=echo-svc)")
                print(f"    entity  short_id: {ent_short_id}  (name=demo-note)")
                await prompt(
                    "Alice has exposed an EchoProto service and a Note "
                    "entity. Both should appear in the corresponding tables, "
                    "with 'alice' shown in their 'registered by' column."
                )

                # --- Connection B ---------------------------
                async with open_workstation_rpc(ws.rpc_url, name="bob-rpc") as session_b:
                    workstation_b = await session_b.remote_service.resolve(session_b.scope)
                    await workstation_b.name_connection("bob")

                    await prompt(
                        "Connection B is up and named 'bob'. The "
                        "RPC connections table should show two rows now."
                    )

                    echo_b_ref = await workstation_b.get_service(svc_short_id)
                    echo_b = await echo_b_ref.replace_service_type(EchoProto).resolve(
                        session_b.scope
                    )
                    result = await echo_b.echo("hi from bob")
                    print(f"\n  bob called echo: {result!r}")

                    note_id = await workstation_b.get_entity(ent_short_id)
                    print(f"  bob retrieved entity FxtrID: {note_id}")

                    await prompt(
                        "Bob retrieved + called the service, and "
                        "retrieved the entity FxtrID. (Tables don't "
                        "change visibly here — get_* doesn't mutate "
                        "registrations.)"
                    )

                await prompt(
                    "Bob's connection just closed. The RPC connections "
                    "table should drop back to one row. The service / "
                    "entity tables still show alice's registrations."
                )
            # expose_service / expose_entity contexts exited
            await prompt(
                "Alice released both her registrations. Both the services "
                "and entities tables should now be empty."
            )

        await prompt(
            "Alice's connection just closed. RPC connections table should "
            "now be empty. Server shutting down on next enter."
        )

    print("\nServer shut down. Bye.")


if __name__ == "__main__":
    try:
        anyio.run(main)
    except KeyboardInterrupt:
        print("\nAborted.")
