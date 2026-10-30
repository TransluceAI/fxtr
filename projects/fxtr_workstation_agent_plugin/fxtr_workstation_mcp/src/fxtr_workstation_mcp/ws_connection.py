"""Long-lived workstation connection management.

Owns two persistent WebSocket connections to a workstation server,
plus the active session subscription. Tools never enter the
underlying scopes themselves — a single supervisor task in the
lifespan's task group owns the entire lifecycle (entry, exit,
draining). Tools submit *jobs* onto a queue; the supervisor's worker
subtask drains the queue.

The worker-queue model is the central invariant: every operation
the MCP performs is a single item drained from this queue.
``_OpenSessionJob`` / ``_CloseSessionJob`` / ``_ScriptJob`` come
from tool calls; ``_EventJob`` comes from the reader pushing
inbound :class:`SessionEventFrame`s onto the queue. The worker
drains one at a time, with full ownership of the connection-side
host scope, per-script script_scopes, and the workstation
``/client`` send side. RPC dispatches into the host (e.g. a script
calling ``lookup_entity``) run on RPC-owned tasks during
``runner.run()``; they're structured-concurrency children of the
per-script scope and fully drain before the worker resumes.

Stage 3 fills in the body of the ``_EventJob`` handler (foreign
state-update reload + invoke-result promotion); stage 2 + post-1
keep it as a no-op log.

Architecture (subtask layout under one ``WorkstationConnection``)::

    supervisor_loop  (one task in the lifespan's task group)
      ├── opens workstation /rpc connection (open_rpc_scope)
      │   resolves a connection-cached workstation_proxy in
      │   host_scope; nothing is mounted there.
      ├── opens workstation /client websocket
      └── runs an inner task group containing:
           ├── reader task          parses ServerFrames; routes
           │                        per-request frames into pending-
           │                        request streams; routes
           │                        SessionEventFrames into _EventJobs
           │                        pushed onto the worker queue
           └── worker task          drains the job queue; one job at
                                    a time; jobs do their own send +
                                    await-response sequencing using
                                    the per-request stream plumbing.
                                    _ScriptJob owns its per-script
                                    script_scope + McpHostImpl mount
                                    for the duration of the job.

Submission API (tool-side):

- ``connection.open_session(metadata)`` — single typed job; creates
  + subscribes + atomically becomes the connection's active session.
  Errors if a session is already active.
- ``connection.close_session()`` — single typed job; sends a single
  ``CloseOp`` mutation against the active subscription, clears
  active-session state on commit.
- ``connection.run_script(...)`` — submits a ``_ScriptJob`` and
  awaits its completion. The worker handler opens a per-script
  ``script_scope``, mounts a fresh :class:`McpHostImpl` on it,
  invokes the sandbox runner with that mount's ``host_ref``, and
  returns the script's tool-result string.
- ``connection.start(tg)`` — schedules the supervisor on ``tg``.
  Idempotent. The supervisor starts in
  :attr:`SupervisorState.PAUSED` and waits for an explicit wake
  signal before doing any work.
- ``connection.wake_and_wait_ready()`` — wake the supervisor if
  paused (signal at most once per call), then wait until
  :attr:`SupervisorState.READY`. Raises
  :class:`SupervisorNotReady` if the supervisor pauses again
  after the one retry attempt, or if it has already EXITED.
- ``connection.shutdown()`` — sets the shutdown event; the
  supervisor drains and exits when the lifespan TG awaits it.

State machine (see :class:`SupervisorState`): the supervisor
collapses all "not currently working" cases into a single
PAUSED state, transitions to STARTING when woken by a tool,
flips to READY once subscribed, and re-enters PAUSED on any
failure that isn't a wire blip with budget remaining. Wire-error
free retries (capped by :data:`MAX_FREE_RETRY_BUDGET`,
refreshed on each successful ``wake_and_wait_ready`` return)
take the READY → STARTING path silently. No non-cancellation
exception escapes the supervisor task.

The connection's *active subscription* is part of supervisor-owned
state. The supervisor knows how to: bring up the connections,
optionally subscribe to a configured session at startup, switch the
subscription via ``open_session`` / ``close_session``, and tear
everything down on shutdown.
"""

from __future__ import annotations

import contextlib
import enum
import itertools
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

import anyio
import anyio.abc
import httpx
import httpx_ws
import msgspec
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.scope_bridge import RPCSession, open_rpc_scope
from fxtr_sandbox_types.json_object_stream import JSONObjectStream
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.direct_launcher import DirectSandboxEnvironment
from fxtr_workstation import expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    AckResponse,
    CallbackEvent,
    CallbackSubscriptionEvent,
    ClientRequest,
    CloseOp,
    CreateSessionRequest,
    ErrorResponse,
    GetCallbackServiceRequest,
    GetCallbackServiceResponse,
    InvokeErrorEvent,
    InvokeResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    NonStateSessionEvent,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    ServerFrame,
    ServiceBrokenNotification,
    SessionCreatedResponse,
    SessionDebugSnapshotEvent,
    SessionEventFrame,
    SessionSnapshotEvent,
    StateUpdateEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import (
    PersistenceProto,
    WorkstationServiceProto,
    WorkstationSessionEventInbox,
)

from .agent_log_types import WsAgentLogEventChain
from .agent_mcp_impl import AgentMcpRunningSessionImpl
from .log_builder import (
    build_callback_notification_event,
    build_invoke_error_event,
    build_invoke_result_event,
    build_script_execution_event,
    extend_chain,
)
from .websocket_util import aconnect_ws

from .channel import ChannelNotifier, summarize_invoke_payload
from .config import ConfigError, read_active_session
from .exception_log import ExceptionLog
from .host_impl import McpHostImpl
from .mutation_builder import BuildResult, build_mutate_request, substitute_op_for_log
from .session_state import (
    CALLBACK_OPEN_TAG,
    KIND_CREATE_CALLBACK,
    KIND_EXPORT_ENTITY,
    KIND_PRESENT_TO_USER,
    KIND_SCHEDULE_CALLBACK_INVOKE,
    KIND_SCHEDULE_INVOKE,
    ForgetMissingKey,
    PendingChange,
    SessionMirror,
    SlotKeyAlreadyExists,
    SlotKeyReserved,
    workstation_state_needs_init,
)
from fxtr_ws_mcp_sandbox_bridge.protocols import McpRunnerProto, RunnerConfig, slot_handle

_log = logging.getLogger(__name__)

_SERVER_FRAME_DECODER = msgspec.json.Decoder(ServerFrame)
_REQUEST_ENCODER = msgspec.json.Encoder()


# --- Common errors ---------------------------------------------------


class ConnectionLost(RuntimeError):
    """Raised when the workstation connection drops while a request
    is in flight, or before the request can complete."""


class SessionAlreadyActive(RuntimeError):
    """``open_session`` was called while the connection already had
    an active session subscription."""


class NoActiveSession(RuntimeError):
    """``close_session`` was called with no active session."""


class ProtocolError(RuntimeError):
    """A workstation-emitted frame violated a wire-protocol invariant
    the MCP relies on (e.g. a state_update with seq <= the last
    state_update's seq, a state_update embedded in a snapshot's
    event buckets, an event already-processed within the cycle).

    Surfaced through the supervisor; the supervisor exits its inner
    task group, the outer reconnect loop reopens, and a fresh
    subscribe rebuilds local state from a clean snapshot."""


class SupervisorNotReady(RuntimeError):
    """The supervisor is not in :attr:`SupervisorState.READY` and
    cannot bring itself there. Raised by
    :meth:`WorkstationConnection.wake_and_wait_ready` after exactly
    one retry attempt failed (or when the supervisor has already
    transitioned to :attr:`SupervisorState.EXITED`).

    The exception message is the supervisor's ``pause_reason`` —
    a human-readable string assembled when the cycle failed (e.g.
    ``"workstation rejected: subscribe_session(...) failed: ..."``,
    ``"connection lost: ..."``, ``"unimplemented handler: ..."``).
    Tool layers surface it as a regular tool-error string."""


class WorkstationRejected(RuntimeError):
    """The workstation refused a request the MCP issued — e.g. a
    ``CreateSessionRequest`` returned ``ErrorResponse``, a
    ``SubscribeSessionRequest`` failed (typically: ``session_gone``),
    a ``MutateSessionRequest`` came back as
    ``MutationRejectedResponse``, or transport errors surfaced on a
    request response.

    These are *tool-level* failures: the connection is still
    healthy, the agent can retry with different parameters, etc.
    Caught by the per-job handlers and routed to the tool's
    completion future — never tears down the supervisor."""


class ConfigInvalid(WorkstationRejected):
    """``.fxtr_workstation.toml`` failed to parse / validate at the
    top of a supervisor cycle.

    Subclass of :class:`WorkstationRejected` so it routes through
    the normal cycle-failure path: the supervisor catches it, sets
    :attr:`pause_reason` to the toml-error string, transitions to
    :attr:`SupervisorState.PAUSED`, and emits the
    ``supervisor_paused`` banner. The agent fixes the toml and
    calls a tool to retry — no MCP restart needed."""


class _DisconnectRequested(RuntimeError):
    """Sentinel raised inside ``_run_inner`` when the agent's
    ``disconnect()`` tool fires ``_disconnect_requested``.

    Routes through the supervisor's outer except branch the same
    way a wire blip does (cancels the inner task group, ends the
    cycle), but the pause path recognizes this specific type so
    it can:

    - Set ``pause_reason`` to ``"disconnected by request"`` (or
      the exception's message) instead of running it through
      :func:`_format_pause_reason`.
    - **Skip** the ``supervisor_paused`` channel banner — the
      agent asked for this state, so re-notifying them would
      be noise.

    Internal type: only the supervisor task constructs and
    catches it. Never escapes the supervisor."""


class ForceCloseTimedOut(RuntimeError):
    """The one-shot websocket flow in
    :func:`force_close_session_one_shot` exceeded its overall time
    budget (default 10s).

    Means the workstation accepted the TCP/WS handshake but never
    sent the response frames we were awaiting (subscribe ack,
    snapshot, or mutation response). Distinct from
    :class:`ConnectionLost` (which means the wire actually dropped):
    here the socket is technically still open but the server is
    unresponsive.

    Caught by ``_do_force_close_session``'s generic
    ``except Exception`` branch, which surfaces a clear error
    string to the agent and leaves the toml's ``active_session``
    untouched (we did not successfully close, so we don't pretend
    the session is gone)."""


# --- Supervisor state machine ----------------------------------------


class SupervisorState(enum.Enum):
    """Lifecycle states for the :class:`WorkstationConnection`
    supervisor task.

    Transitions are owned by the supervisor task. Tools observe
    state via :attr:`WorkstationConnection.state` (read) and
    :attr:`WorkstationConnection.state_changed` (await for change).
    Tools never write the state directly; they may set
    :attr:`WorkstationConnection.retry_requested` to ask the
    supervisor to leave :attr:`PAUSED`.

    Valid transitions:

    - ``PAUSED → STARTING``: tool fired ``retry_requested``.
    - ``STARTING → READY``: cycle subscribed (or skipped subscribe
      for a no-active-session cycle) and is about to drain jobs.
    - ``STARTING → PAUSED``: cycle raised before reaching READY.
    - ``READY → STARTING``: cycle raised a wire error AND
      ``free_retry_budget > 0`` — the supervisor's free-retry
      path runs another cycle without waiting.
    - ``READY → PAUSED``: cycle raised any non-wire exception,
      or a wire exception with the free-retry budget exhausted.
    - ``* → EXITED``: lifespan shutdown fired (or the supervisor
      task was cancelled externally).
    """

    PAUSED = "paused"
    STARTING = "starting"
    READY = "ready"
    EXITED = "exited"


#: How many consecutive wire-error free retries the supervisor will
#: perform without an explicit agent retry signal. Decrements per
#: free retry; refreshed to the constant value each time
#: :meth:`WorkstationConnection.wake_and_wait_ready` returns with
#: state == READY. Once the budget hits zero, the next wire drop
#: routes through PAUSED instead of auto-retry.
MAX_FREE_RETRY_BUDGET = 3


# --- Future ----------------------------------------------------------

_T = TypeVar("_T")


class Future(Generic[_T]):
    """One-shot result holder. Set exactly once via ``set_result`` or
    ``set_exception``; awaited via ``wait``. Used to thread results
    from the worker task back to whoever submitted a job.

    anyio doesn't ship a Future, so we build one on top of
    :class:`anyio.Event` — single waiter is plenty for our usage.
    """

    __slots__ = ("_event", "_result", "_exception", "_set")

    def __init__(self) -> None:
        self._event = anyio.Event()
        self._result: _T | None = None
        self._exception: BaseException | None = None
        self._set = False

    def set_result(self, value: _T) -> None:
        if self._set:
            return
        self._result = value
        self._set = True
        self._event.set()

    def set_exception(self, exc: BaseException) -> None:
        if self._set:
            return
        self._exception = exc
        self._set = True
        self._event.set()

    async def wait(self) -> _T:
        await self._event.wait()
        if self._exception is not None:
            raise self._exception
        # mypy/pyright: _result is set whenever _exception is None and
        # _set is True; the only path past the await sets one of them.
        return self._result  # type: ignore[return-value]


# --- /rpc data bundle -----------------------------------------------


@dataclass
class WorkstationRpc:
    """Data bundle exposing the ``/rpc`` connection's pieces.

    The supervisor owns the lifetime of the underlying ``RPCSession``
    and ``host_scope``; this dataclass is the read-only view tools
    and the worker get when accessing ``connection.rpc``.

    - ``host_scope`` is the connection-lived parent scope for
      per-script ``script_scope``s. It ``include``s the ``/rpc``
      session's user scope so the workstation proxy resolved into it
      stays reachable.
    - ``workstation_proxy`` is the connection-cached
      :class:`WorkstationServiceProto` proxy resolved once at
      supervisor startup. Per-script :class:`McpHostImpl` instances
      hold a reference to this proxy and forward
      ``lookup_entity`` / future stage-3 methods through it.
    - ``workstation_ref`` is the same proxy expressed as a
      ``ServiceRef``; needed by stage-3+ helpers such as
      ``expose_entity`` that take the ref rather than the resolved
      proxy.
    """

    rpc_session: RPCSession[WorkstationServiceProto]
    host_scope: FxtrScope
    workstation_ref: ServiceRef[WorkstationServiceProto]
    workstation_proxy: WorkstationServiceProto


# --- Per-request frame routing --------------------------------------


@dataclass
class _PendingRequest:
    """Routing entry for a single in-flight request_id (or
    subscription_id, since ``SessionSnapshotEvent`` uses the originating
    subscribe's request_id as its subscription_id by protocol
    convention).

    The reader pushes incoming frames into ``send``; the worker awaits
    them via ``receive``. Buffer size is 4 — the longest expected
    response sequence in stage 1 is 2 frames (subscribe → ack +
    snapshot); 4 leaves headroom for stage 5's callback creation
    paths.
    """

    request_id: int
    send: MemoryObjectSendStream[ServerFrame]
    receive: MemoryObjectReceiveStream[ServerFrame]


# --- Job kinds -------------------------------------------------------
#
# Every operation the MCP performs lands as one of these. Tool-call
# jobs (open / close / script) carry a completion future; reader-
# pushed jobs (event) are fire-and-forget. Stage 3 fills in the
# ``_EventJob`` body; stage 4 grows ``_ScriptJob`` to carry queued
# ops back to the worker for the mutation pipeline.


@dataclass
class _OpenSessionJob:
    """Create a fresh session + subscribe to it + atomically become
    the connection's active session. Errors if a session is already
    active.
    """

    metadata: dict[str, Any]
    completion: Future[str]  # session_id


@dataclass
class _CloseSessionJob:
    """Close the active session via a single-op CloseOp mutation;
    clear active-session state on commit."""

    completion: Future[None]


@dataclass(frozen=True)
class ScriptOutcome:
    """Outcome of a ``_ScriptJob`` returned to the tool layer.

    ``body`` is the human-readable result string (mutation summary,
    error message, or compressed two-frame traceback). On script-
    raised paths the traceback is **compressed** by
    :func:`_compress_script_traceback` before landing here — only the
    headline + ``[user]`` (deepest script/preamble frame) + ``[raise]``
    (innermost frame) are kept, plus an ``exc_id`` pointer to the
    full text in :class:`exception_log.ExceptionLog`. The agent
    retrieves the full traceback on demand via the
    ``get_full_exception(exc_id)`` sandbox builtin.

    ``scheduled_invoke_slot_keys`` lists the slot_keys for invokes
    this script just scheduled into ``data_by_key`` as
    ``["invoke_pending", N]`` entries; ``run_script(..., wait_seconds=...)``
    uses it to know which slots to wait on before draining the
    outgoing buffer. Empty list on any path that didn't commit a
    mutation (script-raised, mutation rejected, empty queued ops).
    """

    body: str
    scheduled_invoke_slot_keys: list[str]


@dataclass
class _ScriptJob:
    """Run one sandboxed script.

    The worker handler opens a per-script ``script_scope`` (child of
    ``host_scope``), mounts a fresh :class:`McpHostImpl` on it,
    threads the resulting per-script ``host_ref`` into
    :class:`RunnerConfig`, opens a sandbox via the supplied
    ``env``, and invokes the runner. ``script_scope`` drains on job
    exit, taking the McpHostImpl + any copied-out entities with it.

    Stage 2 + post-1: the queued-ops accumulator is built and
    threaded into the per-script :class:`McpHostImpl` but stays
    empty (no agent-facing builtins write to it yet). Stage 3 grows
    the worker handler to consume it and submit the resulting
    mutation; the job's completion future starts to surface
    mutation outcomes too.
    """

    preamble_src: str
    script_src: str
    env: DirectSandboxEnvironment
    volumes: list[tuple[str, str, bool]]
    sys_path_additions: list[str]
    working_dir: Path
    completion: Future[ScriptOutcome]


@dataclass
class _EventJob:
    """Handle one inbound :class:`SessionEventFrame` for the active
    subscription. Pushed onto the worker queue by the reader so
    foreign-mutation reload + result-event promotion serialize
    behind any in-flight ``_ScriptJob`` / ``_OpenSessionJob`` /
    ``_CloseSessionJob``.

    Reader-pushed (no completion future). Stage 3a: state_update
    advances the mirror + ``_last_processed_seq``. Stage 4 grows
    the handler to promote invoke results into ``data_by_key``.
    """

    frame: SessionEventFrame


@dataclass
class _AckCommitJob:
    """Mark a self-committed mutation's ``seq`` as locally
    processed.

    Enqueued by ``_apply_script_mutation`` after a successful
    commit, so that ``_last_processed_seq`` only advances past
    *foreign* events that were queued before this job — the worker
    drains them first, advancing the seq monotonically, and this
    job lands afterwards as a no-op (or as a small additional
    advancement to our own commit's seq).

    Fire-and-forget (no completion future). The worker's handler
    is just ``self._last_processed_seq = max(...)``.

    ``silent``: when True, suppresses the ``self_commit`` channel
    notification. Internal mutations (e.g. the bootstrap's
    workstation_inbox creation) set this so the agent doesn't see
    a temporal landmark for an MCP-housekeeping commit. Default
    False (agent-script commits get their landmark as before).
    """

    seq: int
    silent: bool = False


@dataclass
class _ServiceBrokenJob:
    """Surface an out-of-band :class:`ServiceBrokenNotification` to the
    agent.

    Pushed by the reader when a break frame arrives for the active
    subscription. Routed through the worker (rather than the reader
    calling the async ``_notify_channel`` directly) so it serializes
    with the other agent-facing notifications and so the per-session
    dedup set is touched single-threaded. Not a reducer event — it
    never touches the mirror / replayable events; it only emits a
    channel notice (deduped per short_id via
    ``_notified_broken_services``).

    Fire-and-forget (no completion future).
    """

    service_short_id: str


@dataclass
class _BootstrapAgentMcpJob:
    """Bring up the per-session ``agent_mcp:{session_id}`` service.

    Submitted only from the **cycle-start path** — the supervisor
    queues this job in ``_run_inner`` after ``_do_subscribe``
    enqueues the snapshot's ``subsequent_events``, then awaits the
    completion future before transitioning to READY. The worker
    drains those ``_EventJob``s first (advancing
    ``_last_processed_seq`` past any pre-bootstrap events), then
    runs this job.

    The other path that needs bootstrap — ``_do_open_session`` —
    can't submit-and-await a job (it's *already* running in the
    worker; the worker only processes one job at a time, that'd
    deadlock). Instead it calls
    ``_do_bootstrap_agent_mcp_inner`` directly, which does the
    same work in the same worker context. Seq ordering stays
    correct because the bootstrap's ``_AckCommitJob`` lands on
    the queue behind any pre-existing ``_EventJob``s; when control
    returns to the worker loop, those drain first.

    Worker handler does the seven bootstrap steps documented in
    ``_do_bootstrap_agent_mcp_job``. On success sets
    ``self._agent_mcp_impl`` / ``self._agent_mcp_persistence_ref``
    (via the spawned lifecycle subtask) and resolves the future.
    On failure both surfaces the error via the future *and*
    re-raises so the supervisor's cycle teardown fires (a failed
    bootstrap means the MCP isn't properly connected — we don't
    want the cycle to keep running with a broken agent_mcp
    surface).

    close_session doesn't use this job either — it runs the
    teardown inline via the saved cancel_scope on the agent_mcp
    subtask before sending ``CloseOp``.
    """

    session_id: str
    completion: Future[None]


