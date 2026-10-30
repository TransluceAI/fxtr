"""End-to-end tests for stage-9 typed-service dispatch.

Each test boots a real :class:`WorkstationServer`, registers a
typed service via the peer-RPC path, and exercises the
sandbox-side typed builtins end-to-end:

- :func:`get_service` / :func:`lookup_service_by_name` with an
  explicit ``@service_protocol`` annotation.
- :func:`schedule_invoke` typed-dispatch path
  (``_typed_encode_invoke_args``): args walk through the dialect
  (each ``EntityRef[T]`` → ``FxtrID``, each ``ServiceRef[T]`` →
  ``ServiceRef[UnknownService]``); kwargs ride alongside.
- :func:`retrieve_result` with both
  ``result_type_of(Proto, Proto.method)`` and a raw annotation.
- The owned-callback ``ServiceRef[WorkstationSessionEventInbox]`` passed to
  a typed arg slot.

The tests intentionally cover both happy paths and the two
documented error edges (non-``@service_protocol`` target_type,
unknown method name).
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any, Protocol

import anyio
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_service

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    connect,
    open_session,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx

from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


# --- Typed test fixtures (peer-mounted on the workstation) ----------


_FIXTURE_NS = uuid.UUID("99887766-5544-3322-1100-aabbccddeeff")
_NOTE_TYPE_ID = system_entities.define_type(_FIXTURE_NS, "typed_note")
_NOTE_PROTO_TYPE_ID = system_entities.define_type(_FIXTURE_NS, "typed_note_proto")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):
    """Tiny test entity. Used to exercise the dialect's
    ``EntityRef[T] ↔ FxtrID`` strip/re-attach via a typed method."""

    title: str
    body: str


@service_protocol
class _NoteEchoProto(Protocol):
    """Typed echo service for stage-9 e2e:

    - ``echo_note`` takes / returns an ``EntityRef[_Note]`` — the
      dialect strips to ``FxtrID`` on encode and re-attaches on
      decode. Exercises the full typed args + typed return path.
    - ``echo_kw`` takes keyword-only args — exercises the typed
      kwargs path (requires the kwargs commit).
    - ``echo_default`` has a kwarg with a Protocol-side default —
      exercises the ``USE_REMOTE_DEFAULT`` round-trip.
    """

    async def echo_note(self, note: EntityRef[_Note]) -> EntityRef[_Note]: ...

    async def echo_kw(self, *, msg: str, prefix: str = "kw") -> str: ...

    async def echo_default(self, base: int, *, factor: int = 10) -> int: ...


class _NoteEchoImpl(TypedServiceImpl[_NoteEchoProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _NoteEchoProto

    def _service_type_id(self) -> FxtrID:
        return _NOTE_PROTO_TYPE_ID

    @service_method
    async def echo_note(self, note: EntityRef[_Note]) -> EntityRef[_Note]:
        # Load, mutate the title to confirm round-trip semantics, re-store.
        loaded = await note.load()
        return _Note(title=f"echoed:{loaded.title}", body=loaded.body).store()

    @service_method
    async def echo_kw(self, *, msg: str, prefix: str = "kw") -> str:
        return f"{prefix}:{msg}"

    @service_method
    async def echo_default(self, base: int, *, factor: int = 10) -> int:
        return base * factor


# --- Fake MCP-context helpers (same shape as test_invoke_e2e) -------


class _FakeWriteStream:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _FakeSession:
    def __init__(self) -> None:
        self._write_stream = _FakeWriteStream()


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
        self.session = _FakeSession()


async def _swd(ctx: _FakeContext, p: str) -> str:
    return await set_working_dir(p, ctx)  # type: ignore[arg-type]


async def _conn(ctx: _FakeContext) -> str:
    return await connect(ctx)  # type: ignore[arg-type]


async def _open(ctx: _FakeContext, md: dict[str, Any] | None = None) -> str:
    return await open_session(ctx, md)  # type: ignore[arg-type]


async def _run(ctx: _FakeContext, src: str) -> str:
    return await run_script(src, ctx)  # type: ignore[arg-type]


#: Source the sandbox scripts prepend to redeclare ``_Note`` and
#: ``_NoteEchoProto`` inside the sandbox. The fxtr_type / method
#: signatures match the host-side classes byte-for-byte so the wire
#: round-trip works. Class identity is irrelevant — the dialect
#: strips ``target_type`` to ``UnknownService`` on encode, and the
#: workstation impl's own (host-side) class drives the decode.
_SANDBOX_FIXTURES_SRC = (
    "import dataclasses\n"
    "import uuid\n"
    "from typing import Protocol\n"
    "from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity\n"
    "from fxtr2.data import system_entities\n"
    "from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef\n"
    "from fxtr2.services.typed_service import service_protocol\n"
    "\n"
    f"_FIXTURE_NS = uuid.UUID({str(_FIXTURE_NS)!r})\n"
    "_NOTE_TYPE_ID = system_entities.define_type(_FIXTURE_NS, 'typed_note')\n"
    "\n"
    "@dataclasses.dataclass(frozen=True)\n"
    "class _Note(DataclassEntity, fxtr_type=_NOTE_TYPE_ID):\n"
    "    title: str\n"
    "    body: str\n"
    "\n"
    "@service_protocol\n"
    "class _NoteEchoProto(Protocol):\n"
    "    async def echo_note(self, note: EntityRef[_Note]) -> EntityRef[_Note]: ...\n"
    "    async def echo_kw(self, *, msg: str, prefix: str = 'kw') -> str: ...\n"
    "    async def echo_default(self, base: int, *, factor: int = 10) -> int: ...\n"
    "\n"
)


def _write_workdir(tmp_path: Path, ws_url: str) -> None:
    (tmp_path / CONFIG_FILENAME).write_text(
        f'workstation_url = "{ws_url}"\n' + "agent_mcp_store = false\n"
    )
    (tmp_path / "preamble.py").write_text("# stage-9 typed-service e2e\n")
    (tmp_path / "packages").mkdir(exist_ok=True)


async def _wait_for_slot_promotion(
    app_ctx: AppContext,
    slot_key: str,
    *,
    poll_interval: float = 0.05,
    max_polls: int = 50,
) -> Any:
    """Poll ``mirror.data_by_key`` until ``slot_key`` is promoted past
    ``invoke_pending``. Returns the final value or raises
    ``AssertionError`` on timeout. SIGALRM in conftest bounds the
    overall test wall-clock."""
    assert app_ctx.connection is not None
    conn = app_ctx.connection
    for _ in range(max_polls):
        v = conn._mirror.data_by_key.get(slot_key)
        if v is not None and v[0] != "invoke_pending":
            return v
        await anyio.sleep(poll_interval)
    raise AssertionError(
        f"timed out waiting for slot {slot_key!r} to promote; "
        f"current value: {conn._mirror.data_by_key.get(slot_key)!r}"
    )


# --- Typed schedule_invoke + entity-ref arg/result --------------------


@pytest.mark.slow
async def test_typed_schedule_invoke_with_entity_ref(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Pass a typed Protocol to ``get_service``; call a method that
    takes / returns ``EntityRef[_Note]``; retrieve via
    ``result_type_of`` to get the typed value back.

    Exercises the full typed encode/decode pipeline:
      - args wrapper: ``EntityRef[_Note] → FxtrID`` on encode.
      - return wrapper: ``FxtrID → EntityRef[_Note]`` on decode.
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-9"})

                    schedule_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        + f"svc = get_service({svc_short_id!r}, _NoteEchoProto)\n"
                        + "note_ref = _Note(title='hello', body='b').store()\n"
                        + "await schedule_invoke("
                        + '"r", svc, lambda s: s.echo_note(note_ref)'
                        + ")\n"
                    )
                    r1 = await _run(ctx, schedule_script)
                    assert "Error" not in r1, r1

                    promoted = await _wait_for_slot_promotion(app_ctx, "r")
                    assert promoted[0] == "invoke_result", promoted

                    retrieve_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import result_type_of, retrieve_result\n"
                        + 'result = await retrieve_result("r", '
                        + "    result_type_of(_NoteEchoProto, _NoteEchoProto.echo_note))\n"
                        # Typed return is an EntityRef[_Note] (not bare FxtrID).
                        + "assert isinstance(result, EntityRef), type(result).__name__\n"
                        + "loaded = await result.load()\n"
                        + 'assert loaded.title == "echoed:hello", loaded.title\n'
                        + 'assert loaded.body == "b", loaded.body\n'
                    )
                    r2 = await _run(ctx, retrieve_script)
                    assert "Error" not in r2, r2


# --- Typed schedule_invoke with kwargs --------------------------------


@pytest.mark.slow
async def test_typed_schedule_invoke_with_kwargs(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Typed dispatch path produces kwargs naturally for a
    keyword-only Protocol method; the wire ``InvokeOp.kwargs``
    plumbing (separate kwargs commit) delivers them to the impl."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    schedule_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        + f"svc = get_service({svc_short_id!r}, _NoteEchoProto)\n"
                        + "await schedule_invoke("
                        + '"r", svc, lambda s: s.echo_kw(msg="there", prefix="hi")'
                        + ")\n"
                    )
                    r1 = await _run(ctx, schedule_script)
                    assert "Error" not in r1, r1

                    promoted = await _wait_for_slot_promotion(app_ctx, "r")
                    assert promoted == ["invoke_result", "hi:there"], promoted


@pytest.mark.slow
async def test_typed_schedule_invoke_kwarg_default_round_trip(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A Protocol-declared default for a keyword-only arg rides
    through as ``USE_REMOTE_DEFAULT`` and the impl's own default
    fires. Confirms the typed encoder strips the sentinel before the
    wire."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    # Omit factor → impl's default factor=10 applies.
                    schedule_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        + f"svc = get_service({svc_short_id!r}, _NoteEchoProto)\n"
                        + "await schedule_invoke("
                        + '"r", svc, lambda s: s.echo_default(7)'
                        + ")\n"
                    )
                    assert "Error" not in await _run(ctx, schedule_script)
                    promoted = await _wait_for_slot_promotion(app_ctx, "r")
                    assert promoted == ["invoke_result", 70], promoted


# --- retrieve_result with raw annotation -----------------------------


@pytest.mark.slow
async def test_retrieve_result_with_raw_entityref_annotation(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``retrieve_result(slot, EntityRef[T])`` decodes a bare
    ``["entity", short_id]`` payload through the dialect's
    ``EntityRefServiceStrategy``, returning a typed
    ``EntityRef[T]`` (not just the bare FxtrID).
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    # Schedule via the typed path; retrieve with a raw
                    # EntityRef[_Note] annotation rather than the
                    # result_type_of sentinel.
                    schedule_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        + f"svc = get_service({svc_short_id!r}, _NoteEchoProto)\n"
                        + "note_ref = _Note(title='raw', body='b').store()\n"
                        + "await schedule_invoke("
                        + '"r", svc, lambda s: s.echo_note(note_ref)'
                        + ")\n"
                    )
                    assert "Error" not in await _run(ctx, schedule_script)
                    await _wait_for_slot_promotion(app_ctx, "r")

                    retrieve_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from _fxtr_mcp_builtin import retrieve_result\n"
                        + 'result = await retrieve_result("r", EntityRef[_Note])\n'
                        + "assert isinstance(result, EntityRef), type(result).__name__\n"
                        + "loaded = await result.load()\n"
                        + 'assert loaded.title == "echoed:raw", loaded.title\n'
                    )
                    assert "Error" not in await _run(ctx, retrieve_script)


# --- callback_open slot-tag default ----------------------------------


@pytest.mark.slow
async def test_callback_open_default_result_type(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``retrieve_result`` on a ``["callback_open", N]`` slot returns
    ``ServiceRef[WorkstationSessionEventInbox]`` by default — the slot-tag
    default kicks in when ``result_type`` is ``None``."""
    async with WorkstationServer() as ws:
        _write_workdir(tmp_path, ws.bound_url)
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {})

            create_script = (
                "from _fxtr_mcp_builtin import create_callback\n" 'create_callback("cb")\n'
            )
            assert "Error" not in await _run(ctx, create_script)

            verify_script = (
                "from fxtr2.data.entity_ref_scope import ServiceRef\n"
                "from _fxtr_mcp_builtin import WorkstationSessionEventInbox, retrieve_result\n"
                'ref = await retrieve_result("cb")\n'
                "assert isinstance(ref, ServiceRef), type(ref).__name__\n"
                "assert ref.target_type is WorkstationSessionEventInbox, ref.target_type\n"
            )
            assert "Error" not in await _run(ctx, verify_script)