_Job = (
    _OpenSessionJob
    | _CloseSessionJob
    | _ScriptJob
    | _EventJob
    | _AckCommitJob
    | _ServiceBrokenJob
    | _BootstrapAgentMcpJob
)


# --- Log intents (chunk E) --------------------------------------------
#
# Pushed by the worker (sync, no awaits) onto an in-memory stream
# the log writer drains asynchronously. Decouples the worker's
# state-mutation flow from the chain-build / pin path: pushing is
# O(1) and never depends on the agent_mcp impl being ready or the
# persistence cap responding.
#
# The writer (see ``_log_writer_loop``) builds the typed log
# entity, extends the chain, pins, calls ``impl.update_head``. It
# exits on receiving ``_LogIntent_Shutdown`` (graceful close
# semantics; see ``_do_close_session``) or on task-group
# cancellation (cycle teardown / abnormal close).


@dataclass(frozen=True)
class _LogIntent_ScriptExecution:
    """Worker → log writer: one script-execution outcome.

    ``error`` is the **full** formatted-traceback string produced
    by ``script_runner.run_preamble_and_script`` (no compression):
    headline + ``traceback.format_exception`` output. The
    on-chain :class:`agent_log_types.ScriptExecutionLogEvent`
    persists this as-is so frontend watchers can show the
    operator the whole stack. The agent's tool response carries a
    parallel **compressed** form (see :class:`ScriptOutcome`); the
    same full string also lands in
    :class:`exception_log.ExceptionLog` keyed by ``exc_id`` for
    on-demand retrieval via ``get_full_exception``.

    ``runner_queued_ops`` is the **post-placeholder-substitution**
    form of the script's queued ops — the caller (the script-job
    handler) runs :func:`mutation_builder.substitute_placeholders`
    on each op's args / kwargs / payload before pushing this
    intent, so the log builder sees ``["entity", short_id]`` /
    ``["callback", N]`` and not ``"entity_handle"`` /
    ``"callback_handle"`` placeholders.

    On error / rejected paths ``runner_queued_ops`` is the empty
    list (no committed mutation → no ops to log).

    ``prefilled_entity_short_ids`` carries every ``short_id`` →
    ``FxtrID`` mapping the worker resolved *while the per-script
    ``expose_entity`` registrations were still alive*. The log
    writer's walker uses this as a pre-populated cache so it
    never has to dispatch ``workstation.get_entity(short_id)``
    later — which would fail for ``present_to_user`` payloads
    (those entities have no other workstation-side pin and would
    be released as the worker's :class:`AsyncExitStack` exits).
    The resolved FxtrIDs land in ``host_scope`` via the proxy's
    ``track_from_scope``, where they outlive the per-script
    stack and remain reachable to the writer's ``impl_scope``
    (which descends from ``host_scope``).
    """

    script_src: str
    preamble_src: str
    stdout: str
    stderr: str
    error: str | None
    rejected: str | None
    committed_seq: int | None
    runner_queued_ops: list[dict[str, Any]]
    export_entity_short_ids: dict[str, str]
    prefilled_entity_short_ids: dict[str, FxtrID]


@dataclass(frozen=True)
class _LogIntent_InvokeResult:
    """Worker → log writer: an ``InvokeResultEvent`` arrived for
    a slot we own."""

    slot_key: str
    slot_n: int
    seq: int
    payload: Any  # wire-form FxtrServiceData


@dataclass(frozen=True)
class _LogIntent_InvokeError:
    """Worker → log writer: an ``InvokeErrorEvent`` arrived for a
    slot we own. Error messages are short strings; no payload to
    walk."""

    slot_key: str
    slot_n: int
    seq: int
    message: str


@dataclass(frozen=True)
class _LogIntent_CallbackNotification:
    """Worker → log writer: a ``CallbackEvent`` arrived for a
    callback slot we own. Includes the ``workstation_inbox`` case
    (user messages sent via ``agent_mcp.send``)."""

    slot_key: str
    slot_n: int
    seq: int
    payload: Any  # wire-form FxtrServiceData


@dataclass(frozen=True)
class _LogIntent_Shutdown:
    """Sentinel pushed by ``_do_close_session``. The writer
    drains every intent ahead of it, then exits cleanly so the
    final ``ScriptExecutionLogEvent`` (or any earlier event) lands
    in the chain before the agent_mcp subtask tears down."""


_LogIntent = (
    _LogIntent_ScriptExecution
    | _LogIntent_InvokeResult
    | _LogIntent_InvokeError
    | _LogIntent_CallbackNotification
    | _LogIntent_Shutdown
)


# --- The connection --------------------------------------------------