# --- Error paths -----------------------------------------------------


@pytest.mark.slow
async def test_schedule_invoke_non_service_protocol_errors(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Passing a non-``@service_protocol`` target_type to
    ``get_service`` and then calling ``schedule_invoke`` raises
    :exc:`NotImplementedError` from the typed encoder."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    schedule_script = (
                        "from typing import Protocol\n"
                        "class _NotDecorated(Protocol):\n"
                        "    async def foo(self) -> str: ...\n"
                        "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        f"svc = get_service({svc_short_id!r}, _NotDecorated)\n"
                        "raised = False\n"
                        "try:\n"
                        '    await schedule_invoke("r", svc, lambda s: s.foo())\n'
                        "except NotImplementedError as e:\n"
                        "    raised = True\n"
                        '    assert "@service_protocol" in str(e), str(e)\n'
                        'assert raised, "expected NotImplementedError"\n'
                    )
                    assert "Error" not in await _run(ctx, schedule_script)


@pytest.mark.slow
async def test_schedule_invoke_unknown_method_errors(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Calling a method that isn't declared on the typed Protocol
    raises an ``IRException`` with category ``UnknownMethod`` from
    the typed encoder. The error surfaces as the script's tool
    output (RunResult.error)."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    schedule_script = (
                        _SANDBOX_FIXTURES_SRC
                        + "from fxtr2.leases.leased_cap_ir import IRException\n"
                        + "from _fxtr_mcp_builtin import get_service, schedule_invoke\n"
                        + f"svc = get_service({svc_short_id!r}, _NoteEchoProto)\n"
                        + "raised = False\n"
                        + "try:\n"
                        + '    await schedule_invoke("r", svc, '
                        + "        lambda s: s.nonexistent_method())\n"
                        + "except IRException as e:\n"
                        + "    raised = True\n"
                        + '    assert e.category == "UnknownMethod", e.category\n'
                        + 'assert raised, "expected IRException"\n'
                    )
                    assert "Error" not in await _run(ctx, schedule_script)


# --- Untyped path still works ---------------------------------------


@pytest.mark.slow
async def test_untyped_path_unchanged(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """The default ``UnknownService`` target_type skips the typed
    encoder entirely — args walk through ``_walk_for_wire``
    directly. Confirms the existing untyped path is untouched."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_NoteEchoImpl())
            async with expose_service(
                peer.remote_service,
                "note-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                _write_workdir(tmp_path, ws.bound_url)
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    # No service_type → UnknownService. Pass msg as
                    # a kwarg to exercise the (now-supported) untyped
                    # kwargs path.
                    schedule_script = (
                        "from _fxtr_mcp_builtin import (\n"
                        "    get_service, schedule_invoke\n"
                        ")\n"
                        f"svc = get_service({svc_short_id!r})\n"
                        "await schedule_invoke("
                        '"r", svc, lambda s: s.echo_kw(msg="there")'
                        ")\n"
                    )
                    assert "Error" not in await _run(ctx, schedule_script)
                    promoted = await _wait_for_slot_promotion(app_ctx, "r")
                    assert promoted == ["invoke_result", "kw:there"], promoted