class WorkstationConnection:
    """Persistent, supervisor-managed connection to a workstation.

    Wraps both the workstation ``/rpc`` and ``/client`` connections
    plus an optional active session subscription. The supervisor task
    (started via :meth:`start`) owns the lifecycle: opens both
    connections, runs reader / worker / session_event_consumer
    subtasks, optionally subscribes to a starting session, parks on
    the shutdown event, and drains cleanly when shutdown fires (or
    when the lifespan task group cancels it).

    Tools never touch context managers themselves; they call typed
    submit methods (:meth:`open_session`, :meth:`close_session`)
    that enqueue jobs onto the worker queue and await completion.
    """

    def __init__(
        self,
        rpc_url: str,
        client_url: str,
        *,
        connection_name: str = "workstation_mcp",
        working_dir: Path | None = None,
        notify_channel: ChannelNotifier | None = None,
        agent_mcp_store: str | None = None,
        exception_log: ExceptionLog | None = None,
    ) -> None:
        """Construct an unstarted connection.

        The supervisor task isn't running yet; call :meth:`start`
        with a task group. The supervisor begins in
        :attr:`SupervisorState.PAUSED` with reason
        ``"(not started)"`` and stays idle until a tool calls
        :meth:`wake_and_wait_ready`.

        Args:
            rpc_url: ``ws://host:port/rpc``.
            client_url: ``ws://host:port/client``.
            connection_name: Human-readable name reported to the
                workstation via ``name_connection``. Visible in the
                workstation's debug-UI connection list.
            working_dir: Path to the directory containing
                ``.fxtr_workstation.toml``. When set, the supervisor
                re-reads the toml at the top of every reconnect
                cycle via :func:`config.read_active_session` and
                refreshes ``_active_session_id`` before attempting
                the subscribe. This is what lets the agent recover
                from a stale session ID by manually editing the
                toml — the next cycle picks up the change.
                ``None`` skips the refresh entirely (tests that
                don't go through the supervisor pass ``None``).
            notify_channel: Optional async callback for surfacing
                slot promotions + ``supervisor_paused`` banners to
                the agent via ``notifications/claude/channel``. If
                ``None``, channel notifications are silently dropped
                (the slot is still promoted in the local mirror; the
                agent will just see it on their next
                ``retrieve_result`` rather than getting a push). The
                lifespan in ``server.py`` constructs a real callback
                bound to the FastMCP server; tests pass a mock or
                omit.
            agent_mcp_store: External name of the workstation
                persistence cap the per-session bootstrap looks up
                to persist the typed event log. ``None`` skips
                persistence entirely (the log lives in memory only).
                Defaults to ``None``; the toml-aware caller
                (``server.set_working_dir``) threads
                ``WorkstationConfig.agent_mcp_store`` through here.
            exception_log: Lifespan-scoped bounded LRU holding
                full script-exception tracebacks. The host-side
                ``_format_script_error`` stores the full text
                here and surfaces a short ``exc_id`` to the
                agent; the agent retrieves the full form via the
                ``get_full_exception(exc_id)`` sandbox builtin.
                ``None`` skips storage entirely (test paths and
                light-touch tests pass ``None``).
        """
        self._rpc_url = rpc_url
        self._client_url = client_url
        self._connection_name = connection_name
        self._working_dir = working_dir
        self._notify_channel = notify_channel
        self._agent_mcp_store = agent_mcp_store
        # ``None`` is allowed for the no-McpHostImpl tests that
        # don't run scripts; production passes a lifespan-scoped log.
        # On a script error with ``None`` the host-side formatter
        # falls back to "exc_id=unavailable" rather than crashing.
        self._exception_log = exception_log

        self._supervisor_started = False

        # --- Supervisor state machine (see SupervisorState) ---
        #
        # ``state`` and ``pause_reason`` are the public observation
        # surface; both are mutated only by the supervisor task via
        # :meth:`_set_state`. ``state_changed`` is fired (and
        # replaced with a fresh unset event) on every state change,
        # so readers can capture the event before reading state and
        # then await it for the next transition. Standard
        # condition-variable pattern using anyio Events.
        self._state: SupervisorState = SupervisorState.PAUSED
        self._pause_reason: str | None = "(not started)"
        self._state_changed = anyio.Event()

        # ``retry_requested`` is set by :meth:`wake_and_wait_ready`
        # to ask the supervisor to leave PAUSED. The supervisor's
        # paused-branch await wakes on it (or on ``_shutdown``),
        # then replaces it with a fresh event for the next park.
        self._retry_requested = anyio.Event()

        self._shutdown = anyio.Event()
        self._exited = anyio.Event()

        # Set by :meth:`disconnect` to ask the supervisor to end
        # its current cycle and park in PAUSED *without* tearing
        # down the supervisor itself or closing the session
        # workstation-side. The cycle's ``_run_inner`` races this
        # against ``_shutdown``; if disconnect wins, the inner
        # task group is cancelled, ``_DisconnectRequested`` is
        # raised, and the supervisor's pause path catches it.
        # Replaced with a fresh event each time it fires.
        self._disconnect_requested = anyio.Event()

        # Free-retry-budget for wire blips after a cycle reached
        # READY. Decrements per free retry; refreshed to
        # ``MAX_FREE_RETRY_BUDGET`` each time
        # :meth:`wake_and_wait_ready` returns with state == READY.
        # Resets to MAX in :meth:`_set_state` when entering
        # PAUSED (cap is a safety net, not a hard limit per agent
        # session — the cap-refresh semantics matter only between
        # successive READY hits without a tool-call in between).
        self._free_retry_budget: int = MAX_FREE_RETRY_BUDGET

        # Per-cycle flag the supervisor flips to True when the
        # current cycle reached READY. Reset at the top of each
        # cycle. Used by the wire-error free-retry decision.
        self._reached_ready_this_cycle: bool = False

        # Set when the supervisor has brought up both connections.
        self._rpc: WorkstationRpc | None = None
        self._ws: httpx_ws.AsyncWebSocketSession | None = None

        # Active session subscription state. Owned by the supervisor;
        # exposed read-only via ``active_session_id``. Populated by
        # :meth:`_refresh_config_from_disk` at the top of every cycle
        # when ``working_dir`` is set (so toml edits take effect on
        # the next cycle without restarting the MCP).
        self._active_session_id: str | None = None
        self._active_subscription_id: int | None = None

        # The highest event ``seq`` we've fully processed locally —
        # the value we send as ``ack_seq`` on outgoing mutations.
        # Advances when a state_update event lands in
        # ``_do_event_job`` (foreign mutator) and when an
        # ``_AckCommitJob`` enqueued after our own commit fires.
        # Stays unchanged when ``_apply_local_commit`` runs — that
        # would jump past any foreign events queued before our
        # response, which we haven't applied yet. The job-queue
        # drain order then guarantees our own commit's seq is
        # acked only after every preceding event has been processed.
        self._last_processed_seq: int = 0

        # Local mirror of the active session's state. Refreshed at
        # subscribe-time from the snapshot, on foreign state_update
        # in ``_do_event_job``, and on our own commit (synthesized
        # locally — workstation self-suppresses self-mutator events)
        # in ``_do_script_job``. Only mutated on the worker task;
        # the worker is parked on ``await`` whenever script-side or
        # event-side code runs, so no contention.
        #
        # ``mirror.data_by_key`` is the *local view* — workstation
        # view (carried by ``mirror.state``) plus the replayed
        # effects of any non-state events in
        # ``_replayable_events``. The mirror itself stores the data
        # directly, not the originating ``StateUpdateEvent``.
        self._mirror = SessionMirror()

        # Authoritative list of non-state events we've received +
        # processed locally that the workstation hasn't yet
        # subsumed via ``ack_seq``. Every event has seq >
        # ``mirror.last_state_ack_seq``, in seq order. Trimmed in
        # ``_handle_state_update`` (drop entries with seq <= new
        # ack_seq). Source of truth for the replay rebuild.
        #
        # In stage 3 this list stays empty (the MCP doesn't issue
        # ops that produce non-state events, and ``_apply_state_effect``
        # is a stub). Stage 4 wires the real handler.
        self._replayable_events: list[NonStateSessionEvent] = []

        # Ordered log of pending mutations the workstation hasn't
        # yet committed. Each entry is a :class:`PendingChange`
        # intent translated to JSON Patch ops at mutation-build
        # time. Two sources populate this list:
        #
        # - **Init prologue.** When ``apply_workstation_view``
        #   lands a workstation state lacking the top-level fields
        #   the MCP writes to (fresh session, post-subscribe
        #   wipe), a synthetic ``PendingChange(op="initialize")``
        #   is prepended.
        # - **Non-state events.** ``_apply_state_effect`` appends
        #   a ``PendingChange(op="set", ..., seq=ev.seq)`` for
        #   each non-state event we absorb locally.
        #
        # Cleared on self-commit (the workstation has now applied
        # everything we sent). Trimmed in ``_handle_state_update``
        # for entries past the workstation's ack horizon, then
        # rebuilt from surviving ``_replayable_events`` (plus an
        # init prologue if the new state still needs one).
        self._pending_changes: list[PendingChange] = []

        # Workstation's slot counter, copied from each
        # :class:`StateUpdateEvent.slot_counter`. Stage 4 will use
        # this to allocate fresh load / invoke slots; stage 3 just
        # tracks it (always 0 in stage 3 since neither side claims
        # slots).
        self._slot_counter: int = 0

        # Seqs of non-state events we've already delivered as
        # channel notifications to the agent. ``_maybe_notify``
        # (called inline from ``_apply_state_effect``) checks +
        # adds before sending. Lets us call ``_apply_state_effect``
        # from both initial arrival and from
        # ``_handle_state_update``'s rebuild loop (or from a
        # subscribe-time replay across a reconnect) without
        # re-surfacing already-notified events: redelivery is
        # bit-identical to initial delivery at the state-mutation
        # layer, and the dedupe layer suppresses the duplicate
        # surface. Pruned alongside the ``_replayable_events`` trim
        # in ``_handle_state_update`` (entries with seq <= new
        # ack_seq drop) and in ``_apply_local_commit`` (cleared
        # outright, same reasoning as the unflushed dict). Reset
        # on subscribe / close.
        self._notified_seqs: set[int] = set()

        # Service short_ids we've already surfaced a ``service_broken``
        # channel notice for. The workstation re-derives + re-delivers
        # break notifications from current state on every (re)subscribe,
        # so this set keeps a reconnect from re-spamming the agent.
        # Reset on session close (a fresh session can legitimately reuse
        # / re-encounter short_ids).
        self._notified_broken_services: set[str] = set()

        # Job queue. Created in :meth:`start` so we can size + type it
        # there; tools submit via ``self._job_send``.
        self._job_send: MemoryObjectSendStream[_Job] | None = None
        self._job_recv: MemoryObjectReceiveStream[_Job] | None = None

        # Per-request frame routing. Populated/cleared by the worker
        # via :meth:`_track_pending`; read by the reader.
        self._next_request_id_counter = itertools.count(1)
        self._next_update_id_counter = itertools.count(1)
        self._pending: dict[int, _PendingRequest] = {}

        # The per-session ``agent_mcp:{session_id}`` impl, mounted
        # by ``_agent_mcp_session_lifecycle`` after subscribe
        # completes. Read by the log builder (chunk E) so chain
        # extensions can call ``update_head``. ``None`` outside a
        # ready cycle, or for cycles where ``_active_session_id`` is
        # ``None``.
        self._agent_mcp_impl: AgentMcpRunningSessionImpl | None = None

        # Persistence cap ref looked up at bootstrap, held alongside
        # the impl. Read by the log builder (chunk E) to pin the
        # chain head on each extension. ``None`` if the toml
        # configured ``agent_mcp_store = false``.
        self._agent_mcp_persistence_ref: ServiceRef[PersistenceProto] | None = None

        # Cancel scope wrapping the agent_mcp lifecycle subtask.
        # Set by the subtask itself when it enters its
        # ``CancelScope``; cleared on exit. ``_do_close_session``
        # cancels this scope to tear down the agent_mcp service
        # before sending ``CloseOp``. ``None`` outside a
        # bootstrap-completed cycle.
        self._agent_mcp_cancel_scope: anyio.CancelScope | None = None

        # The inner task group from ``_run_inner`` — captured on
        # entry, cleared on exit. The worker uses this to start
        # the agent_mcp lifecycle subtask (which must outlive the
        # bootstrap job). ``None`` outside ``_run_inner``.
        self._inner_tg: anyio.abc.TaskGroup | None = None

        # Log-intent stream (chunk E). Created + held by the
        # ``_agent_mcp_session_lifecycle`` subtask once the impl
        # is ready; worker handlers push intents here as part of
        # their normal flow. ``None`` outside a bootstrap-completed
        # cycle — handlers skip the push.
        self._log_intent_send: "MemoryObjectSendStream[_LogIntent] | None" = None
        # Set by the writer task on exit. ``_do_close_session``
        # pushes ``_LogIntent_Shutdown`` and awaits this event so
        # the final log entry (the close mutation's
        # script-execution event, plus any pending events ahead of
        # the sentinel) lands before the agent_mcp subtask tears
        # down. ``None`` outside a writer-running cycle.
        self._log_writer_done: anyio.Event | None = None

    # --- Public read-only accessors ---------------------------------

    @property
    def rpc(self) -> WorkstationRpc:
        if self._rpc is None:
            raise RuntimeError("WorkstationConnection is not ready")
        return self._rpc

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    @property
    def state(self) -> SupervisorState:
        """Current supervisor state. Read-only outside the
        supervisor task."""
        return self._state

    @property
    def pause_reason(self) -> str | None:
        """Human-readable string explaining why the supervisor is
        in :attr:`SupervisorState.PAUSED` (or :attr:`EXITED`), or
        ``None`` when ``state`` is STARTING / READY."""
        return self._pause_reason

    @property
    def is_ready(self) -> bool:
        return self._state == SupervisorState.READY

    def peek_slot_key(self, slot_key: str) -> list[Any] | None:
        """Snapshot read of the local mirror's value for
        ``slot_key`` (the local view, including any unflushed
        non-state event effects), or ``None`` if absent.

        Sync; safe to call from any task. The mirror is mutated
        only by the worker, and anyio's single-threaded scheduling
        means the worker can't run in between this caller's sync
        operations — so the returned list is a consistent snapshot
        of the slot's value at call time.

        Used by ``wait_for_slot_notification`` to error early when
        the slot is absent or already in a terminal state (no
        future notification is expected)."""
        return self._mirror.data_by_key.get(slot_key)

    def peek_callback_notifications(self, slot_key: str) -> dict[str, list[Any]] | None:
        """Snapshot read of the local mirror's
        ``callback_notifications[slot_key]`` map — ``notif_index_str
        → wire_escaped_payload`` — or ``None`` if the slot has no
        entry (i.e. has never received a callback notification).

        Sync; safe to call from any task; same single-task-mutation
        argument as :meth:`peek_slot_key`. The returned dict is a
        shallow copy so callers can iterate it without worrying
        about concurrent mutation by the worker.

        Used by ``wait_for_slot_notification`` to detect callback
        notifications that landed during the agent's compose-window
        and short-circuit the wait. The returned dict may be empty
        if all notifications for this slot have been forgotten."""
        raw = self._mirror.callback_notifications.get(slot_key)
        if raw is None:
            return None
        return dict(raw)

    # --- Public lifecycle -------------------------------------------

    def start(self, tg: anyio.abc.TaskGroup) -> None:
        """Schedule the supervisor on ``tg``. Idempotent.

        The supervisor begins in :attr:`SupervisorState.PAUSED`
        with ``pause_reason = "(not started)"``; it does no work
        until :meth:`wake_and_wait_ready` is called.
        """
        if self._supervisor_started:
            return
        self._supervisor_started = True
        self._job_send, self._job_recv = anyio.create_memory_object_stream[_Job](max_buffer_size=64)
        tg.start_soon(self._supervisor_loop)

    async def wake_and_wait_ready(self) -> None:
        """Wake the supervisor (if paused) and wait until it
        reaches :attr:`SupervisorState.READY`.

        Signals :attr:`_retry_requested` at most once per call —
        on first observation of PAUSED. After that one retry, if
        the supervisor pauses again, this raises
        :class:`SupervisorNotReady` with the new ``pause_reason``
        as its message. If the supervisor has already EXITED,
        raises immediately.

        On successful return (state == READY), refreshes
        :attr:`_free_retry_budget` to
        :data:`MAX_FREE_RETRY_BUDGET`. This is the only place the
        budget refreshes — it's tied to "the agent actually got
        useful work done through this connection."

        The caller never mutates supervisor state directly; the
        ``state_changed`` event is captured *before* reading the
        state field so that a concurrent supervisor update either
        fires the event before we await it (we observe the new
        state immediately) or runs after our event capture (we
        wake on the fire). Standard condition-variable pattern.
        """
        requested_retry = False
        while True:
            # Capture the event reference BEFORE reading state so a
            # transition that happens after this point still wakes us.
            ev = self._state_changed
            state = self._state
            if state == SupervisorState.READY:
                self._free_retry_budget = MAX_FREE_RETRY_BUDGET
                return
            if state == SupervisorState.EXITED:
                raise SupervisorNotReady(self._pause_reason or "supervisor exited")
            if state == SupervisorState.PAUSED:
                if requested_retry:
                    raise SupervisorNotReady(self._pause_reason or "supervisor paused")
                self._retry_requested.set()
                requested_retry = True
            # STARTING (or PAUSED moments before the supervisor
            # picks up our retry signal); wait for next state
            # change and loop.
            await ev.wait()

    async def shutdown(self) -> None:
        """Signal the supervisor to exit. Waits for it to drain
        cleanly, with a generous timeout — if the supervisor is
        wedged, the lifespan TG's eventual ``cancel_scope.cancel()``
        kicks it out for sure."""
        self._shutdown.set()
        with anyio.move_on_after(5.0):
            await self._exited.wait()

    async def disconnect(self) -> str:
        """End the current supervisor cycle and park in PAUSED
        without closing the workstation-side session.

        Use cases:

        - Agent manually edited the toml (e.g. swapped
          ``active_session``) and wants a clean reconnect on the
          next tool call.
        - Something looks "off" with the cycle (cached proxy,
          stale state) and the agent wants a fresh start without
          restarting the MCP.

        Behavior:

        - If the supervisor is already :attr:`SupervisorState.PAUSED`
          or :attr:`SupervisorState.EXITED`, returns
          ``"Already disconnected."`` with no side effects.
        - Otherwise fires :attr:`_disconnect_requested`. The
          cycle's ``_run_inner`` task group is racing the
          ``_shutdown`` event against this one; on fire it
          cancels the inner TG, raises :class:`_DisconnectRequested`,
          and the supervisor's pause path routes to **PAUSED**
          with reason ``"disconnected by request"``. No
          ``supervisor_paused`` channel banner is emitted (this
          is agent-requested, not a crash).
        - In-flight tool jobs (e.g. a long ``run_script``) are
          torn down the same way a wire blip tears them down:
          the inner TG cancel propagates, their awaiters see
          :class:`ConnectionLost` (or cancellation if they were
          mid-await). The agent retries from the new cycle.

        Waits for the supervisor to actually leave READY /
        STARTING before returning, using the standard
        condition-variable pattern (capture ``_state_changed``
        *before* reading state).
        """
        if self._state in (SupervisorState.PAUSED, SupervisorState.EXITED):
            return "Already disconnected."
        # Capture state-change event ref before firing so a
        # transition between fire and wait still wakes us.
        while True:
            ev = self._state_changed
            state = self._state
            if state not in (SupervisorState.READY, SupervisorState.STARTING):
                return "Disconnected; next tool call will reconnect."
            # Only fire once; subsequent loop iterations just wait.
            if not self._disconnect_requested.is_set():
                self._disconnect_requested.set()
            await ev.wait()

    def notify_session_externally_closed(self) -> None:
        """Called by the force-close recovery path
        (:func:`close_session(force=True)`) after a one-shot
        websocket has closed the workstation-side session.

        Clears ``_active_session_id`` locally so the supervisor
        skips subscribing on its next cycle. If the supervisor is
        currently subscribed to that session, its existing
        subscription will get ``session_gone`` on the next
        operation; the supervisor pauses with the rejection
        reason. The next agent tool call wakes a fresh cycle that
        sees no active session and skips subscribe. Either way the
        supervisor converges to a clean no-active-session state.
        """
        self._active_session_id = None

    # --- Public submit methods --------------------------------------

    async def open_session(self, metadata: dict[str, Any]) -> str:
        """Create a fresh session, subscribe to it, and become its
        active subscription. Returns the new ``session_id``.

        Raises:
            SessionAlreadyActive: if a session is already active.
            ConnectionLost: if the connection drops mid-call.
            RuntimeError: for protocol-level failures.
        """
        await self.wake_and_wait_ready()
        fut: Future[str] = Future()
        await self._submit(_OpenSessionJob(metadata=metadata, completion=fut))
        return await fut.wait()

    async def close_session(self) -> None:
        """Close the active session via a single-op ``CloseOp``
        mutation. Clears active-session state on commit.

        Raises:
            NoActiveSession: if no session is active.
            ConnectionLost: if the connection drops mid-call.
            RuntimeError: for protocol-level failures (incl. the
                workstation rejecting the close mutation).
        """
        await self.wake_and_wait_ready()
        fut: Future[None] = Future()
        await self._submit(_CloseSessionJob(completion=fut))
        await fut.wait()

    async def run_script(
        self,
        *,
        preamble_src: str,
        script_src: str,
        env: DirectSandboxEnvironment,
        volumes: list[tuple[str, str, bool]],
        sys_path_additions: list[str],
        working_dir: Path,
    ) -> ScriptOutcome:
        """Submit a sandboxed-script job onto the worker queue and
        return its :class:`ScriptOutcome`.

        The worker handler owns the per-script ``script_scope``
        lifecycle, the per-script :class:`McpHostImpl` mount, and
        the sandbox open/close. The outcome carries the
        human-readable result string plus the list of slot_keys
        this script scheduled invokes on (used by the tool layer's
        optional ``wait_seconds`` knob to wait for results inline).

        Raises:
            ConnectionLost: if the connection drops mid-call.
            RuntimeError: for sandbox-execution failures (surfaced as
                the outcome's ``body`` in the success path; raised
                here only if the job future surfaces an exception).
        """
        await self.wake_and_wait_ready()
        fut: Future[ScriptOutcome] = Future()
        await self._submit(
            _ScriptJob(
                preamble_src=preamble_src,
                script_src=script_src,
                env=env,
                volumes=volumes,
                sys_path_additions=sys_path_additions,
                working_dir=working_dir,
                completion=fut,
            )
        )
        return await fut.wait()

    async def _submit(self, job: _Job) -> None:
        if self._job_send is None or not self._supervisor_started:
            raise RuntimeError("WorkstationConnection.start has not been called")
        try:
            await self._job_send.send(job)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as e:
            raise ConnectionLost("supervisor exited before the job could be submitted") from e

    # --- Supervisor body -------------------------------------------

    async def _supervisor_loop(self) -> None:
        """Top-level supervisor body. Owns the entire connection
        lifecycle in one task.

        The supervisor starts in :attr:`SupervisorState.PAUSED`
        with reason ``"(not started)"``; the first iteration waits
        on ``_retry_requested`` so no work happens until a tool
        calls :meth:`wake_and_wait_ready`. After each successful
        wake, the supervisor runs one open → subscribe →
        run-inner cycle. Cycle failures route through one of:

        - **Wire error after the cycle reached READY, budget > 0**:
          immediate retry (no banner, no state-change to PAUSED).
        - **Anything else**: enter PAUSED with a formatted
          ``pause_reason`` string, emit a ``supervisor_paused``
          channel banner, wait for the next wake.

        No non-cancellation exception ever escapes this task. The
        only ways out are shutdown (clean) or external
        cancellation from the lifespan TG.

        The job queue (``_job_send`` / ``_job_recv``) lives across
        cycles; pending request streams (``_pending``) and the
        active subscription_id are per-cycle. ``_active_session_id``
        persists so reconnects re-subscribe to whatever's current.
        """
        cancelled_exc_class = anyio.get_cancelled_exc_class()
        try:
            while not self._shutdown.is_set():
                if self._state == SupervisorState.PAUSED:
                    await self._wait_for_retry_or_shutdown()
                    if self._shutdown.is_set():
                        return
                    self._retry_requested = anyio.Event()

                self._reached_ready_this_cycle = False
                self._set_state(SupervisorState.STARTING, None)
                try:
                    await self._run_one_cycle()
                    # Inner returned cleanly = shutdown received.
                    return
                except BaseException as e:
                    if isinstance(e, cancelled_exc_class):
                        raise
                    self._reset_per_cycle_state_on_disconnect()
                    # Agent-initiated disconnect: route to PAUSED
                    # with the sentinel's message verbatim, and
                    # skip the supervisor_paused banner — the agent
                    # asked for this state, re-notifying would be
                    # noise. Wins over wire-blip and pause-routing
                    # logic; if disconnect was requested, that's
                    # the user's intent regardless of any other
                    # cycle-end cause.
                    disconnect_leaf = _find_disconnect_leaf(e)
                    if disconnect_leaf is not None:
                        reason = str(disconnect_leaf) or "disconnected by request"
                        _log.info("workstation supervisor cycle ended by disconnect request")
                        self._set_state(SupervisorState.PAUSED, reason)
                        continue
                    reason = _format_pause_reason(e)
                    wire = _is_wire_disconnect(e)
                    if wire and self._reached_ready_this_cycle and self._free_retry_budget > 0:
                        # Free retry: silent, no state change to
                        # PAUSED, no banner. The loop top will skip
                        # the park branch and run a fresh cycle.
                        self._free_retry_budget -= 1
                        _log.warning(
                            "workstation cycle dropped after READY (wire); " "free retry %d/%d: %s",
                            MAX_FREE_RETRY_BUDGET - self._free_retry_budget,
                            MAX_FREE_RETRY_BUDGET,
                            reason,
                        )
                        continue
                    # Pause: any non-wire crash, pre-ready failure,
                    # or wire failure with the budget exhausted.
                    _log.exception(
                        "workstation supervisor cycle failed; pausing: %s",
                        reason,
                    )
                    self._set_state(SupervisorState.PAUSED, reason)
                    await self._maybe_notify_supervisor_paused(reason)
                    # Top-of-loop park branch handles the wait on
                    # the next iteration.
        finally:
            self._rpc = None
            self._ws = None
            # Mark EXITED before draining queued jobs so any
            # concurrent ``wake_and_wait_ready`` calls see the
            # terminal state and raise rather than wait forever.
            self._set_state(
                SupervisorState.EXITED,
                self._pause_reason or "lifespan exited",
            )
            self._drain_pending_jobs_and_requests()
            self._exited.set()

    async def _run_one_cycle(self) -> None:
        """One open → subscribe → run-inner pass. Re-entered by
        :meth:`_supervisor_loop` after a wire disconnect. Clears
        ``_rpc`` / ``_ws`` references on exit so consumers don't
        observe stale handles between cycles.

        Refreshes ``_active_session_id`` from the toml at the very
        top (when ``working_dir`` was supplied at construction), so
        manual edits to the toml's ``active_session`` line take
        effect on the next cycle without restarting the MCP."""
        self._refresh_config_from_disk()
        try:
            async with _open_workstation_rpc(self._rpc_url, name=self._connection_name) as rpc:
                self._rpc = rpc
                await self._name_connection_via_rpc(rpc, self._connection_name)
                async with aconnect_ws(self._client_url) as ws:
                    self._ws = ws
                    await self._run_inner()
        finally:
            self._rpc = None
            self._ws = None

    def _refresh_config_from_disk(self) -> None:
        """Re-parse ``.fxtr_workstation.toml`` from
        ``self._working_dir`` and refresh per-cycle config fields.

        Only ``_active_session_id`` is refreshed today (so manual
        edits to clear or change ``active_session`` take effect on
        the next cycle — the canonical recovery path when a session
        UUID becomes stale). Other fields (``workstation_url``,
        ``agent_mcp_store``) intentionally are not refreshed: the
        URLs are baked into ``self._rpc_url`` / ``self._client_url``
        at construction, and switching them mid-process would
        require a much bigger rebuild.

        No-op when ``working_dir`` is ``None`` (test paths that
        don't go through ``set_working_dir``).

        Raises :class:`ConfigInvalid` if the toml is missing or
        malformed — the supervisor's pause path catches it and
        surfaces the underlying ``ConfigError`` message as
        ``pause_reason``."""
        if self._working_dir is None:
            return
        try:
            self._active_session_id = read_active_session(self._working_dir)
        except ConfigError as e:
            raise ConfigInvalid(str(e)) from e

    async def _run_inner(self) -> None:
        """Run the reader and worker subtasks under an inner task
        group. If ``_active_session_id`` is set (from
        ``initial_session_id`` on first cycle, or from the
        previous cycle's open/close session bookkeeping), subscribe
        to it before starting the worker and transitioning to
        :attr:`SupervisorState.READY`. Park on the shutdown event;
        cancel the inner TG when shutdown fires.

        A failed subscribe (e.g. ``session_gone``) raises into the
        inner TG, which propagates the exception out — the
        supervisor's pause path catches it and routes to PAUSED.
        Wire-level errors (``ConnectionLost`` /
        ``HTTPXWSException``) propagate the same way and are
        handled by the free-retry path if the cycle had reached
        READY (and there's budget remaining).

        Subtask ordering: the reader starts first because subscribe
        needs the per-request stream plumbing routed by the reader.
        The worker starts *after* subscribe completes (and
        ``_active_subscription_id`` is populated) so queued jobs
        from a prior cycle's disconnect can't be drained against a
        not-yet-subscribed connection.
        """
        async with anyio.create_task_group() as inner_tg:
            self._inner_tg = inner_tg
            try:
                inner_tg.start_soon(self._reader_loop)

                bootstrap_future: Future[None] | None = None
                if self._active_session_id is not None:
                    await self._do_subscribe(self._active_session_id)
                    # Submit the bootstrap as the next worker job.
                    # It lands behind any ``_EventJob``s the reader
                    # queued during _do_subscribe (the snapshot's
                    # ``subsequent_events``); the worker drains them
                    # first (advancing ``_last_processed_seq``), then
                    # runs the bootstrap. We await the bootstrap's
                    # completion future before transitioning to READY
                    # so frontends never see a partially-ready cycle.
                    bootstrap_future = Future()
                    await self._submit(
                        _BootstrapAgentMcpJob(
                            session_id=self._active_session_id,
                            completion=bootstrap_future,
                        )
                    )

                inner_tg.start_soon(self._worker_loop)

                if bootstrap_future is not None:
                    # Surfaces any bootstrap failure as an exception
                    # here, which propagates out of _run_inner →
                    # supervisor's pause path → PAUSED with the
                    # formatted reason. (The job handler also
                    # re-raises into the worker task, which would
                    # tear down the inner_tg anyway; awaiting the
                    # future gives us the exception type-preserved.)
                    await bootstrap_future.wait()
                # Subscribe (if applicable) completed; mark READY so
                # tools waiting on ``wake_and_wait_ready`` unblock.
                # The ``_reached_ready_this_cycle`` flag gates the
                # supervisor's wire-error free-retry path.
                self._reached_ready_this_cycle = True
                self._set_state(SupervisorState.READY, None)
                # Race shutdown against agent-initiated disconnect.
                # Whichever fires first cancels the wait task group,
                # so the other branch unblocks immediately. The post-
                # wait check decides whether to raise the disconnect
                # sentinel (cycle teardown → PAUSED) or fall through
                # to a clean shutdown (cycle teardown → return).
                async with anyio.create_task_group() as wait_tg:

                    async def _waiter(event: anyio.Event) -> None:
                        await event.wait()
                        wait_tg.cancel_scope.cancel()

                    wait_tg.start_soon(_waiter, self._shutdown)
                    wait_tg.start_soon(_waiter, self._disconnect_requested)
                if self._disconnect_requested.is_set():
                    # Replace the event with a fresh one so a later
                    # disconnect() call after a successful retry
                    # cycle isn't pre-fired. Then raise the
                    # sentinel — the supervisor's pause path
                    # recognizes it and routes to PAUSED without a
                    # banner.
                    self._disconnect_requested = anyio.Event()
                    raise _DisconnectRequested("disconnected by request")
                inner_tg.cancel_scope.cancel()
            finally:
                self._inner_tg = None

    async def _name_connection_via_rpc(self, rpc: WorkstationRpc, name: str) -> None:
        await rpc.workstation_proxy.name_connection(name)

    def _reset_per_cycle_state_on_disconnect(self) -> None:
        """Reconnect-time cleanup: reset the cycle-scoped fields
        and defensively close any leaked per-request streams.

        We deliberately do **not** drain queued jobs — tool calls
        submitted during the reconnect window stay in the queue,
        and the next cycle's worker picks them up after the new
        subscribe succeeds. In-flight jobs (whose handler was
        running when the wire dropped) fail via
        :meth:`_dispatch_job`'s ``BaseException`` path, which
        surfaces a "job aborted by Cancelled" error to their
        submitters when the inner task group cancels the worker.

        Pending per-request streams are normally cleaned up by
        ``_track_pending``'s context exit and the reader's
        ``finally`` block; we close + clear here as defense in
        depth in case a cleanup path was interrupted.
        """
        for entry in self._pending.values():
            entry.send.close()
        self._pending.clear()
        # The active subscription_id is per-cycle. The next
        # subscribe (if ``_active_session_id`` is still set) will
        # mint a fresh one.
        self._active_subscription_id = None

    def _drain_pending_jobs_and_requests(self) -> None:
        """Cleanup path, run from the supervisor's outermost finally.

        - Closes the job-send side: any future ``_submit`` raises
          ``ConnectionLost``.
        - Drains anything still queued and fails each tool-call
          job's completion future with ``ConnectionLost`` so the
          awaiter sees a clean error instead of hanging. Reader-
          pushed jobs (``_EventJob``) have no completion future and
          are simply discarded.
        - Closes pending per-request send streams so any
          worker-internal awaits surface ``ConnectionLost`` via
          ``EndOfStream``.

        Subtasks deliberately do *not* close these streams via
        ``async with`` — the supervisor owns the lifecycle so this
        cleanup is the single place each stream is closed.
        """
        terminal_exc = (
            anyio.WouldBlock,
            anyio.EndOfStream,
            anyio.ClosedResourceError,
        )
        if self._job_send is not None:
            self._job_send.close()
        if self._job_recv is not None:
            lost = ConnectionLost("supervisor exited; job not processed")
            while True:
                try:
                    job = self._job_recv.receive_nowait()
                except terminal_exc:
                    break
                # Tool-call jobs surface the error on their
                # completion future. Reader-pushed jobs
                # (``_EventJob`` / ``_AckCommitJob``) have no
                # submitter to notify; drop them.
                match job:
                    case _OpenSessionJob() | _CloseSessionJob() | _ScriptJob():
                        job.completion.set_exception(lost)
                    case _BootstrapAgentMcpJob():
                        job.completion.set_exception(lost)
                    case _EventJob() | _AckCommitJob() | _ServiceBrokenJob():
                        pass
            self._job_recv.close()
        for entry in self._pending.values():
            entry.send.close()
        self._pending.clear()

    # --- Reader subtask ---------------------------------------------

    async def _reader_loop(self) -> None:
        """Continuously pull frames off the websocket and route them.

        Routing rules:
        - Frames that correlate with a pending request_id (or
          subscription_id, for ``SessionSnapshotEvent`` /
          ``SessionDebugSnapshotEvent`` whose sub_id by protocol
          equals the originating subscribe's request_id) → push to
          that pending entry's send stream.
        - ``SessionEventFrame`` whose ``subscription_id`` matches our
          active subscription → push onto the long-lived session
          event stream (consumed by the event consumer subtask).
        - Anything else → log + drop.

        EOF / error exits the reader, which cancels the inner TG.
        Final cleanup (closing pending streams) lives in
        :meth:`_supervisor_loop`'s finally block, which runs once
        per supervisor lifetime.
        """
        assert self._ws is not None
        ws = self._ws
        try:
            while True:
                try:
                    text = await ws.receive_text()
                except (httpx_ws.HTTPXWSException, anyio.EndOfStream) as e:
                    raise ConnectionLost(f"workstation /client connection ended: {e!r}") from e
                try:
                    frame = _SERVER_FRAME_DECODER.decode(text)
                except msgspec.ValidationError:
                    _log.warning("ws_client: dropped malformed server frame: %r", text)
                    continue
                self._route_frame(frame)
        finally:
            # Wake every awaiter on a pending request stream — they
            # see ``EndOfStream`` and surface ``ConnectionLost``.
            for entry in self._pending.values():
                entry.send.close()

    def _route_frame(self, frame: ServerFrame) -> None:
        match frame:
            case AckResponse() | ErrorResponse():
                self._send_to_pending(frame.request_id, frame)
            case SessionCreatedResponse():
                self._send_to_pending(frame.request_id, frame)
            case MutationCommittedResponse() | MutationRejectedResponse():
                self._send_to_pending(frame.request_id, frame)
            case GetCallbackServiceResponse():
                self._send_to_pending(frame.request_id, frame)
            case SessionSnapshotEvent() | SessionDebugSnapshotEvent():
                self._send_to_pending(frame.subscription_id, frame)
            case ResourceSnapshotEvent() | ResourceChangesEvent():
                # Resource subscriptions aren't used by the MCP for
                # now — but the routing still wants to land somewhere
                # if a future feature subscribes.
                self._send_to_pending(frame.subscription_id, frame)
            case SessionEventFrame():
                if (
                    self._active_subscription_id is not None
                    and frame.subscription_id == self._active_subscription_id
                ):
                    if self._job_send is None:
                        return  # supervisor on its way out
                    try:
                        self._job_send.send_nowait(_EventJob(frame=frame))
                    except anyio.WouldBlock:
                        _log.error("ws_client: worker queue full; dropping session event frame")
                    except anyio.ClosedResourceError:
                        pass
                else:
                    _log.debug(
                        "ws_client: dropping SessionEventFrame for unknown subscription_id=%d",
                        frame.subscription_id,
                    )
            case ServiceBrokenNotification():
                # Out-of-band side channel (no seq, not a reducer
                # event). Route through the worker so it serializes with
                # other agent notifications and the dedup set is touched
                # single-threaded.
                if (
                    self._active_subscription_id is not None
                    and frame.subscription_id == self._active_subscription_id
                ):
                    if self._job_send is None:
                        return  # supervisor on its way out
                    try:
                        self._job_send.send_nowait(
                            _ServiceBrokenJob(service_short_id=frame.service_short_id)
                        )
                    except anyio.WouldBlock:
                        _log.error("ws_client: worker queue full; dropping service-broken frame")
                    except anyio.ClosedResourceError:
                        pass
                else:
                    _log.debug(
                        "ws_client: dropping ServiceBrokenNotification for unknown "
                        "subscription_id=%d",
                        frame.subscription_id,
                    )
            case _:
                _log.warning("ws_client: unhandled server frame type: %r", frame)

    def _send_to_pending(self, request_id: int, frame: ServerFrame) -> None:
        entry = self._pending.get(request_id)
        if entry is None:
            _log.debug(
                "ws_client: no pending request for id=%d (frame=%r)",
                request_id,
                frame,
            )
            return
        try:
            entry.send.send_nowait(frame)
        except anyio.WouldBlock:
            _log.error(
                "ws_client: pending request %d buffer full (frame dropped)",
                request_id,
            )
        except anyio.ClosedResourceError:
            pass

    # --- Worker subtask ---------------------------------------------

    async def _worker_loop(self) -> None:
        """Drain the job queue, dispatching each job to its
        per-kind ``_do_*`` implementation. Per-job exceptions are
        captured on ``job.completion`` so submitters get them.

        Exits cleanly when the supervisor closes the job-send side
        (``async for`` raises ``EndOfStream`` after queue drains).
        Cancellation interrupts the iterator; the supervisor's
        ``_drain_pending_jobs_and_requests`` handles any queued
        jobs that didn't get processed.

        Notably, the worker does *not* ``async with`` the receiver
        — the supervisor owns the lifecycle so cancellation
        followed by drain works without hitting
        ``ClosedResourceError``.
        """
        assert self._job_recv is not None
        async for job in self._job_recv:
            await self._dispatch_job(job)

    async def _dispatch_job(self, job: _Job) -> None:
        """Bare per-job dispatch. Any exception propagating out
        tears down the inner task group; the supervisor's outer
        retry loop reconnects on any post-ready ``Exception``
        (wire or otherwise), so a propagated bug becomes a
        reconnect blip rather than an MCP crash.

        Tool-call handlers (:meth:`_handle_open_session_job` /
        :meth:`_handle_close_session_job` /
        :meth:`_handle_script_job`) absorb ``Exception`` onto the
        completion future and return — the worker keeps draining,
        no reconnect needed. They only re-raise ``BaseException``
        (cancellation / ``SystemExit``), which propagates to tear
        down cleanly.

        Non-tool-call jobs (``_EventJob`` / ``_AckCommitJob``) run
        their bodies directly; if they raise, the supervisor's
        safety net reconnects + rebuilds local state from a fresh
        subscribe-time snapshot.
        """
        match job:
            case _OpenSessionJob():
                await self._handle_open_session_job(job)
            case _CloseSessionJob():
                await self._handle_close_session_job(job)
            case _ScriptJob():
                await self._handle_script_job(job)
            case _EventJob():
                await self._do_event_job(job)
            case _AckCommitJob():
                await self._do_ack_commit_job(job)
            case _ServiceBrokenJob():
                await self._do_service_broken_job(job)
            case _BootstrapAgentMcpJob():
                await self._do_bootstrap_agent_mcp_job(job)

    # All three per-tool-call handlers follow the same two-bucket
    # pattern:
    #
    # - ``Exception`` (whether expected tool-error like
    #   ``SessionAlreadyActive`` / ``WorkstationRejected`` /
    #   ``NoActiveSession``, or unexpected internal error like
    #   ``ProtocolError`` / ``NotImplementedError`` / a bug in
    #   the job body): set on the completion future and return.
    #   The tool's ``await`` surfaces the exception to the agent
    #   as a normal tool-level failure; the worker keeps draining.
    #   The supervisor's safety net (post-#2) would catch this
    #   even if we re-raised, but there's no reason to subject the
    #   agent to a reconnect blip for what's really just a bad
    #   call.
    #
    # - ``BaseException`` (cancellation, ``SystemExit``,
    #   ``KeyboardInterrupt``): wrap in a ``RuntimeError`` so the
    #   awaiter sees a clean error rather than hanging, then
    #   re-raise. Re-raising lets cancellation propagate to tear
    #   down the supervisor cleanly; the wrapping is defensive in
    #   case the awaiter survives the cancellation propagation
    #   (e.g. anyio shielded scopes) and still needs a result.

    async def _handle_open_session_job(self, job: _OpenSessionJob) -> None:
        """Tool-call wrapper around :meth:`_do_open_session`."""
        try:
            session_id = await self._do_open_session(job.metadata)
        except Exception as exc:
            job.completion.set_exception(exc)
            return
        except BaseException as exc:
            job.completion.set_exception(RuntimeError(f"job interrupted: {exc!r}"))
            raise
        job.completion.set_result(session_id)

    async def _handle_close_session_job(self, job: _CloseSessionJob) -> None:
        """Tool-call wrapper around :meth:`_do_close_session`."""
        try:
            await self._do_close_session()
        except Exception as exc:
            job.completion.set_exception(exc)
            return
        except BaseException as exc:
            job.completion.set_exception(RuntimeError(f"job interrupted: {exc!r}"))
            raise
        job.completion.set_result(None)

    async def _handle_script_job(self, job: _ScriptJob) -> None:
        """Tool-call wrapper around :meth:`_do_script_job`."""
        try:
            result = await self._do_script_job(job)
        except Exception as exc:
            job.completion.set_exception(exc)
            return
        except BaseException as exc:
            job.completion.set_exception(RuntimeError(f"job interrupted: {exc!r}"))
            raise
        job.completion.set_result(result)

    # --- Per-job implementations -----------------------------------

    async def _do_open_session(self, metadata: dict[str, Any]) -> str:
        if self._active_session_id is not None:
            raise SessionAlreadyActive(f"a session is already active: {self._active_session_id}")

        # Phase 1: create.
        req_id = self._next_request_id()
        with self._track_pending(req_id) as receive:
            await self._send_request(CreateSessionRequest(request_id=req_id, metadata=metadata))
            frame = await self._expect(receive)
            if isinstance(frame, ErrorResponse):
                raise WorkstationRejected(f"create_session failed: {frame.message}")
            if not isinstance(frame, SessionCreatedResponse):
                raise ProtocolError(f"unexpected response to create_session: {frame!r}")
            session_id = frame.session_id

        # Phase 2: subscribe.
        await self._do_subscribe(session_id)

        # Phase 3: bootstrap agent_mcp.
        # We're already inside the worker (this method is invoked
        # from ``_handle_open_session_job``), so we can't submit a
        # ``_BootstrapAgentMcpJob`` + await — the worker only
        # processes one job at a time, that would deadlock. Instead
        # call the inner helper directly; it does the same work in
        # the same worker context.
        #
        # Seq ordering w.r.t. ``subsequent_events``: the helper's
        # bootstrap mutation submits an ``_AckCommitJob(seq=N)``
        # onto the queue *behind* any ``_EventJob``s the reader
        # queued during subscribe. The worker (us) returns after
        # this method exits and resumes draining: subsequent_events
        # advance ``_last_processed_seq`` to their seqs, then the
        # AckCommitJob lifts it to our commit seq. The bootstrap's
        # outgoing ack_seq is the pre-drain value (last_state.seq),
        # which the workstation accepts — it doesn't validate ack
        # ordering against events still in-flight to us.
        await self._do_bootstrap_agent_mcp_inner(session_id)
        return session_id

    async def _do_subscribe(self, session_id: str) -> None:
        """Subscribe to ``session_id`` and rebuild local state from
        the snapshot. Used for both initial subscribe and reconnect.

        Steps (per stage 3-post-2):

        1. Send the subscribe request, await the ack + snapshot
           frame.
        2. Throw away all per-session local state. The workstation's
           snapshot is authoritative; whatever we had locally is
           either (a) reflected in ``snapshot.last_state.state``
           (foreign mutator promoted unflushed entries on our
           behalf) or (b) present in
           ``snapshot.subsequent_events`` (events we'd received
           but hadn't flushed; the workstation kept them past the
           ack horizon since no successful mutation moved
           ``ack_seq`` past them). Either way we'll re-derive.
        3. Validate + log+skip ``preserved_events`` — they're
           subsumed by ``last_state.state`` (their effects are
           already promoted) so they don't need replay. Future
           stages may surface them as a debug history.
        4. Apply ``last_state`` via ``_handle_state_update`` (the
           same code path used for stream state_updates).
        5. Enqueue ``subsequent_events`` as ``_EventJob``s in seq
           order. They drain through the same per-kind arms as
           stream events; each runs ``_apply_state_effect``,
           which writes the unflushed entry that captured our
           pre-disconnect promotion *and* fires the channel
           notification (deduped by ``_notified_seqs``, so seqs
           the agent already saw before the disconnect are
           suppressed).

        The fresh-subscribe and reconnect paths are identical —
        no special leftover-state logic, no risk of stale data
        carrying across a disconnect.
        """
        sub_id = self._next_request_id()
        with self._track_pending(sub_id) as receive:
            await self._send_request(
                SubscribeSessionRequest(request_id=sub_id, session_id=session_id)
            )
            ack = await self._expect(receive)
            if isinstance(ack, ErrorResponse):
                raise WorkstationRejected(f"subscribe_session({session_id}) failed: {ack.message}")
            if not isinstance(ack, AckResponse):
                raise ProtocolError(f"unexpected first frame on subscribe: {ack!r}")
            snapshot = await self._expect(receive)
            if not isinstance(snapshot, SessionSnapshotEvent):
                raise ProtocolError(f"unexpected second frame on subscribe: {snapshot!r}")

        # Step 2: wipe local state.
        # Note: ``_notified_seqs`` is NOT wiped here. It's wiped only
        # in ``_do_close_session`` (session lifecycle ends) and at
        # connection construction. Keeping it across reconnects is
        # the whole point of the dedupe — re-delivered events from
        # ``snapshot.subsequent_events`` get suppressed if we
        # already notified the agent in the previous cycle. The
        # subsequent prune in ``_handle_state_update`` drops any
        # entries past the new ack horizon, so growth stays bounded.
        self._mirror = SessionMirror()
        self._replayable_events = []
        self._pending_changes = []
        self._last_processed_seq = 0
        self._slot_counter = 0
        self._active_session_id = session_id
        self._active_subscription_id = sub_id

        # Step 3: log+skip preserved_events.
        last_ack = snapshot.last_state.ack_seq
        for ev in snapshot.preserved_events:
            if ev.seq > last_ack:
                raise ProtocolError(
                    f"snapshot.preserved_events contained event seq={ev.seq} "
                    f"above last_state.ack_seq={last_ack}"
                )
            _log.debug(
                "ws_client: subscribe-time preserved event seq=%d kind=%s; skipping",
                ev.seq,
                type(ev).__name__,
            )

        # Step 4: apply last_state through the unified handler,
        # then inline-advance _last_processed_seq (the helper
        # doesn't touch it; subscribe is the inline-advance path).
        await self._handle_state_update(snapshot.last_state)
        self._last_processed_seq = max(self._last_processed_seq, snapshot.last_state.seq)

        # Step 5: enqueue subsequent_events as _EventJobs. They run
        # in seq order through the worker's normal job-dispatch
        # path, each one advancing _last_processed_seq in turn.
        if self._job_send is None:
            return  # supervisor on its way out
        for ev in snapshot.subsequent_events:
            if ev.seq <= last_ack:
                raise ProtocolError(
                    f"snapshot.subsequent_events contained event seq={ev.seq} "
                    f"at or below last_state.ack_seq={last_ack}"
                )
            self._job_send.send_nowait(
                _EventJob(
                    frame=SessionEventFrame(subscription_id=sub_id, event=ev),
                )
            )

    async def _do_close_session(self) -> None:
        if self._active_session_id is None or self._active_subscription_id is None:
            raise NoActiveSession("no active session to close")

        # Drain the log-intent stream before tearing down the
        # agent_mcp subtask. Push a ``_LogIntent_Shutdown`` sentinel
        # and wait for the writer to drain everything queued behind
        # it — without this, any in-flight log intents (script
        # executions from before close, callback notifications that
        # arrived during the close-mutation's await window, etc.)
        # would be cancelled with the subtask and lost.
        #
        # Order:
        # (1) push sentinel + await drain
        # (2) cancel the lifecycle subtask (now safe; writer has
        #     already exited cleanly, so its impl_tg drains
        #     instantly; ``expose_service`` releases; impl_scope
        #     drains)
        # (3) send ``CloseOp``
        if self._log_intent_send is not None and self._log_writer_done is not None:
            log_send = self._log_intent_send
            writer_done = self._log_writer_done
            try:
                log_send.send_nowait(_LogIntent_Shutdown())
            except anyio.BrokenResourceError:
                # Writer task already gone (cycle teardown raced
                # us). Nothing to drain.
                pass
            await writer_done.wait()
        if self._agent_mcp_cancel_scope is not None:
            self._agent_mcp_cancel_scope.cancel()

        req_id = self._next_request_id()
        update_id = next(self._next_update_id_counter)
        req = MutateSessionRequest(
            request_id=req_id,
            subscription_id=self._active_subscription_id,
            ack_seq=self._last_processed_seq,
            update_id=update_id,
            patch=[],
            ops=[CloseOp()],
        )
        with self._track_pending(req_id) as receive:
            await self._send_request(req)
            frame = await self._expect(receive)
            if isinstance(frame, MutationRejectedResponse):
                raise WorkstationRejected(
                    f"close-mutation rejected: {frame.reason}: {frame.message}"
                )
            if isinstance(frame, ErrorResponse):
                raise WorkstationRejected(f"close-mutation transport error: {frame.message}")
            if not isinstance(frame, MutationCommittedResponse):
                raise ProtocolError(f"unexpected response to close-mutation: {frame!r}")

        self._active_session_id = None
        self._active_subscription_id = None
        self._last_processed_seq = 0
        self._mirror = SessionMirror()
        self._replayable_events = []
        self._pending_changes = []
        self._slot_counter = 0
        self._notified_seqs = set()
        self._notified_broken_services = set()

    # --- Agent MCP bootstrap (worker-queued, runs after subscribe) ---

    async def _do_bootstrap_agent_mcp_job(self, job: _BootstrapAgentMcpJob) -> None:
        """Worker handler for :class:`_BootstrapAgentMcpJob`.

        Runs the seven bootstrap steps inside the worker context so
        the mutation goes through the standard
        ``_apply_local_commit`` + ``_AckCommitJob`` flow and any
        ``_EventJob``s queued behind us drain in order.

        On success: sets ``self._agent_mcp_impl`` /
        ``self._agent_mcp_persistence_ref`` (via the spawned
        lifecycle subtask) and resolves the future.

        On failure: surfaces via the future *and* re-raises so the
        worker's bare-dispatch teardown fires → cycle pauses with
        the formatted reason. A failed bootstrap means the cycle
        isn't properly connected; we don't want it to continue
        with a broken agent_mcp surface.
        """
        try:
            await self._do_bootstrap_agent_mcp_inner(job.session_id)
        except Exception as exc:
            job.completion.set_exception(exc)
            raise
        except BaseException as exc:
            # Cancellation / SystemExit — surface as a synthesized
            # exception on the future so the awaiter sees something,
            # then propagate.
            job.completion.set_exception(RuntimeError(f"bootstrap interrupted: {exc!r}"))
            raise
        job.completion.set_result(None)

    async def _do_bootstrap_agent_mcp_inner(self, session_id: str) -> None:
        """Resolve persistence + workstation_inbox + inbox ref +
        chain head, then ``inner_tg.start(...)`` the long-lived
        :meth:`_agent_mcp_session_lifecycle` subtask.

        Separated from :meth:`_do_bootstrap_agent_mcp_job` so the
        worker handler can wrap exceptions cleanly.
        """
        assert self._rpc is not None
        assert self._inner_tg is not None
        rpc = self._rpc

        # Step 1: persistence cap (optional).
        persistence_ref: ServiceRef[PersistenceProto] | None = None
        if self._agent_mcp_store is not None:
            try:
                cap_short_id = await rpc.workstation_proxy.lookup_short_id_by_external_name(
                    self._agent_mcp_store
                )
                cap_untyped = await rpc.workstation_proxy.get_service(cap_short_id)
            except Exception as exc:
                raise WorkstationRejected(
                    f"agent_mcp_store service {self._agent_mcp_store!r} not "
                    f"found on workstation: {exc}"
                ) from exc
            persistence_ref = cap_untyped.replace_service_type(PersistenceProto)

        # Step 2: workstation_inbox callback (mutation goes through
        # the standard apply-local-commit + AckCommitJob flow).
        inbox_slot_n = await self._bootstrap_workstation_inbox()

        # Step 3: inbox callback's service short_id.
        inbox_short_id = await self._get_callback_service_short_id(inbox_slot_n)

        # Step 4: inbox ref. Handle is registered in ``rpc.host_scope``
        # (the dispatch scope of the get_service call). The
        # ``impl_scope`` opened by the lifecycle subtask includes
        # host_scope, so the impl can ``.resolve(scope=impl_scope)``
        # it via inheritance.
        inbox_untyped = await rpc.workstation_proxy.get_service(inbox_short_id)
        inbox_ref = inbox_untyped.replace_service_type(WorkstationSessionEventInbox)

        # Step 5: prior chain head from persistence.
        initial_head: EntityRef[WsAgentLogEventChain] | None = None
        if persistence_ref is not None:
            # Resolve through rpc.host_scope (the persistence ref's
            # registered scope); the FxtrID returned chain-bridges
            # back into our scope.
            persistence_proxy = await persistence_ref.resolve(scope=rpc.host_scope)
            head_fid = await persistence_proxy.try_retrieve(session_id)
            if head_fid is not None:
                initial_head = EntityRef(entity_id=head_fid, target_type=WsAgentLogEventChain)

        # Steps 6-7: spawn the long-lived subtask. ``start`` blocks
        # until the subtask calls ``task_status.started()``, which
        # happens once the impl is mounted + expose_service is open
        # — i.e. once the agent_mcp service is reachable.
        await self._inner_tg.start(
            self._agent_mcp_session_lifecycle,
            session_id,
            persistence_ref,
            inbox_ref,
            initial_head,
        )

    # --- Helpers used by the bootstrap job ----------------------------

    async def _bootstrap_workstation_inbox(self) -> int:
        """Ensure ``data_by_key["workstation_inbox"]`` holds a
        ``["callback_open", N]`` marker; return ``N``.

        Reused across cycles: if the slot already exists in the
        mirror (loaded from a snapshot of a previously-created
        callback), return its existing slot_n. Otherwise send a
        one-op ``create_callback`` mutation against the current
        subscription, manually apply the local commit, and return
        the new slot_n.

        Runs on the supervisor task, after ``_do_subscribe`` and
        before the worker starts. ``_pending_changes`` /
        ``_replayable_events`` are typically empty at this point (a
        fresh subscribe wipes them); we leverage
        :func:`build_mutate_request` to construct a well-formed
        mutation including any synthetic ``initialize`` prologue.

        Raises :class:`WorkstationRejected` on workstation rejection
        or :class:`ProtocolError` on unexpected response shapes.
        """
        existing = self._mirror.data_by_key.get("workstation_inbox")
        if existing is not None:
            if not (
                isinstance(existing, list)
                and len(existing) == 2
                and existing[0] == CALLBACK_OPEN_TAG
                and isinstance(existing[1], int)
            ):
                raise WorkstationRejected(
                    f"workstation_inbox slot is occupied by non-callback " f"value: {existing!r}"
                )
            return existing[1]

        assert self._active_subscription_id is not None
        build_result = build_mutate_request(
            mirror=self._mirror,
            pending_changes=self._pending_changes,
            runner_queued_ops=[
                {
                    "kind": KIND_CREATE_CALLBACK,
                    "slot_key": "workstation_inbox",
                }
            ],
            entity_short_ids={},
            slot_counter=self._slot_counter,
            ack_seq=self._last_processed_seq,
            request_id=self._next_request_id(),
            subscription_id=self._active_subscription_id,
            update_id=next(self._next_update_id_counter),
            # Bypass the reserved-slot check — this internal
            # mutation is the one that creates the reserved slot.
            allow_reserved_slot_keys=True,
        )
        assert (
            build_result is not None
        ), "workstation_inbox create_callback should never produce a no-op mutation"
        req = build_result.request
        with self._track_pending(req.request_id) as receive:
            await self._send_request(req)
            frame = await self._expect(receive)
            if isinstance(frame, MutationRejectedResponse):
                raise WorkstationRejected(
                    f"workstation_inbox create rejected: " f"{frame.reason}: {frame.message}"
                )
            if isinstance(frame, ErrorResponse):
                raise WorkstationRejected(
                    f"workstation_inbox create transport error: {frame.message}"
                )
            if not isinstance(frame, MutationCommittedResponse):
                raise ProtocolError(f"unexpected response to workstation_inbox create: {frame!r}")

        self._apply_local_commit(build_result, frame)
        # Submit an _AckCommitJob for the seq advance — same flow as
        # _apply_script_mutation. This bootstrap helper runs *inside*
        # a worker job (either _BootstrapAgentMcpJob or
        # _OpenSessionJob), so submitting onto the queue is correct:
        # any foreign _EventJobs queued during our send/await drain
        # first, then the AckCommitJob advances ``_last_processed_seq``
        # past our own commit.
        #
        # ``silent=True`` suppresses the user-facing ``self_commit``
        # channel notification — this is an MCP-internal mutation,
        # not something the agent script ran.
        await self._submit(_AckCommitJob(seq=frame.seq, silent=True))

        new_value = self._mirror.data_by_key.get("workstation_inbox")
        if not (
            isinstance(new_value, list)
            and len(new_value) == 2
            and new_value[0] == CALLBACK_OPEN_TAG
            and isinstance(new_value[1], int)
        ):
            raise ProtocolError(
                f"workstation_inbox slot post-commit has unexpected value: {new_value!r}"
            )
        return new_value[1]

    async def _get_callback_service_short_id(self, slot: int) -> str:
        """Issue a :class:`GetCallbackServiceRequest` for ``slot`` and
        return the workstation short_id of the callback's send-side
        :class:`WorkstationSessionEventInbox` service.

        Raises :class:`WorkstationRejected` if the workstation returns
        an :class:`ErrorResponse`; :class:`ProtocolError` on unexpected
        response shape.
        """
        assert self._active_subscription_id is not None
        req_id = self._next_request_id()
        req = GetCallbackServiceRequest(
            request_id=req_id,
            subscription_id=self._active_subscription_id,
            slot=slot,
        )
        with self._track_pending(req_id) as receive:
            await self._send_request(req)
            frame = await self._expect(receive)
            if isinstance(frame, ErrorResponse):
                raise WorkstationRejected(
                    f"GetCallbackServiceRequest failed for slot={slot}: " f"{frame.message}"
                )
            if not isinstance(frame, GetCallbackServiceResponse):
                raise ProtocolError(f"unexpected response to GetCallbackServiceRequest: {frame!r}")
            return frame.service_short_id

    async def _agent_mcp_session_lifecycle(
        self,
        session_id: str,
        persistence_ref: "ServiceRef[PersistenceProto] | None",
        inbox_ref: "ServiceRef[WorkstationSessionEventInbox]",
        initial_head: "EntityRef[WsAgentLogEventChain] | None",
        *,
        task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
    ) -> None:
        """Long-lived per-session subtask. Owns the agent_mcp
        impl_scope + impl_tg + the workstation-side
        ``agent_mcp:{session_id}`` registration.

        Started by :meth:`_do_bootstrap_agent_mcp_job` via
        ``await self._inner_tg.start(...)``, *after* that job has
        resolved the persistence cap, the inbox callback ref, and
        the prior chain head. The bootstrap job awaits
        ``task_status.started()``, which fires once this task has
        the impl mounted + ``expose_service`` open — by then the
        agent_mcp service is reachable to frontends.

        Cancellation paths:

        - ``self._agent_mcp_cancel_scope.cancel()`` — called by
          :meth:`_do_close_session` before sending ``CloseOp``.
          The async-with chain unwinds: ``expose_service`` drops
          the registration → ``impl_tg`` cancels watcher workers
          → ``impl_scope`` drains.
        - Cycle teardown via ``inner_tg.cancel_scope.cancel()`` —
          same unwind.
        """
        assert self._rpc is not None
        rpc = self._rpc
        with anyio.CancelScope() as cs:
            self._agent_mcp_cancel_scope = cs
            try:
                async with (
                    FxtrScope(
                        isolated=True,
                        # ``GLOBAL_SCOPE`` is required so the log builder
                        # can store typed log entities (`ScriptPreamble`,
                        # `WsAgentLogEventChain`, the per-kind event
                        # entities) — their mashumaro decode chases
                        # type-descriptor entities that live in
                        # GLOBAL_SCOPE.
                        include=[rpc.host_scope, GLOBAL_SCOPE],
                        name=f"agent_mcp_impl/{session_id}",
                    ) as impl_scope,
                    anyio.create_task_group() as impl_tg,
                ):
                    # Resolve the persistence cap (if configured) into
                    # a proxy local to ``impl_scope`` — the log writer
                    # serializes chain-head ``FxtrID``s through this
                    # proxy on every ``pin``; the FxtrIDs are stored
                    # in ``impl_scope`` (by ``extend_chain``), so the
                    # proxy must dispatch *from* ``impl_scope`` to
                    # find them.
                    persistence_proxy: PersistenceProto | None = None
                    if persistence_ref is not None:
                        persistence_proxy = await persistence_ref.resolve(scope=impl_scope)
                    impl = AgentMcpRunningSessionImpl(
                        scope=impl_scope,
                        workstation_ref=rpc.workstation_ref,
                        inbox_callback_ref=inbox_ref,
                        initial_head=initial_head,
                        impl_tg=impl_tg,
                    )
                    impl_ref = await impl_scope.mount(impl)
                    # Build the log-intent stream + spawn the writer
                    # task BEFORE mounting expose_service. Order:
                    # (1) stream send/recv created, (2) writer spawned
                    # on impl_tg, (3) state fields published so worker
                    # handlers can push, (4) expose_service. Reverse
                    # order on shutdown: expose_service releases →
                    # state fields cleared → writer drains + exits
                    # (on Shutdown sentinel or cancellation) →
                    # impl_tg drains → impl_scope drains.
                    log_send, log_recv = anyio.create_memory_object_stream[_LogIntent](
                        max_buffer_size=256
                    )
                    writer_done = anyio.Event()
                    impl_tg.start_soon(
                        self._log_writer_loop,
                        log_recv,
                        impl_scope,
                        impl,
                        persistence_proxy,
                        session_id,
                        rpc.workstation_ref,
                        writer_done,
                    )
                    async with expose_service(
                        rpc.workstation_ref,
                        f"agent_mcp:{session_id}",
                        impl_ref,
                        impl_scope,
                    ):
                        self._agent_mcp_impl = impl
                        self._agent_mcp_persistence_ref = persistence_ref
                        self._log_intent_send = log_send
                        self._log_writer_done = writer_done
                        try:
                            task_status.started()
                            # Park until cancelled.
                            await anyio.sleep_forever()
                        finally:
                            self._agent_mcp_impl = None
                            self._agent_mcp_persistence_ref = None
                            self._log_intent_send = None
                            self._log_writer_done = None
                            # Close the send-side so the writer
                            # exits cleanly (if it hasn't already
                            # been shut down via the sentinel). The
                            # impl_tg.__aexit__ will then cancel +
                            # join any remaining writer task on
                            # cancellation paths.
                            log_send.close()
            finally:
                # Clear our cancel-scope reference only if it's
                # still us (defensive; concurrent reconfigures
                # shouldn't happen but guard against stale clears).
                if self._agent_mcp_cancel_scope is cs:
                    self._agent_mcp_cancel_scope = None

    async def _log_writer_loop(
        self,
        log_recv: "MemoryObjectReceiveStream[_LogIntent]",
        impl_scope: FxtrScope,
        impl: AgentMcpRunningSessionImpl,
        persistence_proxy: "PersistenceProto | None",
        session_id: str,
        workstation_ref: ServiceRef[WorkstationServiceProto],
        done: anyio.Event,
    ) -> None:
        """Drain log intents from ``log_recv``; build typed event
        entities; extend + pin the chain; call ``impl.update_head``.

        Exit conditions:

        - **Graceful close**: receive :class:`_LogIntent_Shutdown`,
          fire ``done``, return. ``_do_close_session`` uses this
          to flush the close mutation's script-execution log entry
          before tearing down the agent_mcp subtask.
        - **Stream closed** (no more senders): clean exit. Happens
          when the lifecycle subtask's outer ``finally`` closes
          the send-side on cycle teardown.
        - **Cancellation**: cycle teardown / abnormal close —
          propagates, ``done`` may or may not be set.
        - **Pin failure / build failure**: re-raises into the
          impl_tg → propagates out of the lifecycle subtask →
          supervisor pauses with the formatted reason.

        All entities are stored into ``impl_scope`` (per scope
        discipline). The walker re-resolves ``workstation_ref`` into
        a transient child scope for each entity / service lookup —
        scope-discipline-friendly, and independent of the active
        scope (which is ``GLOBAL_SCOPE`` here since the writer task
        was started via ``impl_tg.start_soon``).
        """
        try:
            async for intent in log_recv:
                if isinstance(intent, _LogIntent_Shutdown):
                    return
                event_ref = await self._build_log_event(
                    intent,
                    impl_scope=impl_scope,
                    workstation_ref=workstation_ref,
                )
                await extend_chain(
                    scope=impl_scope,
                    prev_head=impl.chain_head,
                    event_ref=event_ref,
                    persistence=persistence_proxy,
                    session_id=session_id,
                    update_head=impl.update_head,
                )
        finally:
            done.set()

    async def _build_log_event(
        self,
        intent: _LogIntent,
        *,
        impl_scope: FxtrScope,
        workstation_ref: ServiceRef[WorkstationServiceProto],
    ) -> "EntityRef[Any]":
        """Dispatch one ``_LogIntent`` to the matching ``log_builder``
        function, returning a typed ``EntityRef`` for the event."""
        slot_key_for_slot_n = self._mirror.pending_slot_index.get
        match intent:
            case _LogIntent_ScriptExecution():
                return await build_script_execution_event(
                    scope=impl_scope,
                    script_src=intent.script_src,
                    preamble_src=intent.preamble_src,
                    stdout=intent.stdout,
                    stderr=intent.stderr,
                    error=intent.error,
                    rejected=intent.rejected,
                    committed_seq=intent.committed_seq,
                    runner_queued_ops=intent.runner_queued_ops,
                    export_entity_short_ids=intent.export_entity_short_ids,
                    prefilled_entity_short_ids=intent.prefilled_entity_short_ids,
                    workstation_ref=workstation_ref,
                    slot_key_for_slot_n=slot_key_for_slot_n,
                )
            case _LogIntent_InvokeResult():
                return await build_invoke_result_event(
                    scope=impl_scope,
                    slot_key=intent.slot_key,
                    slot_n=intent.slot_n,
                    seq=intent.seq,
                    payload=intent.payload,
                    workstation_ref=workstation_ref,
                    slot_key_for_slot_n=slot_key_for_slot_n,
                )
            case _LogIntent_InvokeError():
                return build_invoke_error_event(
                    scope=impl_scope,
                    slot_key=intent.slot_key,
                    slot_n=intent.slot_n,
                    seq=intent.seq,
                    message=intent.message,
                )
            case _LogIntent_CallbackNotification():
                return await build_callback_notification_event(
                    scope=impl_scope,
                    slot_key=intent.slot_key,
                    slot_n=intent.slot_n,
                    seq=intent.seq,
                    payload=intent.payload,
                    workstation_ref=workstation_ref,
                    slot_key_for_slot_n=slot_key_for_slot_n,
                )
            case _LogIntent_Shutdown():
                raise AssertionError(
                    "_LogIntent_Shutdown should be filtered out before _build_log_event"
                )

    async def _do_script_job(self, job: _ScriptJob) -> ScriptOutcome:
        """Run one sandboxed script under a fresh per-script
        ``script_scope``, then apply the script's queued ops as a
        single workstation mutation.

        Phases:

        1. Open ``script_scope`` (child of ``host_scope``); mount a
           fresh :class:`McpHostImpl` on it; thread the per-script
           ``host_ref`` into :class:`RunnerConfig`.
        2. Open the sandbox under
           ``script_scope.as_active_scope()`` and run the script.
           During this phase, host RPC dispatches (e.g.
           ``copy_entity_in``) populate the per-script
           ``McpHostImpl.copied_entities`` and the runner's
           ``queued_ops`` list.
        3. End-of-script: for each ``ExportEntityOp`` in the
           runner's queue, ``expose_entity`` the FxtrID over the
           ``/rpc`` connection (held open in an
           :class:`AsyncExitStack`); collect the
           workstation-allocated short_ids into
           ``entity_short_ids``.
        4. Build a :class:`MutateSessionRequest` via
           :func:`build_mutate_request`. If ``None`` (empty
           mutation), short-circuit and return success.
        5. Send the mutation; await ``MutationCommittedResponse`` /
           ``MutationRejectedResponse``. On commit, synthesize a
           :class:`StateUpdateEvent` locally (the workstation
           self-suppresses our own state_update) and reset the
           mirror; release the registrations (session-pin keeps
           refs alive); return success. On reject, release
           registrations (nothing committed); return tool-error
           string.

        ``script_scope`` drains on exit either way; copied-out
        entities go with it.
        """
        if self._rpc is None:
            raise RuntimeError("script job ran with /rpc connection unavailable")
        if self._active_subscription_id is None:
            raise NoActiveSession("run_script requires an active session; call open_session first.")
        rpc = self._rpc
        host_queued_ops: list[dict[str, Any]] = []
        async with FxtrScope(
            isolated=True,
            # ``GLOBAL_SCOPE`` is required for inbound sandbox→MCP RPC
            # dispatches (which run under ``script_scope`` via the
            # sandbox-side ``open_rpc_scope``'s session.scope). The
            # sandbox-provenance walker introspects ``host_ref.$fxtr.type``
            # at connect time, and the MCP-side answer carries a
            # type-descriptor FxtrID that lives in GLOBAL_SCOPE — the
            # dispatch's ``call_scope`` needs to be able to lift it.
            include=[rpc.host_scope, GLOBAL_SCOPE],
            name="script_scope",
        ) as script_scope:
            host_impl = McpHostImpl(
                rpc.workstation_proxy,
                script_scope,
                host_queued_ops,
                self._mirror,
                job.working_dir,
                self._exception_log,
            )
            host_ref = await script_scope.mount(host_impl)
            config = RunnerConfig(
                preamble_src=job.preamble_src,
                script_src=job.script_src,
                host_ref=host_ref,
            )
            with script_scope.as_active_scope():
                async with job.env.open_sandbox(
                    entry_point="fxtr_ws_mcp_sandbox_bridge.entry:run",
                    volumes=job.volumes,
                    sys_path_additions=job.sys_path_additions,
                ) as sandbox_ref:
                    typed = sandbox_ref.replace_service_type(
                        SandboxConnectProto[RunnerConfig, McpRunnerProto]
                    )
                    sandbox = await typed.resolve()
                    runner_ref = await sandbox.connect(config)
                    runner = await runner_ref.resolve()
                    result = await runner.run()

                runner_queued_ops: list[dict[str, Any]] = list(result.get("queued_ops", []))
                script_stdout: str = result.get("stdout", "") or ""
                script_stderr: str = result.get("stderr", "") or ""
                script_error: str | None = result.get("error")

                if script_error is not None:
                    # Script (or preamble) raised. The sandbox
                    # already zeroed queued_ops on the wire; we
                    # skip _apply_script_mutation entirely so the
                    # workstation stays unchanged. The agent sees a
                    # clean Python traceback (filename
                    # ``<mcp_script>`` / ``preamble.py``, line
                    # numbers honest) plus anything the script
                    # printed before it raised.
                    self._push_script_execution_log_intent(
                        script_src=job.script_src,
                        preamble_src=job.preamble_src,
                        stdout=script_stdout,
                        stderr=script_stderr,
                        error=script_error,
                        rejected=None,
                        committed_seq=None,
                        runner_queued_ops=[],
                        export_entity_short_ids={},
                    )
                    return ScriptOutcome(
                        body=_format_script_error(
                            script_error,
                            script_stdout,
                            script_stderr,
                            self._exception_log,
                        ),
                        scheduled_invoke_slot_keys=[],
                    )

                # End-of-script flow under script_scope.as_active_scope()
                # so expose_entity's resolves into the right scope.
                outcome = await self._apply_script_mutation(
                    rpc=rpc,
                    script_scope=script_scope,
                    host_impl=host_impl,
                    runner_queued_ops=runner_queued_ops,
                    script_src=job.script_src,
                    preamble_src=job.preamble_src,
                    stdout=script_stdout,
                    stderr=script_stderr,
                )
        return ScriptOutcome(
            body=_append_streams(outcome.body, script_stdout, script_stderr),
            scheduled_invoke_slot_keys=outcome.scheduled_invoke_slot_keys,
        )

    async def _apply_script_mutation(
        self,
        *,
        rpc: WorkstationRpc,
        script_scope: FxtrScope,
        host_impl: McpHostImpl,
        runner_queued_ops: list[dict[str, Any]],
        script_src: str,
        preamble_src: str,
        stdout: str,
        stderr: str,
    ) -> ScriptOutcome:
        """Phases 3-5 of the script flow.

        Phase 3: register each export-entity FxtrID with the
        workstation (delegated to :meth:`_register_exported_entities`)
        — registrations live for the duration of the
        :class:`AsyncExitStack`; on exit (return *or* raise) every
        one is released, with the workstation's session pin keeping
        refs alive after a successful commit.

        Phase 4: build the request via :func:`build_mutate_request`.
        ``None`` means an empty mutation — short-circuit the wire.

        Phase 5: send + await response, then either synthesize a
        local :class:`StateUpdateEvent` from the build result and
        enqueue an :class:`_AckCommitJob`, or surface the rejection
        as a tool-error string. Stage 4 will extend phase 3 to also
        register entity refs surfaced inside ``schedule_invoke``
        arg lists.

        Returns a :class:`ScriptOutcome` with the tool-result body
        plus the list of slot_keys this script's ``schedule_invoke``
        ops just placed into ``data_by_key`` as
        ``["invoke_pending", N]`` entries. Error paths return an
        empty slot-key list (nothing was committed).
        """
        assert self._active_subscription_id is not None
        async with contextlib.AsyncExitStack() as stack:
            entity_short_ids = await self._register_exported_entities(
                stack=stack,
                rpc=rpc,
                script_scope=script_scope,
                host_impl=host_impl,
                runner_queued_ops=runner_queued_ops,
            )
            try:
                build_result = build_mutate_request(
                    mirror=self._mirror,
                    pending_changes=self._pending_changes,
                    runner_queued_ops=runner_queued_ops,
                    entity_short_ids=entity_short_ids,
                    slot_counter=self._slot_counter,
                    ack_seq=self._last_processed_seq,
                    request_id=self._next_request_id(),
                    subscription_id=self._active_subscription_id,
                    update_id=next(self._next_update_id_counter),
                )
            except ForgetMissingKey as e:
                rejected = (
                    f"forget({e.slot_key!r}) referenced a key or callback "
                    "notification that is not in the session's state"
                )
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=rejected,
                    committed_seq=None,
                    runner_queued_ops=[],
                    export_entity_short_ids={},
                )
                return ScriptOutcome(
                    body=(
                        f"Error: forget({e.slot_key!r}) referenced a key "
                        "or callback notification that is not in the session's "
                        "state. The mutation was not sent."
                    ),
                    scheduled_invoke_slot_keys=[],
                )
            except SlotKeyAlreadyExists as e:
                rejected = (
                    f"cannot {e.kind}({e.slot_key!r}) — slot already holds " f"{e.running_value!r}"
                )
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=rejected,
                    committed_seq=None,
                    runner_queued_ops=[],
                    export_entity_short_ids={},
                )
                return ScriptOutcome(
                    body=(
                        f"Error: cannot {e.kind}({e.slot_key!r}) — slot already "
                        f"holds {e.running_value!r}. Call forget({e.slot_key!r}) "
                        "in a prior script first. The mutation was not sent."
                    ),
                    scheduled_invoke_slot_keys=[],
                )
            except SlotKeyReserved as e:
                rejected = f"slot_key {e.slot_key!r} is reserved for MCP-internal use"
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=rejected,
                    committed_seq=None,
                    runner_queued_ops=[],
                    export_entity_short_ids={},
                )
                return ScriptOutcome(
                    body=(
                        f"Error: cannot {e.kind}({e.slot_key!r}) — slot_key "
                        f"{e.slot_key!r} is reserved for MCP-internal use. "
                        "Pick a different slot_key. The mutation was not sent."
                    ),
                    scheduled_invoke_slot_keys=[],
                )
            if build_result is None:
                # No wire mutation needed. Still worth a log entry —
                # the agent ran a script. ``runner_queued_ops`` can
                # still be non-empty if the script only called
                # MCP-side-only builtins (currently just
                # ``present_to_user``); pre-substitute placeholders
                # for those ops the same way the committed path
                # does, with an empty ``slot_allocations`` (no
                # slot-claiming ops in this branch).
                substituted_ops = [
                    substitute_op_for_log(op, entity_short_ids, {}) for op in runner_queued_ops
                ]
                export_entity_short_ids_by_slot_key = {
                    op["slot_key"]: entity_short_ids[slot_handle(op["slot_key"])]
                    for op in runner_queued_ops
                    if op.get("kind") == KIND_EXPORT_ENTITY
                }
                # Resolve every workstation short_id to its FxtrID
                # *now*, while the ``expose_entity`` registrations
                # are still alive on the stack. See
                # :class:`_LogIntent_ScriptExecution.prefilled_entity_short_ids`
                # for the lifetime rationale.
                prefilled = await self._prefill_entity_short_ids(rpc, entity_short_ids)
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=None,
                    committed_seq=None,
                    runner_queued_ops=substituted_ops,
                    export_entity_short_ids=export_entity_short_ids_by_slot_key,
                    prefilled_entity_short_ids=prefilled,
                )
                return ScriptOutcome(
                    body=_summarize_mutation_outcome(runner_queued_ops, req=None),
                    scheduled_invoke_slot_keys=[],
                )
            req = build_result.request
            with self._track_pending(req.request_id) as receive:
                await self._send_request(req)
                frame = await self._expect(receive)
            if isinstance(frame, MutationRejectedResponse):
                rejected_str = f"{frame.reason}: {frame.message}"
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=rejected_str,
                    committed_seq=None,
                    runner_queued_ops=[],
                    export_entity_short_ids={},
                )
                return ScriptOutcome(
                    body=(
                        f"Error: mutation rejected ({frame.reason}): "
                        f"{frame.message}. The session state was not "
                        "changed."
                    ),
                    scheduled_invoke_slot_keys=[],
                )
            if isinstance(frame, ErrorResponse):
                self._push_script_execution_log_intent(
                    script_src=script_src,
                    preamble_src=preamble_src,
                    stdout=stdout,
                    stderr=stderr,
                    error=None,
                    rejected=f"transport error: {frame.message}",
                    committed_seq=None,
                    runner_queued_ops=[],
                    export_entity_short_ids={},
                )
                return ScriptOutcome(
                    body=f"Error: mutation transport error: {frame.message}",
                    scheduled_invoke_slot_keys=[],
                )
            if not isinstance(frame, MutationCommittedResponse):
                raise ProtocolError(f"unexpected response to mutation: {frame!r}")
            # Synthesize the StateUpdateEvent locally — the
            # workstation self-suppresses our own state_update
            # events. Route it through ``_handle_state_update``
            # with ``advance_last_processed_seq=False`` so the
            # seq advance is deferred to the ``_AckCommitJob``
            # below — that job lands behind any foreign events
            # queued before our response, so they drain first.
            self._apply_local_commit(build_result, frame)
            # Enqueue an ack-commit job so ``_last_processed_seq``
            # advances *after* any foreign events queued before
            # our response have been drained. Fire-and-forget —
            # submission can't fail unless the supervisor's
            # exiting, in which case the seq doesn't matter
            # anyway.
            await self._submit(_AckCommitJob(seq=frame.seq))
            scheduled_invoke_slot_keys = [
                op["slot_key"]
                for op in runner_queued_ops
                if op.get("kind") in (KIND_SCHEDULE_INVOKE, KIND_SCHEDULE_CALLBACK_INVOKE)
            ]
            # Push log intent for the committed script. The mutation
            # builder already pre-substituted placeholders into the
            # wire-final shape (``["entity", short_id]`` /
            # ``["callback", N]``) and exposed the result via
            # ``BuildResult.substituted_runner_queued_ops``.
            # ``entity_short_ids`` is keyed by the host-side *handle*
            # (e.g. ``slot:foo`` for export_entity, ``invoke_arg:…``
            # for invoke args). The log builder wants a slot_key-keyed
            # subset covering only export_entity ops — re-project here.
            export_entity_short_ids_by_slot_key = {
                op["slot_key"]: entity_short_ids[slot_handle(op["slot_key"])]
                for op in runner_queued_ops
                if op.get("kind") == KIND_EXPORT_ENTITY
            }
            # Resolve every workstation short_id to its FxtrID
            # *now*, while the ``expose_entity`` registrations
            # are still alive on the stack. See
            # :class:`_LogIntent_ScriptExecution.prefilled_entity_short_ids`
            # for the lifetime rationale.
            prefilled = await self._prefill_entity_short_ids(rpc, entity_short_ids)
            self._push_script_execution_log_intent(
                script_src=script_src,
                preamble_src=preamble_src,
                stdout=stdout,
                stderr=stderr,
                error=None,
                rejected=None,
                committed_seq=frame.seq,
                runner_queued_ops=build_result.substituted_runner_queued_ops,
                export_entity_short_ids=export_entity_short_ids_by_slot_key,
                prefilled_entity_short_ids=prefilled,
            )
            return ScriptOutcome(
                body=_summarize_mutation_outcome(runner_queued_ops, req),
                scheduled_invoke_slot_keys=scheduled_invoke_slot_keys,
            )
        # Unreachable: every branch of the ``async with`` body
        # above returns or raises. Type-checkers don't follow that
        # through ``contextlib.AsyncExitStack``'s ``__aexit__``;
        # this assert is the explicit "no fall-through" marker.
        raise AssertionError("unreachable: _apply_script_mutation has no fall-through")

    async def _register_exported_entities(
        self,
        *,
        stack: contextlib.AsyncExitStack,
        rpc: WorkstationRpc,
        script_scope: FxtrScope,
        host_impl: McpHostImpl,
        runner_queued_ops: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Walk ``runner_queued_ops`` for entity-bearing handles —
        ``slot_handle(op.slot_key)`` for each ``ExportEntityOp`` and
        each entry in ``ScheduleInvokeOp.entity_arg_handles`` —
        register each FxtrID over ``/rpc`` via ``expose_entity``
        (entered on ``stack`` so they release on phase exit), and
        return a ``handle → workstation short_id`` dict for the
        mutation builder to substitute in.

        Handles live in a namespace distinct from agent-facing
        slot_keys: see :func:`slot_handle` /
        :func:`fresh_invoke_arg_handle` in
        ``fxtr_ws_mcp_sandbox_bridge.protocols``.

        Registration ``external_name`` is
        ``f".mcp-tmp:{session_id}:{uuid4}:{handle}"``: the leading
        dot marks it as an internal (non-public) registration; the
        embedded session_id + uuid4 + handle make it easy to trace
        in workstation-side logs.

        Once the mutation commits, each registered entity stays
        alive without our /rpc reg:

        - Exported entities: pinned by the tracker diff on this
          same mutation (they land in ``data_by_key`` and so
          fall in :func:`desired_tracker_set`'s scope).
        - Invoke args: pinned by the workstation's invoke worker
          when it processes the :class:`InvokeOp` (the args
          aren't in ``data_by_key`` so the tracker diff doesn't
          cover them).

        Either way the per-script AsyncExitStack can release all
        registrations on mutation-commit without waiting for
        result events.
        """
        entity_short_ids: dict[str, str] = {}
        session_id = self._active_session_id or "no-session"

        async def _register(handle: str) -> None:
            if handle in entity_short_ids:
                raise RuntimeError(f"duplicate entity-registration handle {handle!r}")
            fxtr_id = host_impl.copied_entities.get(handle)
            if fxtr_id is None:
                raise RuntimeError(
                    f"entity for handle {handle!r} missing from "
                    "host copied_entities (sandbox / host bookkeeping "
                    "out of sync)"
                )
            external_name = f".mcp-tmp:{session_id}:{uuid.uuid4()}:{handle}"
            short_id = await stack.enter_async_context(
                expose_entity(
                    rpc.workstation_ref,
                    external_name=external_name,
                    entity=fxtr_id,
                    scope=script_scope,
                )
            )
            entity_short_ids[handle] = short_id

        # TODO: registrations are sequential because each
        # ``expose_entity`` is awaited in turn. For scripts with
        # many entity-bearing ops the round-trips serialize. Could
        # be parallelized via ``anyio.create_task_group()`` +
        # ``stack.enter_async_context`` per task; revisit if invoke
        # arg counts grow large.
        for raw_op in runner_queued_ops:
            kind = raw_op.get("kind")
            if kind == KIND_EXPORT_ENTITY:
                await _register(slot_handle(raw_op["slot_key"]))
            elif kind in (KIND_SCHEDULE_INVOKE, KIND_SCHEDULE_CALLBACK_INVOKE):
                for handle in raw_op.get("entity_arg_handles", []):
                    await _register(handle)
            elif kind == KIND_PRESENT_TO_USER:
                # ``present_to_user`` doesn't emit a workstation op,
                # but its payload may reference entities the agent
                # copy_entity_in'd from the sandbox. Register each
                # so the log walker can resolve them; the
                # registration releases on the AsyncExitStack exit.
                # The log walker's resolution would then race with
                # the workstation dropping the short_id mapping —
                # see ``_prefill_entity_short_ids`` for how we keep
                # the FxtrIDs alive past the stack exit.
                for handle in raw_op.get("entity_arg_handles", []):
                    await _register(handle)
        return entity_short_ids

    async def _prefill_entity_short_ids(
        self,
        rpc: WorkstationRpc,
        entity_short_ids: dict[str, str],
    ) -> dict[str, FxtrID]:
        """Resolve every workstation ``short_id`` registered for this
        script's ops into a ``short_id → FxtrID`` map, *while the
        ``expose_entity`` registrations on the caller's
        :class:`AsyncExitStack` are still alive*.

        Why this exists: the log writer task drains
        :class:`_LogIntent_ScriptExecution` intents asynchronously,
        and its walker would normally call
        ``workstation.get_entity(short_id)`` on each
        ``["entity", short_id]`` leaf. But by the time the writer
        runs, the per-script ``AsyncExitStack`` has exited and the
        workstation has released the only thing pinning the
        ``present_to_user`` entities — there is no in-flight
        invoke holding them, unlike ``schedule_invoke`` args. The
        walker's RPC would then fail with "unknown or inactive
        entity short id".

        Resolution here is done against ``rpc.workstation_proxy``,
        which is bound to ``host_scope``. The workstation's
        ``get_entity`` implementation calls ``track_from_scope``
        from ``host_scope`` onto the entity's record, which keeps
        the FxtrID reachable in ``host_scope`` (connection-
        lifetime) — and therefore in the writer's ``impl_scope``,
        since impl_scope ``include``s host_scope.

        The writer's walker uses the returned map as a pre-populated
        cache; the RPC never has to run from the writer task.

        Schedule_invoke args benefit incidentally — their pin
        normally outlives the writer's processing window, but the
        race is structurally identical and prefilling closes it.
        """
        if not entity_short_ids:
            return {}
        unique_short_ids = sorted(set(entity_short_ids.values()))
        # Resolve in parallel — each get_entity is an independent RPC.
        prefilled: dict[str, FxtrID] = {}

        async def _resolve_one(short_id: str) -> None:
            prefilled[short_id] = await rpc.workstation_proxy.get_entity(short_id)

        async with anyio.create_task_group() as tg:
            for short_id in unique_short_ids:
                tg.start_soon(_resolve_one, short_id)
        return prefilled

    def _apply_local_commit(
        self,
        build_result: BuildResult,
        response: MutationCommittedResponse,
    ) -> None:
        """Update the local mirror from a successfully-committed
        mutation we just sent. The workstation self-suppresses
        broadcasting our own state_update, but we know everything
        we need from the response + build_result, so we update
        the mirror directly — no synthesized event.

        Uses the build result's pre-computed ``post_data_by_key``
        + tracker sets directly rather than re-applying the JSON
        Patch — :func:`build_mutate_request` already did that
        work. ``slot_counter`` stays at the connection's
        last-known value (stage 3 claims no slots; stage 4 will
        bump here based on invokes in this mutation).

        **By construction we have nothing to replay**: we sent
        ``ack_seq = _last_processed_seq``, and every event in
        ``_replayable_events`` has ``seq <= _last_processed_seq``
        (because ``_apply_state_effect`` is the only writer of
        the list and ``_do_event_job`` advances
        ``_last_processed_seq`` past every event it processes).
        After commit, the new ``last_state_ack_seq == ack_seq``
        covers every event, so trim drops everything. We just
        clear ``_replayable_events`` + ``_unflushed_state_changes``
        directly instead of going through the full
        ``_handle_state_update`` rebuild loop (which would do
        the same work but with empty result).

        Does not advance ``_last_processed_seq``; that's the
        ``_AckCommitJob`` enqueued by
        :meth:`_apply_script_mutation` after this returns. The
        ack-commit lands behind any foreign events queued during
        the response await, so those drain first and we never
        ack ahead of locally-processed work.
        """
        if self._mirror.last_state_seq and response.seq <= self._mirror.last_state_seq:
            raise ProtocolError(
                f"self-commit seq={response.seq} regressed (last applied "
                f"seq={self._mirror.last_state_seq})"
            )
        # build_result.post_state is the full state dict that
        # results from applying our patch to the pre-commit state.
        # We trust it directly — no merging back into prior
        # ``mirror.state``, no field-by-field synthesis.
        self._mirror.apply_workstation_view(
            state=build_result.post_state,
            last_state_seq=response.seq,
            last_state_ack_seq=build_result.request.ack_seq,
            tracked_entities=sorted(build_result.desired_entities),
            tracked_services=sorted(build_result.desired_services),
        )
        # Bump _slot_counter to reflect any slots this mutation
        # consumed (one per ScheduleInvokeOp + CreateCallbackOp).
        self._slot_counter = max(self._slot_counter, build_result.new_slot_counter)
        # By construction: every event in _replayable_events has
        # seq <= sent ack_seq, so all subsumed by the new
        # last_state_ack_seq. Clear directly. Likewise pending
        # changes: the workstation just applied everything we
        # sent, including the init prologue if it was there.
        self._replayable_events = []
        self._pending_changes = []
        # Same reasoning for the notification-dedupe set — any seq
        # we'd notified for is now past the ack horizon.
        self._notified_seqs = {s for s in self._notified_seqs if s > build_result.request.ack_seq}

    async def _do_event_job(self, job: _EventJob) -> None:
        """Handle one inbound :class:`SessionEventFrame` for the
        active subscription.

        Two arms:

        - :class:`StateUpdateEvent` → call
          :meth:`_handle_state_update` (mirror update + event-list
          trim + derived-dict rebuild, **no seq advance**), then
          advance ``_last_processed_seq`` inline. Foreign closes
          additionally clear the active subscription.
        - Non-state events (:class:`NonStateSessionEvent` —
          invoke / load / metadata / callback) → run hard-error
          checks, then ``_apply_state_effect`` (appends to
          ``_replayable_events``, writes the derived
          ``_unflushed_state_changes`` entry, and fires the
          channel notification — all in one place). Advance
          ``_last_processed_seq``.

        Stage 3 has no MCP-issued operations that produce non-state
        events, so the non-state arm shouldn't fire under normal
        use; the helpers are stubs that log + drop.
        """
        ev = job.frame.event
        if isinstance(ev, StateUpdateEvent):
            await self._handle_state_update(ev)
            self._last_processed_seq = max(self._last_processed_seq, ev.seq)
            if ev.closed:
                # Foreign close — our subscription is gone. Stage 3
                # logs; stage 4 will channel-notify. Reconnect treats
                # this as session_gone if we try to re-subscribe.
                _log.warning(
                    "ws_client: active session was closed by a foreign "
                    "mutator; subsequent tool calls will fail until "
                    "the agent opens a new session"
                )
                self._active_session_id = None
                self._active_subscription_id = None
            return

        # --- Non-state event arm ---
        # Hard-error invariants (B.5):
        # - Within-cycle re-delivery (we already advanced past this
        #   seq in this cycle).
        # - Past the workstation's ack horizon (we should never
        #   receive a non-preserved event with seq <=
        #   mirror.last_state_ack_seq on the live stream).
        if ev.seq <= self._last_processed_seq:
            raise ProtocolError(
                f"non-state event seq={ev.seq} re-delivered within cycle "
                f"(last_processed_seq={self._last_processed_seq})"
            )
        if ev.seq <= self._mirror.last_state_ack_seq:
            raise ProtocolError(
                f"non-state event seq={ev.seq} arrived but is past the "
                "workstation's ack horizon "
                f"(mirror.last_state_ack_seq={self._mirror.last_state_ack_seq})"
            )
        # Look up the slot_key BEFORE _apply_state_effect — for
        # invoke results, the per-kind branch drops the
        # pending_slot_index entry on promote, so this lookup must
        # happen ahead of the mirror mutation.
        slot_key_for_log: str | None = None
        if isinstance(ev, (InvokeResultEvent, InvokeErrorEvent, CallbackEvent)):
            slot_key_for_log = self._mirror.pending_slot_index.get(ev.slot)
        await self._apply_state_effect(ev)
        self._last_processed_seq = max(self._last_processed_seq, ev.seq)
        if slot_key_for_log is not None:
            self._push_event_log_intent(ev, slot_key_for_log)

    def _push_script_execution_log_intent(
        self,
        *,
        script_src: str,
        preamble_src: str,
        stdout: str,
        stderr: str,
        error: str | None,
        rejected: str | None,
        committed_seq: int | None,
        runner_queued_ops: list[dict[str, Any]],
        export_entity_short_ids: dict[str, str],
        prefilled_entity_short_ids: dict[str, FxtrID] | None = None,
    ) -> None:
        """Push a ``_LogIntent_ScriptExecution`` if the log stream is
        open. Called from :meth:`_do_script_job` (error path) and
        :meth:`_apply_script_mutation` (commit / rejected paths).

        ``prefilled_entity_short_ids`` defaults to an empty map for
        the error / rejected paths (no ops committed → no entities
        referenced from the log payload). The commit path passes a
        populated map.
        """
        if self._log_intent_send is None:
            return
        self._log_intent_send.send_nowait(
            _LogIntent_ScriptExecution(
                script_src=script_src,
                preamble_src=preamble_src,
                stdout=stdout,
                stderr=stderr,
                error=error,
                rejected=rejected,
                committed_seq=committed_seq,
                runner_queued_ops=runner_queued_ops,
                export_entity_short_ids=export_entity_short_ids,
                prefilled_entity_short_ids=prefilled_entity_short_ids or {},
            )
        )

    def _push_event_log_intent(
        self,
        ev: NonStateSessionEvent,
        slot_key: str,
    ) -> None:
        """Push a log intent for an inbound event the agent cares
        about.

        Sync — no awaits, no failure path for the worker. If the
        log stream isn't open yet (pre-bootstrap _EventJobs in the
        cycle-start path, before the lifecycle subtask publishes
        the send-side), drop the intent silently — that's the
        documented asymmetry between the cycle-start and
        open_session bootstraps.

        ``send_nowait`` can raise ``anyio.WouldBlock`` if the
        stream buffer is full (256 events behind unflushed).
        That's treated as a structural error — we re-raise so the
        worker's bare-dispatch teardown fires + supervisor
        pauses.
        """
        if self._log_intent_send is None:
            return
        match ev:
            case InvokeResultEvent():
                self._log_intent_send.send_nowait(
                    _LogIntent_InvokeResult(
                        slot_key=slot_key,
                        slot_n=ev.slot,
                        seq=ev.seq,
                        payload=ev.payload,
                    )
                )
            case InvokeErrorEvent():
                self._log_intent_send.send_nowait(
                    _LogIntent_InvokeError(
                        slot_key=slot_key,
                        slot_n=ev.slot,
                        seq=ev.seq,
                        message=ev.message,
                    )
                )
            case CallbackEvent():
                self._log_intent_send.send_nowait(
                    _LogIntent_CallbackNotification(
                        slot_key=slot_key,
                        slot_n=ev.slot,
                        seq=ev.seq,
                        payload=ev.payload,
                    )
                )
            case _:
                # Other event kinds (load / metadata) don't have
                # log-intent handlers yet — drop silently. We can
                # add per-kind branches as the corresponding
                # builtins land.
                pass

    async def _handle_state_update(self, ev: StateUpdateEvent) -> None:
        """Apply a workstation-emitted state_update to the mirror.

        Updates the mirror via ``apply_workstation_view``,
        advances ``_slot_counter``, trims ``_replayable_events``
        by ``ev.ack_seq``, then **rebuilds**
        ``_replayable_events`` and ``_unflushed_state_changes``
        by re-running :meth:`_apply_state_effect` on each
        surviving event. Redelivery is bit-identical to initial
        delivery. **Does not advance ``_last_processed_seq``** —
        the caller advances inline (foreign-event arm of
        :meth:`_do_event_job`, snapshot bootstrap in
        :meth:`_do_subscribe`).

        Steps:
        1. Reject ``ev.seq <= mirror.last_state_seq`` — workstation
           guarantees state_updates are strictly monotonic per
           session. The no-prior-state branch (post-wipe inside
           ``_do_subscribe`` or ``_do_close_session``) has
           ``last_state_seq == 0`` and applies cleanly.
        2. Update the mirror to the workstation view via
           ``apply_workstation_view``.
        3. Advance ``_slot_counter`` to ``ev.slot_counter``.
        4. Save the surviving events (``seq > ev.ack_seq``),
           clear ``_replayable_events`` +
           ``_unflushed_state_changes``, iterate calling
           ``_apply_state_effect`` on each. If a replay raises
           (e.g. foreign mutator overwrote a slot_key so the
           lookup fails), it propagates out — the supervisor
           tears down and the next subscribe rebuilds from a
           fresh snapshot.

        Self-commits don't go through this method:
        :meth:`_apply_local_commit` updates the mirror directly.
        Our own ``ack_seq`` always equals ``_last_processed_seq``,
        which by construction covers every event in
        ``_replayable_events``, so the trim would always empty
        the list and the rebuild would iterate zero times — the
        primitive isn't pulling its weight there.
        """
        if self._mirror.last_state_seq and ev.seq <= self._mirror.last_state_seq:
            raise ProtocolError(
                f"state_update seq={ev.seq} regressed (last applied "
                f"seq={self._mirror.last_state_seq})"
            )
        self._mirror.apply_workstation_view(
            state=ev.state,
            last_state_seq=ev.seq,
            last_state_ack_seq=ev.ack_seq,
            tracked_entities=ev.tracked_entities,
            tracked_services=ev.tracked_services,
        )
        self._slot_counter = max(self._slot_counter, ev.slot_counter)
        events_to_replay = [e for e in self._replayable_events if e.seq > ev.ack_seq]
        self._replayable_events = []
        # Rebuild ``_pending_changes`` from scratch: synthetic
        # init prologue (if the workstation's state still lacks
        # the top-level fields the MCP writes to), then one
        # entry per surviving event via the standard
        # ``_apply_state_effect`` path.
        self._pending_changes = []
        if workstation_state_needs_init(ev.state):
            self._pending_changes.append(PendingChange(op="initialize"))
        # Prune notification-dedupe entries that are now past the
        # workstation's ack horizon. The workstation has pruned
        # those events from its event_log, so we can't get a
        # re-delivery of them; the dedupe entries are dead.
        self._notified_seqs = {s for s in self._notified_seqs if s > ev.ack_seq}
        for e in events_to_replay:
            await self._apply_state_effect(e)
        _log.debug(
            "ws_client: state_update seq=%d ack_seq=%d slot_counter=%d "
            "applied; data_by_key keys=%r replayable=%d pending=%d",
            ev.seq,
            ev.ack_seq,
            ev.slot_counter,
            sorted(self._mirror.data_by_key.keys()),
            len(self._replayable_events),
            len(self._pending_changes),
        )

    async def _apply_state_effect(self, ev: NonStateSessionEvent) -> None:
        """The single place that maintains ``_replayable_events`` +
        ``_unflushed_state_changes`` + ``mirror.data_by_key`` for
        one non-state event, **and** fires the corresponding
        channel notification to the agent. Called identically at
        initial arrival (from the non-state arm of
        :meth:`_do_event_job`) and at replay time (from the rebuild
        loop inside :meth:`_handle_state_update`). Redelivery is
        bit-identical to initial delivery at the state-mutation
        layer; the notification step is gated by ``_notified_seqs``
        so the agent sees each promotion exactly once per session
        lifetime.

        Append-first: the event lands in ``_replayable_events``
        before dispatch attempts anything. If the dispatch raises
        (slot-lookup failure → :class:`ProtocolError`; unknown
        event kind → :class:`NotImplementedError`), partial state
        survives in the connection, but the exception propagates
        out of ``_dispatch_job`` and tears down the supervisor
        cycle — the next subscribe rebuilds from a fresh snapshot,
        so partial state in the dying connection doesn't matter.

        Per-kind dispatch:

        - :class:`InvokeResultEvent` / :class:`InvokeErrorEvent`:
          look up the slot_key from ``mirror.pending_slot_index``
          (O(1)); write ``["invoke_result", payload]`` or
          ``["invoke_error", message]`` to ``mirror.data_by_key``
          and ``_unflushed_state_changes``; drop the slot from the
          pending index; fire a ``notifications/claude/channel``
          frame via ``_maybe_notify`` (deduped by seq). Slot-lookup
          failure (mirror has no ``["invoke_pending", N]`` for
          this slot — happens if a foreign mutator overwrote the
          slot_key in the meantime) raises ``ProtocolError``.
        - :class:`CallbackEvent` (stage 6): look up the slot_key
          from ``mirror.pending_slot_index`` (the
          ``["callback_open", N]`` entry); store the payload in
          ``mirror.callback_notifications[slot_key][str(seq)]`` +
          the parallel
          ``_unflushed_callback_notifications[(slot_key, seq)]``;
          fire a channel notification with ``kind="callback"``
          and ``notif_index=str(seq)``. **Does not** modify
          ``data_by_key`` or drop the pending_slot_index entry —
          callbacks fire repeatedly.
        - Other non-state event kinds (``LoadResult*`` / ``Load*Error`` /
          ``EntityMetadata*`` / ``ServiceMetadata*``):
          stub — the MCP doesn't issue the corresponding ops yet.
          Raises ``NotImplementedError``. Later stages fill these
          in with the same shape as the invoke branches.
        """
        self._replayable_events.append(ev)
        match ev:
            case InvokeResultEvent():
                slot_key = self._mirror.pending_slot_index.get(ev.slot)
                if slot_key is None:
                    raise ProtocolError(
                        f"InvokeResultEvent slot={ev.slot} (seq={ev.seq}) "
                        "has no matching invoke_pending entry in "
                        "mirror.data_by_key"
                    )
                value: list[Any] = ["invoke_result", ev.payload]
                self._mirror.data_by_key[slot_key] = value
                del self._mirror.pending_slot_index[ev.slot]
                self._pending_changes.append(
                    PendingChange(
                        op="set",
                        path=("data_by_key", slot_key),
                        value=value,
                        seq=ev.seq,
                    )
                )
                await self._maybe_notify(
                    ev.seq,
                    slot_key,
                    "invoke_result",
                    summarize_invoke_payload(ev.payload),
                )
            case InvokeErrorEvent():
                slot_key = self._mirror.pending_slot_index.get(ev.slot)
                if slot_key is None:
                    raise ProtocolError(
                        f"InvokeErrorEvent slot={ev.slot} (seq={ev.seq}) "
                        "has no matching invoke_pending entry in "
                        "mirror.data_by_key"
                    )
                value = ["invoke_error", ev.message]
                self._mirror.data_by_key[slot_key] = value
                del self._mirror.pending_slot_index[ev.slot]
                self._pending_changes.append(
                    PendingChange(
                        op="set",
                        path=("data_by_key", slot_key),
                        value=value,
                        seq=ev.seq,
                    )
                )
                await self._maybe_notify(
                    ev.seq,
                    slot_key,
                    "invoke_error",
                    ev.message,
                )
            case CallbackEvent():
                slot_key = self._mirror.pending_slot_index.get(ev.slot)
                if slot_key is None:
                    raise ProtocolError(
                        f"CallbackEvent slot={ev.slot} (seq={ev.seq}) "
                        "has no matching callback_open entry in "
                        "mirror.data_by_key"
                    )
                # Store the wire-escaped payload under
                # callback_notifications[slot_key][str(seq)]. The
                # workstation's wire encoding of payload already
                # has entity / service / bytes / callback tags;
                # we keep that form as the canonical storage shape.
                per_slot = self._mirror.callback_notifications.setdefault(slot_key, {})
                per_slot[str(ev.seq)] = ev.payload
                self._pending_changes.append(
                    PendingChange(
                        op="set",
                        path=("callback_notifications", slot_key, str(ev.seq)),
                        value=ev.payload,
                        seq=ev.seq,
                    )
                )
                # Intentionally NOT deleting the pending_slot_index
                # entry — callbacks fire repeatedly.
                #
                # Inbox messages get a dedicated channel ``kind`` and
                # the full unabbreviated payload as content (operator
                # messages are typically short text — truncating the
                # default repr summary at 200 chars and making the
                # agent call ``retrieve_callback_notification`` to see
                # the rest is a bad default). Everything else stays
                # on the generic ``kind="callback"`` path with the
                # truncated summary.
                if slot_key == "workstation_inbox":
                    kind, content = _render_workstation_inbox_notif(ev.payload, ev.seq)
                else:
                    kind = "callback"
                    content = summarize_invoke_payload(ev.payload)
                await self._maybe_notify(
                    ev.seq,
                    slot_key,
                    kind,
                    content,
                    notif_index=ev.seq,
                )
            case CallbackSubscriptionEvent():
                # Connection-state transition for a callback inbox's
                # ``subscribe_to`` subscription. The inbox slot is an
                # ``["callback_open", N]`` entry that keeps its
                # pending_slot_index mapping, so the slot resolves while
                # the inbox is open.
                slot_key = self._mirror.pending_slot_index.get(ev.slot)
                if slot_key is None:
                    # The inbox slot is gone (closed / forgotten, or a
                    # foreign mutator overwrote it). A connection-state
                    # transition we can't map to a slot is purely
                    # informational, so drop it rather than tearing down
                    # the supervisor (unlike payload-bearing events).
                    _log.debug(
                        "ws_client: dropping CallbackSubscriptionEvent slot=%d "
                        "(seq=%d): no open callback_open entry in mirror",
                        ev.slot,
                        ev.seq,
                    )
                    return
                self._mirror.callback_subscription_states[slot_key] = (
                    "connected" if ev.connected else "broken"
                )
                # Only the break direction is actionable for the agent
                # ("resubscribe me"); connected transitions update mirror
                # state silently. Dedup is automatic via ``_notified_seqs``
                # (the event carries a seq), so reconnect / replay does
                # not re-spam the agent.
                if not ev.connected:
                    await self._maybe_notify(
                        ev.seq,
                        slot_key,
                        "subscription_broken",
                        _render_subscription_broken_notif(slot_key, ev.reason),
                    )
            case _:
                raise NotImplementedError(
                    f"no handler yet for non-state event {type(ev).__name__} (seq={ev.seq})"
                )

    async def _maybe_notify(
        self,
        seq: int,
        slot_key: str,
        kind: str,
        summary: str,
        *,
        notif_index: int | None = None,
    ) -> None:
        """Fire a ``notifications/claude/channel`` frame for one
        promoted slot, unless ``seq`` is already in
        ``_notified_seqs``.

        Dedupe so subscribe-time replay (across reconnect) and
        the rebuild loop inside ``_handle_state_update`` don't
        re-notify the agent for events they already saw. The
        dedupe set persists across ``_do_subscribe`` wipes (reset
        only on session close) and is pruned alongside the ack
        horizon in ``_handle_state_update`` /
        ``_apply_local_commit``.

        ``notif_index`` (stage 6) is set on callback frames so the
        agent can correlate the channel summary to a stored
        notification reachable via
        ``retrieve_callback_notification(slot_key, notif_index)``.
        Rendered as a decimal string in the channel attrs to fit
        the identifier-shape contract.
        """
        if seq in self._notified_seqs:
            return
        self._notified_seqs.add(seq)
        if self._notify_channel is None:
            return  # no channel wired (e.g. test harness)
        attrs: dict[str, str] = {"slot_key": slot_key, "kind": kind}
        if notif_index is not None:
            attrs["notif_index"] = str(notif_index)
        await self._notify_channel(summary, attrs)

    def _set_state(
        self,
        new_state: SupervisorState,
        reason: str | None,
    ) -> None:
        """Atomically update :attr:`_state` and :attr:`_pause_reason`,
        then fire + replace :attr:`_state_changed` so any
        ``wake_and_wait_ready`` caller waking up sees the new state.

        Standard condition-variable pattern: readers capture the
        event reference *before* reading state; here we replace
        the field with a fresh event *before* setting the old one,
        so readers that captured before our update see the fire
        and readers that captured after see the new unset event
        tied to the next transition.
        """
        self._state = new_state
        self._pause_reason = reason
        ev = self._state_changed
        self._state_changed = anyio.Event()
        ev.set()

    async def _wait_for_retry_or_shutdown(self) -> None:
        """Block until either ``_retry_requested`` or ``_shutdown``
        fires. Used by the supervisor loop's paused branch.
        Whichever lands first cancels the inner TG, so the other
        branch unblocks promptly."""
        async with anyio.create_task_group() as tg:

            async def _waiter(event: anyio.Event) -> None:
                await event.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(_waiter, self._shutdown)
            tg.start_soon(_waiter, self._retry_requested)

    async def _maybe_notify_supervisor_paused(self, reason: str) -> None:
        """Push a ``supervisor_paused`` channel frame to the agent
        on entry to the paused state. Best-effort: any failure to
        deliver is swallowed so the pause path doesn't compound
        the existing crash.

        Fired on every PAUSED entry except the initial construction
        state (which the supervisor task never explicitly transitions
        into — it's the starting value). The "tool's own
        ``wake_and_wait_ready`` raised, then the supervisor also
        emits a banner" duplication is harmless; the alternative
        (tracking whether anyone is waiting on the state change) is
        more complex than it's worth.
        """
        if self._notify_channel is None:
            return
        content = (
            f"Workstation MCP supervisor is paused: {reason}. "
            "Make any tool call to retry. If the pause repeats, "
            "try close_session(force=True) to clear the poisoned "
            "session."
        )
        try:
            await self._notify_channel(content, {"kind": "supervisor_paused"})
        except Exception:
            _log.exception("failed to deliver supervisor_paused channel frame")

    async def _do_ack_commit_job(self, job: _AckCommitJob) -> None:
        """Advance ``_last_processed_seq`` to ``job.seq`` if not
        already past it. Enqueued by ``_apply_script_mutation``
        after a successful self-commit; lands behind any foreign
        events that were queued before our response, so each of
        those gets a chance to advance the seq incrementally
        before our own commit's seq is acked.

        Also emits a ``kind=self_commit`` notification through
        the buffer so the agent sees a temporal landmark in the
        drained event stream — without it, slot promotions that
        landed both before and after our commit are
        indistinguishable in the drained tool output."""
        self._last_processed_seq = max(self._last_processed_seq, job.seq)
        if not job.silent and self._notify_channel is not None:
            await self._notify_channel(
                f"Your script's mutation committed at seq={job.seq}.",
                {"kind": "self_commit", "seq": str(job.seq)},
            )

    async def _do_service_broken_job(self, job: _ServiceBrokenJob) -> None:
        """Surface a broken service to the agent as a best-effort
        ``service_broken`` channel notice.

        Deduped per short_id via ``_notified_broken_services`` so a
        reconnect (which re-derives the break from current state) does
        not re-spam. Names any ``data_by_key`` slot_keys whose value is
        ``["exported_service", short_id]`` so the agent can tie the
        break back to slots it's holding. Informational only — the
        agent should stop invoking on that service."""
        short_id = job.service_short_id
        if short_id in self._notified_broken_services:
            return
        self._notified_broken_services.add(short_id)
        if self._notify_channel is None:
            return  # no channel wired (e.g. test harness)
        holding = sorted(
            slot_key
            for slot_key, value in self._mirror.data_by_key.items()
            if value[:2] == ["exported_service", short_id]
        )
        msg = f"Service {short_id} has broken and is no longer usable; invokes on it will fail."
        if holding:
            msg += " Held in slot_key(s): " + ", ".join(holding) + "."
        await self._notify_channel(msg, {"kind": "service_broken", "service_short_id": short_id})

    # --- Request/response plumbing ---------------------------------

    def _next_request_id(self) -> int:
        return next(self._next_request_id_counter)

    @contextlib.contextmanager
    def _track_pending(self, request_id: int):  # type: ignore[no-untyped-def]
        """Register a pending-request entry for the duration of the
        block. Yields a :class:`MemoryObjectReceiveStream` from which
        the caller awaits the expected frame(s)."""
        send, receive = anyio.create_memory_object_stream[ServerFrame](max_buffer_size=4)
        entry = _PendingRequest(request_id=request_id, send=send, receive=receive)
        if request_id in self._pending:
            raise RuntimeError(f"duplicate pending request id {request_id}")
        self._pending[request_id] = entry
        try:
            yield receive
        finally:
            self._pending.pop(request_id, None)
            send.close()

    async def _send_request(self, request: ClientRequest) -> None:
        if self._ws is None:
            raise ConnectionLost("WorkstationConnection is not connected")
        text = _REQUEST_ENCODER.encode(request).decode()
        try:
            await self._ws.send_text(text)
        except httpx_ws.HTTPXWSException as e:
            raise ConnectionLost(f"send failed: {e!r}") from e

    async def _expect(self, receive: MemoryObjectReceiveStream[ServerFrame]) -> ServerFrame:
        try:
            return await receive.receive()
        except anyio.EndOfStream as e:
            raise ConnectionLost("connection closed while awaiting response") from e


# --- Reconnect classification ----------------------------------------


def _iter_leaves(exc: BaseException):
    """Walk an exception or :class:`BaseExceptionGroup` and yield
    every leaf (non-group) exception."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_leaves(sub)
    else:
        yield exc


def _find_disconnect_leaf(exc: BaseException) -> _DisconnectRequested | None:
    """Return the first :class:`_DisconnectRequested` leaf in
    ``exc`` (walking through any ``BaseExceptionGroup`` wrapping),
    or ``None`` if none is present.

    Used by the supervisor's pause path to recognize
    agent-initiated disconnects so they route to PAUSED without
    the ``supervisor_paused`` channel banner. The disconnect's
    leaf wins even when the exception group also carries other
    failures — the user's intent is "end this cycle now"."""
    for leaf in _iter_leaves(exc):
        if isinstance(leaf, _DisconnectRequested):
            return leaf
    return None


def _format_pause_reason(exc: BaseException) -> str:
    """Build the short human-readable ``pause_reason`` string used
    in :attr:`WorkstationConnection.pause_reason`, in
    :class:`SupervisorNotReady` messages, and in the
    ``supervisor_paused`` channel banner content.

    Per-kind formats:

    - :class:`WorkstationRejected` → ``"workstation rejected: <msg>"``
    - :class:`ConnectionLost` / :class:`httpx_ws.HTTPXWSException` →
      ``"connection lost: <repr>"``
    - :class:`ProtocolError` → ``"protocol error: <msg>"``
    - :class:`NotImplementedError` →
      ``"unimplemented handler: <msg>"``
    - Anything else → ``"unexpected error: <type>: <msg>"``

    For :class:`BaseExceptionGroup`, walks leaves (filtering out
    cancellations) and joins up to the first two with ``" | "``.
    """
    cancelled_exc_class = anyio.get_cancelled_exc_class()
    leaves = [e for e in _iter_leaves(exc) if not isinstance(e, cancelled_exc_class)]
    if not leaves:
        return "supervisor cancelled"
    parts = [_format_one_pause_reason(e) for e in leaves[:2]]
    if len(leaves) > 2:
        parts.append(f"(+{len(leaves) - 2} more)")
    return " | ".join(parts)


def _format_one_pause_reason(exc: BaseException) -> str:
    if isinstance(exc, WorkstationRejected):
        return f"workstation rejected: {exc}"
    if isinstance(exc, ConnectionLost):
        return f"connection lost: {exc!r}"
    if isinstance(exc, (httpx_ws.HTTPXWSException, httpx.HTTPError)):
        return f"connection lost: {exc!r}"
    if isinstance(exc, ProtocolError):
        return f"protocol error: {exc}"
    if isinstance(exc, NotImplementedError):
        return f"unimplemented handler: {exc}"
    return f"unexpected error: {type(exc).__name__}: {exc}"


def _is_wire_disconnect(exc: BaseException) -> bool:
    """Classify an exception (or group) as a recoverable wire
    disconnect.

    Recognized recoverable kinds: :class:`ConnectionLost`,
    :class:`httpx_ws.HTTPXWSException`. Anything else (notably
    :class:`RuntimeError` from a workstation rejection like a
    stale ``active_session``, or a ``CancelledError`` from
    external cancellation) is non-recoverable.

    For groups, every non-cancellation leaf must be a recoverable
    kind. (anyio normally filters out ``CancelledError`` leaves
    that result from the TG's own cancellation, but we filter
    defensively in case a future version preserves them.)
    """
    cancelled_exc_class = anyio.get_cancelled_exc_class()
    leaves = [e for e in _iter_leaves(exc) if not isinstance(e, cancelled_exc_class)]
    if not leaves:
        # All cancellations — propagate the cancellation up.
        return False
    return all(
        isinstance(e, (ConnectionLost, httpx_ws.HTTPXWSException, httpx.HTTPError)) for e in leaves
    )


def _fenced(name: str, body: str) -> str | None:
    """Format one fenced block (``--- {name} ---`` … ``--- end {name} ---``)
    or return ``None`` when the body is empty so callers can skip
    the block entirely."""
    if not body:
        return None
    trimmed = body.rstrip("\n")
    return f"--- {name} ---\n{trimmed}\n--- end {name} ---"


def _append_streams(outcome: str, stdout: str, stderr: str) -> str:
    """Format captured script stdout/stderr plus the mutation summary
    (``outcome``) into the tool-result string the agent sees.

    Streams come first so the summary line stays visually separate
    from script output.

    Empty streams are dropped (no fence markers) so a no-op call
    stays terse. Non-empty streams are fenced with
    ``--- stdout ---`` / ``--- stderr ---`` markers so the agent
    can tell where captured output begins/ends — useful when the
    script printed something that itself looks like the outcome
    summary line, and when stdout and stderr both have content.
    """
    parts: list[str] = []
    for block in (_fenced("stdout", stdout), _fenced("stderr", stderr)):
        if block is not None:
            parts.append(block)
    parts.append(outcome)
    return "\n".join(parts)


#: Synthetic filenames that ``script_runner`` uses for the agent's
#: own code (preamble + the script body). Anything else in the
#: traceback frame list is treated as library / sandbox internals
#: when picking the ``[user]`` frame for the compressed form.
_USER_CODE_FILENAMES: frozenset[str] = frozenset({"<mcp_script>", "preamble.py"})


def _parse_traceback_frames(text: str) -> list[tuple[str, int, str, str]]:
    """Parse ``traceback.format_exception`` output into a flat list
    of ``(filename, lineno, func, source_line)`` tuples in
    bottom-up order (outermost first, innermost last — matching
    Python's default rendering).

    ``source_line`` is the rstripped second-line source preview
    when ``format_exception`` was able to provide one (linecache
    hit); empty string when the next line wasn't a source line
    (rare — e.g. a frame whose filename has no linecache entry).

    Pure string parsing — does not need the live ``exc`` object.
    Robust to leading-context blocks ("The above exception was
    the direct cause of...") and trailing exception-type lines;
    just ignores any line that doesn't match the
    ``File "...", line N, in ...`` pattern.
    """
    out: list[tuple[str, int, str, str]] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _TRACEBACK_FRAME_RE.match(lines[i])
        if m:
            filename, lineno_s, func = m.group(1), m.group(2), m.group(3)
            source = ""
            if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                source = lines[i + 1].strip()
                i += 2
            else:
                i += 1
            out.append((filename, int(lineno_s), func, source))
        else:
            i += 1
    return out


_TRACEBACK_FRAME_RE = re.compile(r'^  File "([^"]+)", line (\d+), in (.+)$')


def _compress_script_traceback(full: str) -> str:
    """Reduce a script-raised traceback to a two-frame summary
    suitable for the default tool-result.

    Output shape::

        {headline}
          [user] {file}:{lineno} in {func}: {source}
          [raise] {file}:{lineno} in {func}: {source}

    - ``[user]`` is the deepest frame whose filename is in
      :data:`_USER_CODE_FILENAMES` (i.e. the script body or the
      preamble — the agent's own code).
    - ``[raise]`` is the very last frame in the traceback —
      unfiltered, even if it's deep in library code. This is
      where the exception actually came out.
    - If the two are the same frame, only emit one line (with
      the ``[user]`` tag, since by definition the raise IS in
      user code).
    - If no user-code frame is found (defensive — shouldn't
      happen since ``script_runner`` always runs user code),
      only emit the ``[raise]`` line.
    - If no frames at all are parseable from ``full`` (also
      defensive), return just the headline.

    The first line of ``full`` is ``script_runner``'s headline
    (``<phase> raised <ExcType>: <msg>``) — we keep that intact.
    The trailing ``<ExcType>: <msg>`` line from
    ``traceback.format_exception`` is dropped because the
    headline already says the same thing.
    """
    if not full:
        return ""
    headline, _, _ = full.partition("\n")
    frames = _parse_traceback_frames(full)
    if not frames:
        return headline

    raise_frame = frames[-1]
    user_frame: tuple[str, int, str, str] | None = None
    for f in reversed(frames):
        if f[0] in _USER_CODE_FILENAMES:
            user_frame = f
            break

    def _render(tag: str, f: tuple[str, int, str, str]) -> str:
        filename, lineno, func, source = f
        head = f"  [{tag}] {filename}:{lineno} in {func}"
        if source:
            return f"{head}: {source}"
        return head

    lines = [headline]
    if user_frame is None:
        lines.append(_render("raise", raise_frame))
    elif user_frame == raise_frame:
        lines.append(_render("user", user_frame))
    else:
        lines.append(_render("user", user_frame))
        lines.append(_render("raise", raise_frame))
    return "\n".join(lines)


def _format_script_error(
    error: str,
    stdout: str,
    stderr: str,
    exception_log: ExceptionLog | None,
) -> str:
    """Tool-result formatter for the script-raised exit path.

    Captured streams and the **compressed** traceback appear first
    so the rollback message reads as a verdict after the evidence.
    Empty sections are omitted.

    Stores the full unmodified traceback string in
    ``exception_log`` (when supplied) and surfaces the resulting
    ``exc_id`` in the rollback message. The agent retrieves the
    full text via the ``get_full_exception(exc_id)`` sandbox
    builtin in a subsequent ``run_script`` call. When
    ``exception_log`` is ``None`` (test paths that don't build a
    full ``AppContext``), the message says
    ``exc_id=unavailable`` and the agent's only signal is the
    compressed summary.

    Closing line explains that queued ops before the raise are
    dropped (nothing was committed).
    """
    compressed = _compress_script_traceback(error)
    if exception_log is not None:
        exc_id = exception_log.store(error)
        retrieval_hint = (
            f" Full traceback stored as exc_id={exc_id!r}; call "
            f"get_full_exception({exc_id!r}) from a future run_script to retrieve."
        )
    else:
        retrieval_hint = " (no exception log configured; full traceback not retained.)"
    rollback = (
        "Error: script raised. All changes made by this script "
        "have been *rolled back* and were not applied." + retrieval_hint
    )
    parts: list[str] = []
    for block in (
        _fenced("stdout", stdout),
        _fenced("stderr", stderr),
        _fenced("traceback", compressed),
    ):
        if block is not None:
            parts.append(block)
    parts.append(rollback)
    return "\n".join(parts)


def _summarize_mutation_outcome(
    runner_queued_ops: list[dict[str, Any]],
    req: "MutateSessionRequest | None",
) -> str:
    """One-line tool-result describing what the script committed.

    ``req`` is ``None`` when ``build_mutate_request`` returned no
    wire mutation (e.g. the script's only side effects were
    MCP-side-only ops like ``present_to_user``). Counts come purely
    from ``runner_queued_ops`` in that case; otherwise we also
    surface the track / untrack counts the request carried so the
    agent can see them.

    Whether the underlying mechanism was a workstation mutation or
    an event-log-only write is intentionally not exposed in the
    wording — both shapes report the same way so the agent isn't
    asked to keep that implementation detail in mind.
    """
    counts: dict[str, int] = {}
    for raw_op in runner_queued_ops:
        kind = raw_op.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{n} {k}" for k, n in sorted(counts.items())]
    extras = []
    if req is not None:
        track_count = sum(1 for op in req.ops if type(op).__name__.startswith("Track"))
        untrack_count = sum(1 for op in req.ops if type(op).__name__.startswith("Untrack"))
        if track_count:
            extras.append(f"{track_count} track")
        if untrack_count:
            extras.append(f"{untrack_count} untrack")
    summary = ", ".join(parts + extras) if (parts or extras) else "no-op"
    return f"Script ran. Committed: {summary}."


def _render_workstation_inbox_notif(payload: Any, seq: int) -> tuple[str, str]:
    """Build the ``(kind, content)`` pair for an inbox message
    channel frame.

    Returns ``kind = "workstation_message"`` so the agent-side
    renderer can distinguish operator-driven messages from
    ordinary callback firings (which keep ``kind="callback"``).

    Content rules:

    - **String payload** (the overwhelmingly common case —
      ``await agent_mcp.send("hello")`` arrives as the bare
      string ``"hello"`` after the wire walker): emit the raw
      string verbatim. No ``repr`` quoting, no truncation, no
      retrieval hint. The agent reads the message in-context
      and can respond directly without calling
      ``retrieve_callback_notification``.
    - **Non-string payload** (dict / list / etc. — operator sent a
      structured message): emit the unabbreviated ``repr`` followed
      by a brief hint pointing at
      ``retrieve_callback_notification('workstation_inbox', <seq>)``
      so the agent knows how to fetch the typed object if they
      need to walk its structure.
    """
    if isinstance(payload, str):
        return "workstation_message", payload
    hint = (
        f" (use retrieve_callback_notification('workstation_inbox', {seq}) " "for the typed object)"
    )
    return "workstation_message", f"{payload!r}{hint}"


def _render_subscription_broken_notif(slot_key: str, reason: str | None) -> str:
    """Build the channel content for a ``subscription_broken`` frame.

    Names the inbox ``slot_key`` whose ``subscribe_to`` feed broke,
    includes the break ``reason`` when the workstation supplied one, and
    points the agent at re-subscribing — the subscription is recoverable
    by calling ``subscribe_to`` again (the inbox slot itself stays open).
    """
    detail = f": {reason}" if reason else ""
    return (
        f"Callback inbox '{slot_key}' lost its subscription{detail}. "
        f"It has stopped receiving events; subscribe_to a source again to "
        f"resume."
    )


# --- Force-close recovery -------------------------------------------


async def force_close_session_one_shot(
    *,
    client_url: str,
    session_id: str,
    timeout_seconds: float = 10.0,
) -> str:
    """Emergency recovery: close a workstation-side session via a
    fresh out-of-band ``/client`` websocket.

    Used by ``close_session(force=True)`` when the supervisor is
    parked or otherwise unable to issue a normal close — e.g.
    when ``subsequent_events`` replay reliably crashes the worker
    before any agent job drains, so a regular ``close_session``
    tool call would never run.

    Steps:

    1. Open a fresh ``aconnect_ws(client_url)`` (see :mod:`fxtr_workstation_mcp.websocket_util`).
    2. Send ``SubscribeSessionRequest(session_id=session_id)``;
       receive ack + snapshot. The snapshot is discarded — we
       don't process any events.
    3. Send ``MutateSessionRequest(ops=[CloseOp()],
       patch=[], ack_seq=0, ...)`` on the new subscription.
       ``ack_seq=0`` is safe: the workstation prunes its event
       log against the cumulative ack, and we don't care about
       prune behavior because the session is being torn down.
    4. Await ``MutationCommittedResponse`` or
       ``MutationRejectedResponse``. Either is treated as success
       — rejection typically means the session was already
       closed by some other path.
    5. Close the websocket.

    The whole flow runs under ``anyio.fail_after(timeout_seconds)``
    (default 10s). If the workstation accepts the connection but
    never sends a response frame (process gone but TCP socket
    lingering, server stuck, etc.), the timeout fires and we
    raise :class:`ForceCloseTimedOut`. The caller's tool-error
    branch surfaces a clear message and leaves local state
    intact (we did not successfully close).

    Returns a short status string for the tool result. Raises
    :class:`ConnectionLost` on network failure;
    :class:`ForceCloseTimedOut` if the overall budget elapses;
    :class:`WorkstationRejected` if subscribe was rejected for a
    reason other than session-already-gone;
    :class:`ProtocolError` if the wire frames are unexpected.

    Runs entirely outside the supervisor — completely isolated
    from the supervisor's state machine. The caller is responsible
    for clearing the toml's ``active_session`` and calling
    :meth:`WorkstationConnection.notify_session_externally_closed`
    after success so the supervisor converges to a no-active-
    session state.

    ``timeout_seconds`` is a kw-only parameter on the function so
    tests can shorten it to a fraction of a second; production
    callers should use the 10s default.
    """
    try:
        with anyio.fail_after(timeout_seconds):
            try:
                ws_cm = aconnect_ws(client_url)
            except httpx_ws.HTTPXWSException as e:
                raise ConnectionLost(f"force_close: could not open /client: {e!r}") from e
            async with ws_cm as ws:
                # Subscribe.
                sub_id = 1
                subscribe_req = SubscribeSessionRequest(request_id=sub_id, session_id=session_id)
                try:
                    await ws.send_text(_REQUEST_ENCODER.encode(subscribe_req).decode())
                except httpx_ws.HTTPXWSException as e:
                    raise ConnectionLost(f"force_close: send subscribe failed: {e!r}") from e
                # Receive subscribe ack.
                ack = await _force_close_receive_frame(ws)
                if isinstance(ack, ErrorResponse):
                    # Treat "session not found" / "session_gone" as success —
                    # the goal (session is gone) is met.
                    msg = (ack.message or "").lower()
                    if "not found" in msg or "session_gone" in msg or "gone" in msg:
                        return f"session {session_id} was already closed on the workstation"
                    raise WorkstationRejected(f"force_close: subscribe rejected: {ack.message}")
                if not isinstance(ack, AckResponse):
                    raise ProtocolError(
                        f"force_close: unexpected first frame on subscribe: {ack!r}"
                    )
                # Receive snapshot (and discard — we don't process events).
                snapshot = await _force_close_receive_frame(ws)
                if not isinstance(snapshot, SessionSnapshotEvent):
                    raise ProtocolError(
                        f"force_close: unexpected second frame on subscribe: {snapshot!r}"
                    )

                # Send the close mutation. Use the snapshot's last_state.seq
                # as ack_seq: the workstation's stale_ack check rejects
                # ack_seq below ``record.last_state_seq`` ("another client
                # has updated the session"). For an MCP that ran bootstrap
                # (which advances last_state_seq via workstation_inbox), the
                # snapshot's seq is non-zero and using it is required.
                close_req = MutateSessionRequest(
                    request_id=2,
                    subscription_id=sub_id,
                    ack_seq=snapshot.last_state.seq,
                    update_id=1,
                    patch=[],
                    ops=[CloseOp()],
                )
                try:
                    await ws.send_text(_REQUEST_ENCODER.encode(close_req).decode())
                except httpx_ws.HTTPXWSException as e:
                    raise ConnectionLost(f"force_close: send close mutation failed: {e!r}") from e
                # Receive committed or rejected. Either is success for our
                # purposes — rejection means "session is already in a
                # state where close has no effect", which is fine.
                response = await _force_close_receive_frame(ws)
                if isinstance(response, MutationCommittedResponse):
                    return f"force-closed session {session_id}"
                if isinstance(response, MutationRejectedResponse):
                    return (
                        "force_close: workstation reported the close was a no-op "
                        f"({response.reason}: {response.message}); session "
                        f"{session_id} is gone"
                    )
                if isinstance(response, ErrorResponse):
                    return (
                        f"force_close: transport error from close ({response.message}); "
                        f"session {session_id} status uncertain — try restarting the MCP"
                    )
                raise ProtocolError(
                    f"force_close: unexpected response to close mutation: {response!r}"
                )
    except TimeoutError as e:
        raise ForceCloseTimedOut(
            f"force_close: timed out after {timeout_seconds}s "
            f"contacting workstation; session {session_id} state unknown "
            "on the workstation side, local toml left as-is so you can retry"
        ) from e


async def _force_close_receive_frame(ws: httpx_ws.AsyncWebSocketSession) -> ServerFrame:
    """Receive one server frame on the one-shot websocket."""
    try:
        text = await ws.receive_text()
    except (httpx_ws.HTTPXWSException, anyio.EndOfStream) as e:
        raise ConnectionLost(f"force_close: receive failed: {e!r}") from e
    try:
        return _SERVER_FRAME_DECODER.decode(text)
    except msgspec.ValidationError as e:
        raise ProtocolError(f"force_close: malformed server frame: {text!r}") from e


# --- /rpc connection helper -----------------------------------------


@contextlib.asynccontextmanager
async def _open_workstation_rpc(
    rpc_url: str,
    *,
    name: str,
) -> AsyncGenerator[WorkstationRpc]:
    """Open the workstation ``/rpc`` connection and yield a
    :class:`WorkstationRpc` for the duration of the block.

    Resource ordering:
    - WebSocket via :func:`~fxtr_workstation_mcp.websocket_util.aconnect_ws`.
    - JSON-framed object stream wrapped to RPC messages.
    - ``open_rpc_scope`` with ``our_main_service=None`` (we don't
      expose any service back to the workstation; we only call into
      the workstation's main service).
    - ``host_scope`` opened mountable + isolated, ``include``-ing the
      RPC session's scope so workstation main is resolvable here.
    - Resolves the workstation proxy once into ``host_scope``; this
      proxy is reused by every per-script :class:`McpHostImpl`. No
      :class:`McpHostImpl` is mounted at the connection level —
      that's a per-``_ScriptJob`` concern handled by the worker.

    Used internally by :class:`WorkstationConnection`'s supervisor;
    not part of the public API. (Tools should access ``rpc`` via
    the connection.)
    """
    async with aconnect_ws(rpc_url) as ws:
        async with _httpx_ws_object_stream(ws) as raw_stream:
            transport = wrap_json_stream(raw_stream)
            async with open_rpc_scope(
                transport,
                our_main_service=None,
                remote_service_type=WorkstationServiceProto,
                isolated=True,
                name=f"{name}/rpc",
            ) as session:
                async with FxtrScope(
                    isolated=True,
                    include=[session.scope],
                    name=f"{name}/host_scope",
                ) as host_scope:
                    # Resolve the workstation proxy once. The proxy
                    # lives in host_scope; results bridged back land
                    # there too, which is reachable from per-script
                    # script_scopes (those inherit through
                    # host_scope).
                    workstation_proxy = await session.remote_service.resolve(host_scope)
                    yield WorkstationRpc(
                        rpc_session=session,
                        host_scope=host_scope,
                        workstation_ref=session.remote_service,
                        workstation_proxy=workstation_proxy,
                    )


@contextlib.asynccontextmanager
async def _httpx_ws_object_stream(
    ws: httpx_ws.AsyncWebSocketSession,
) -> AsyncGenerator[anyio.abc.ObjectStream[JSONTypes]]:
    """Adapter mirroring fxtr_workstation/tests/_ws_client.py's helper.

    The workstation server's wire convention uses
    :class:`JSONObjectStream` so the in-band EOF sentinel works
    consistently on both sides; we mirror it on the client side.
    """

    async def _send(item: JSONTypes) -> None:
        try:
            await ws.send_text(json.dumps(item))
        except httpx_ws.HTTPXWSException as e:
            raise anyio.BrokenResourceError from e

    async def _receive() -> JSONTypes:
        try:
            text = await ws.receive_text()
        except httpx_ws.HTTPXWSException:
            raise anyio.EndOfStream from None
        return json.loads(text)

    stream = JSONObjectStream(_send, _receive)
    try:
        yield stream
    finally:
        await stream.aclose()
