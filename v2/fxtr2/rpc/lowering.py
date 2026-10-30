"""RPC call lowering for LeasedCapIR ("LeasedCapIR lowering protocol version 1").

This module implements a lowering pass that implements a channel for stateful LeasedCapIR objects
in terms of a bidirectional stream of discrete ``RPCMessage`` objects. The concrete wire encoding
of those messages is the responsibility of a separate transport layer (Stage E).

The particular lowering scheme implemented here is heavily based on the [Cap'n'Web][] JSON message
protocol. However, there are some substantial differences. Most notably:
- LeasedCapIR uses a lease-based ownership model, so many of the lowering messages are specific
    to leases, and the connection itself acts as a "regrantor" for any lease that crosses it.
- LeasedCapIR explicitly treats IRFutures as first-class values, since it is designed as an
    intermediate representation for a higher-level user-facing representation.
- Cap'n'Web includes additional JavaScript and web-platform-specific features that we choose not
    to support over this protocol.

[Cap'n'Web]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

See ``lowering_types.py`` for the full protocol specification.

Design sketch
-------------

The top-level object is ``LeasedCapRPCConnection``. It owns:

- A pair of import/export tables (keyed by integer ID) tracking every long-lived reference that
    crosses the connection.
- A background reader task pulling ``RPCMessage``s off the transport and dispatching them to
    per-message handlers (``_on_push`` / ``_on_pull`` / ``_on_resolve`` / etc.).
- An outbound ``memory_object_stream`` fed by synchronous ``_enqueue_message`` calls and drained
    by a background writer task — this lets sync code (``dispatch``, ``push``) allocate import IDs
    and queue a ``PushMessage`` without awaiting.
- Its own task group for reader/writer and for async work triggered by incoming messages
    (unwrapping ``AwaitFutureExpr``s before delivering a request to a local handle, etc.).

The imported-side proxy classes (``RemoteLifetimeToken``, ``RemoteGrant``, ``RemoteIRHandle``,
``RemoteIRFuture``) are stateless frozen dataclasses keyed by ``(connection, id)``. They are
essentially tickets: all mutable state (refcounts, end callbacks, backing leases, resolution state,
etc.) lives on the connection's import-table entry. This gives bounce-back identity for free —
two proxies with the same ID on the same connection compare equal — and lets the connection
revoke every imported grant / close every imported token on abort by iterating the table.

Exported-side (values we hold and refer to by export ID) does not need parallel proxy classes:
an "export" is just a locally-owned object (token / grant-backing-lease / handle / future) plus an
ID in the export table. When we receive a reference back to an export we made, we resolve to the
original object directly.

Regranting is done directly by the connection: when the remote sends us a lease whose grant has
import ID ``i``, we construct ``LocalLifetimeLease(owner=self, grant=RemoteGrant(self, i))``.
The connection itself implements the ``_LeaseOwner`` protocol (``_register_lease`` /
``_deregister_lease`` / ``_clean_up_lease_data``), bookkeeping refcounts in its import table and
synthesizing ``DiscardMessage``s when the last lease for a grant drops.

File layout
-----------

This module (``lowering.py``) contains only ``LeasedCapRPCConnection`` plus thin wrappers that
call into focused submodules:

- ``lowering_errors.py`` — ``RPCConnectionError``, ``HandshakeError``, ``RPCConnectionAborted``
- ``lowering_state.py`` — reserved IDs, ``_ConstantExpr``, ``_ConnectionLeaseCleanupInfo``,
  all 8 ``_*ImportEntry``/``_*ExportEntry`` dataclasses, and the ``_ImportEntry``/``_ExportEntry``
  union aliases
- ``lowering_remote_proxies.py`` — ``RemoteLifetimeToken``, ``RemoteGrant``, ``RemoteIRHandle``,
  ``RemoteIRFuture``
- ``lowering_null_main.py`` — ``_NullMainInterface``
- ``lowering_devaluate.py`` — outbound devaluator
- ``lowering_evaluate.py`` — inbound phase-1 substitution + standalone ``eval_static``
"""

from __future__ import annotations

import contextlib
import logging
import math
import weakref
from typing import Any, AsyncGenerator, Callable, NoReturn, Self, cast

import anyio
import anyio.abc
import anyio.streams.memory

from fxtr2.leases.lease_types import (
    FreshLifetime,
    Grant,
    Lease,
    LeaseBrokenError,
    LifetimeBrand,
    LifetimeToken,
)
from fxtr2.leases.leased_cap_common import PendingResultBox
from fxtr2.leases.leased_cap_ir import (
    IRCallArg,
    IRException,
    IRFuture,
    IRHandle,
    IRStatic,
    IRValue,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import (
    LocalLifetimeLease,
    UnconditionalLocalLifetime,
    _capture_alloc_tb,
    _LeaseCleanupInfo,
    _on_lease_leaked,
)
from fxtr2.rpc.lowering_devaluate import devaluate  # noqa
from fxtr2.rpc.lowering_errors import HandshakeError as HandshakeError  # noqa
from fxtr2.rpc.lowering_errors import RPCConnectionAborted as RPCConnectionAborted  # noqa
from fxtr2.rpc.lowering_errors import RPCConnectionError as RPCConnectionError  # noqa
from fxtr2.rpc.lowering_evaluate import (  # noqa
    eval_as_call_arg,
    eval_as_value,
    eval_forwardable,
    eval_static,
    phase1_protocol_violation,
    phase1_substitute,
)
from fxtr2.rpc.lowering_message_handlers import (  # noqa
    dispatch_message,
    spawn_resolver_for_future_export,
)
from fxtr2.rpc.lowering_null_main import NullMainInterface as NullMainInterface  # noqa
from fxtr2.rpc.lowering_remote_proxies import RemoteGrant as RemoteGrant  # noqa
from fxtr2.rpc.lowering_remote_proxies import RemoteIRFuture as RemoteIRFuture  # noqa
from fxtr2.rpc.lowering_remote_proxies import RemoteIRHandle as RemoteIRHandle  # noqa
from fxtr2.rpc.lowering_remote_proxies import RemoteLifetimeToken as RemoteLifetimeToken  # noqa
from fxtr2.rpc.lowering_state import MAIN_GRANT_ID as MAIN_GRANT_ID  # noqa
from fxtr2.rpc.lowering_state import MAIN_HANDLE_ID as MAIN_HANDLE_ID  # noqa
from fxtr2.rpc.lowering_state import MAIN_TOKEN_ID as MAIN_TOKEN_ID  # noqa
from fxtr2.rpc.lowering_state import (  # noqa
    ConnectionLeaseCleanupInfo as ConnectionLeaseCleanupInfo,
)
from fxtr2.rpc.lowering_state import ConstantExpr as ConstantExpr  # noqa
from fxtr2.rpc.lowering_state import FutureExportEntry as FutureExportEntry  # noqa
from fxtr2.rpc.lowering_state import FutureImportEntry as FutureImportEntry  # noqa
from fxtr2.rpc.lowering_state import GrantExportEntry as GrantExportEntry  # noqa
from fxtr2.rpc.lowering_state import GrantImportEntry as GrantImportEntry  # noqa
from fxtr2.rpc.lowering_state import HandleExportEntry as HandleExportEntry  # noqa
from fxtr2.rpc.lowering_state import HandleImportEntry as HandleImportEntry  # noqa
from fxtr2.rpc.lowering_state import ImportEntryKindMismatch as ImportEntryKindMismatch  # noqa
from fxtr2.rpc.lowering_state import TokenExportEntry as TokenExportEntry  # noqa
from fxtr2.rpc.lowering_state import TokenImportEntry as TokenImportEntry  # noqa
from fxtr2.rpc.lowering_types import (  # noqa
    AbortMessage,
    BreakMessage,
    DiscardMessage,
    Expression,
    HelloMessage,
    RPCMessage,
    StaticExpr,
    UnpullMessage,
)
from fxtr2.util.anyio_util import create_task_group as _create_task_group

logger = logging.getLogger(__name__)


def _noop_deregister() -> None:
    """Shared deregister handle returned whenever there was nothing to register
    (already-broken grant, torn-down entry, etc.). Matches the
    :meth:`Lease.register_broken_callback` shape so callers don't have to special-case.
    """


####################################################################################################
# LeasedCapRPCConnection
####################################################################################################


class LeasedCapRPCConnection[B_Ours: LifetimeBrand, B_Theirs: FreshLifetime[Any]](
    anyio.AsyncContextManagerMixin
):
    """A connection between two LeasedCapIR objects over an RPC transport.

    ``LeasedCapRPCConnection`` implements the v1 LeasedCapIR lowering protocol (see
    ``lowering_types.py``), and is responsible for maintaining the import and export tables of
    the connection as well as re-granting leases and tokens sent across the connection.

    Lifecycle
    ---------

    Two entry patterns are supported, both producing the same runtime behavior:

    1. ``async with LeasedCapRPCConnection(transport, ...) as conn:`` — on entry, opens an
       internal task group, spawns the reader and writer, performs the ``HelloMessage``
       handshake, and yields once the connection is ready. On normal exit, drains (waits for
       the connection to fully drain — i.e. for neither side to hold any non-broken lease
       imported from the other side — then cleanly closes the transport). On any exception,
       aborts: sends an :class:`AbortMessage` best-effort, revokes all imported grants,
       closes all imported tokens, and re-raises.
    2. ``await LeasedCapRPCConnection(transport, ...).start_in(task_group)`` — spawns the
       reader/writer into an externally-supplied task group (actually, into a sub-task-group
       of it, so the connection can still scope its own work). Awaits the handshake and
       returns the connection once it is usable. The background tasks run until the
       connection fully drains, the connection aborts, or the caller's task group is
       cancelled (which attempts a best-effort :class:`AbortMessage` before unwinding).

    Internally, pattern 1 calls into pattern 2 (or vice-versa — TBD at implementation time).

    Main-interface bootstrap
    ------------------------

    At construction, IDs ``0`` / ``1`` / ``2`` are implicitly reserved for the main interface
    (token / handle / grant, in both directions). ``our_main_lease`` is retained as the
    connection's backing lease for export ID ``2`` and is treated for refcount purposes as if
    it had been introduced exactly once (symmetric with the way we treat the peer's main lease
    we import at ID ``2``).

    Regranting
    ----------

    The connection implements the local ``_LeaseOwner`` protocol so that
    :class:`LocalLifetimeLease` can back every imported lease with ``self`` as owner and a
    :class:`RemoteGrant` as its grant. ``_deregister_lease`` removes the lease from the
    corresponding ``_GrantImportEntry.live_leases``; when ``live_leases`` becomes empty we
    send a :class:`DiscardMessage`. On abort, every import grant entry is synthetically
    broken so that all live leases fire their broken-callbacks.

    Re-export identity
    ------------------

    A set of reverse-lookup tables (``_*_export_id_by_*``) maps locally-owned values back to
    their existing export IDs so that repeated sends of the same local object produce equal
    proxies on the receiving side. See the ``_devaluate`` helper for how they are consulted.
    """

    ################################################################################################
    # Internal state
    # --------------
    # Annotated here for discoverability; actually initialized in ``__init__`` / the context
    # manager entry.
    ################################################################################################

    # Import and export tables. Keyed by integer ID; the sign of the ID identifies the
    # original allocator (positive = importer-allocated, negative = exporter-allocated); the
    # table tells us which side we are.
    _imports: dict[int, TokenImportEntry | GrantImportEntry | HandleImportEntry | FutureImportEntry]
    _exports: dict[int, TokenExportEntry | GrantExportEntry | HandleExportEntry | FutureExportEntry]

    # Re-export checker: locally-owned values we have already exported, mapped back to their
    # existing export ID. Consulted by ``_devaluate`` so that sending the same local object
    # twice produces the same ``ExportedXExpr(id)`` both times (and thus the same proxy on
    # the remote side). Entries are removed when the corresponding export-table entry is
    # removed (see ``_on_discard``).
    #
    # Tokens and grants are keyed by the object itself — both are expected to be hashable and
    # to hold only weak references to any mutable state (so using them as dict keys is cheap
    # and doesn't keep heavy state alive). See ``phase_d_prep_todos.md`` for the cleanup that
    # makes this a contract of the ``LifetimeToken`` / ``Grant`` base types.
    #
    # Handles and futures are keyed by ``id(obj)`` — their equality semantics vary across
    # implementations, so identity is the safe choice. The export entry holds a strong
    # reference to the object, which keeps ``id()`` stable for the entry's lifetime.
    _grant_export_id_by_grant: dict[Grant[Any], int]
    _token_export_id_by_token: dict[LifetimeToken[Any], int]
    _handle_export_id_by_handle_id: dict[int, int]
    _future_export_id_by_future_id: dict[int, int]

    # Break tombstones: grant exports we tore down because their backing lease broke
    # (``_subscribe_backing_lease_break``'s ``on_break``), mapped to ``(grant, break
    # reason)``. Export IDs are never reused, so an inbound ``ImportedLeaseExpr``
    # naming one of these is a teardown race — the peer's request crossed our
    # :class:`BreakMessage` on the wire — not a protocol violation. Phase 1
    # substitutes an already-broken lease on the original grant (see
    # ``lowering_evaluate.py``), so the request degrades with the
    # :class:`LeaseBrokenError` the requester already handles instead of aborting
    # the connection. An entry is dropped when the peer's :class:`DiscardMessage`
    # for the id arrives (its acknowledgment that it will never reference the id
    # again); the peer isn't obligated to send one, in which case the entry is
    # retained for the connection's lifetime — small, and consistent with import
    # entries never being removed.
    _broken_grant_export_tombstones: dict[int, tuple[Grant[Any], str]]

    # Next-ID allocators. Positive allocator starts at 3 (skipping the reserved main IDs);
    # negative allocator starts at -1.
    _next_positive_import_id: int
    _next_negative_export_id: int

    # Our view of the peer's next positive-import allocator. Each :class:`PushMessage` the peer
    # sends implicitly allocates 3 sequential positive IDs in the peer's import table (token,
    # future, grant); on our side those IDs land in ``_exports`` at the same numeric values.
    # We track the running counter so ``_on_push`` can register the three new export entries
    # without needing the IDs to be spelled out on the wire. Starts at 3; advances by 3 per
    # incoming push.
    _next_peer_positive_export_id: int

    # Outbox: sync code calls ``_enqueue_message`` to append; the writer loop drains.
    _outbox_send: anyio.streams.memory.MemoryObjectSendStream[RPCMessage]
    _outbox_receive: anyio.streams.memory.MemoryObjectReceiveStream[RPCMessage]

    # Abort state. ``_aborted_reason`` is non-None once the connection has begun aborting.
    _aborted_reason: IRException[Any] | None

    # The connection's internal task group. Set inside ``__asynccontextmanager__`` for the
    # duration of the block; None otherwise. Reader / writer / any spawned work live here.
    _tg: anyio.abc.TaskGroup | None

    # Cached main-interface proxies on the peer side. Populated in ``__init__`` once the
    # main-interface import entries have been registered. Accessible via the public
    # ``their_*`` properties. ``_their_main_token_proxy`` is held by a strong reference
    # (unlike transient token proxies, which live in the ``WeakValueDictionary`` cache
    # below) specifically to keep the main token's ``live_proxy_count`` ≥1 for the
    # connection's lifetime — that's what prevents the §2j token-discard path from
    # spuriously emitting a discard for the reserved main-interface token.
    _their_main_interface_proxy: RemoteIRHandle[B_Theirs]
    _their_main_token_proxy: LifetimeToken[B_Theirs]
    _their_main_lease_obj: Lease[B_Theirs]

    # Singleton proxy caches, keyed by import id. Every ``Remote…`` proxy produced
    # anywhere in the connection (phase-1 substitution, ``.token`` / ``.subject``
    # property accesses, ``dispatch`` return values, ``walk()`` — the last only for
    # tokens/handles; future walks with non-empty ``attr_path`` stay fresh by design)
    # goes through the ``_get_or_create_*_proxy`` helpers below, which consult these
    # caches first. That collapses the churn of repeated property accesses down to one
    # proxy per id and makes :attr:`live_proxy_count` track actual user references
    # rather than per-access bumps.
    #
    # Stored as :class:`weakref.WeakValueDictionary` so cached proxies don't pin
    # themselves — the moment the last external reference drops, the weakref evicts
    # and the proxy's finalizer fires. The main-interface proxies are pinned via
    # dedicated ``_their_main_*_proxy`` attributes so their entries stay alive.
    _token_proxy_cache: weakref.WeakValueDictionary[int, RemoteLifetimeToken[Any]]
    _handle_proxy_cache: weakref.WeakValueDictionary[int, RemoteIRHandle[Any]]
    _future_proxy_cache: weakref.WeakValueDictionary[int, RemoteIRFuture[Any, Any]]

    # Cached main-interface values on our side. Populated in ``__init__`` from the
    # caller's constructor arguments and returned by the ``our_main_*`` properties
    # directly — we prefer caching over a per-access lookup in ``_exports`` because
    # (a) the values are immutable for the connection's lifetime, and (b) the
    # export entry can be torn down by ``on_break`` when the user revokes their
    # main lifetime locally, which would otherwise leave the properties raising.
    _our_main_interface_obj: IRHandle[B_Ours]
    _our_main_grant_obj: Grant[B_Ours]

    # Tracks whether we've already processed a HelloMessage from the peer. A second one
    # (or a non-Hello first message) is a protocol violation → abort.
    _peer_hello_received: bool

    # Set whenever the drain condition becomes satisfied (no non-broken imported-grant
    # leases on our side AND no live outstanding introductions of grants we've exported).
    # The normal-exit path of ``__asynccontextmanager__`` awaits this before tearing down
    # the transport; abort paths also ``set()`` the event so a pending drain wait unwinds
    # promptly. Initially unset; re-checking happens every time a state transition might
    # flip the drain condition (outbound DiscardMessage emission, inbound DiscardMessage,
    # inbound BreakMessage, abort).
    _drain_event: anyio.Event

    # "Active" subsets of the import/export tables. Each set is maintained
    # synchronously at every state transition that enters or leaves the
    # relevant condition, so that drain checks and abort teardown don't have
    # to linearly scan the full tables (which grow unboundedly over long
    # sessions — we never remove entries from ``_imports``).
    #
    # Invariants:
    # - ``_active_grant_imports``: grant import IDs whose entry's
    #   ``live_leases`` is non-empty AND whose ``broken_reason is None``.
    #   These are the grants for which we're holding at least one live lease
    #   on a peer-owned resource — exactly the set of import grants blocking
    #   drain (we can't cleanly shut down while still holding one) and
    #   exactly the set that needs broken-callbacks fired on abort.
    # - ``_active_grant_exports``: grant export IDs whose
    #   ``outstanding_count > 0`` (entry still live, ``backing_lease`` still
    #   held). These block drain and need their backing leases discarded on
    #   abort. Broken grants are removed immediately (see
    #   ``_subscribe_backing_lease_break``) since a break tears the entry
    #   down locally.
    # - ``_active_token_imports``: import token IDs whose entry has
    #   ``ended is False``. Not drain-relevant; used by abort to fire
    #   ``end_callbacks``.
    # - ``_watched_token_exports``: export token IDs with an active
    #   ``watch_deregister``. Added when ``_on_watch`` installs a watch,
    #   removed when the local token ends (our ``on_end`` callback scrubs
    #   the deregister) or on abort. Not drain-relevant; used by abort to
    #   unregister peer-requested end-subscriptions.
    # - ``_active_resolve_obligation_imports``: future import IDs whose
    #   :attr:`FutureImportEntry.resolve_obligation_refcount` is non-zero.
    #   The peer still owes us a resolve (via our own sent Pull or via an
    #   AwaitFutureExpr we received) — drop the entry on inbound
    #   :class:`ResolveMessage` / :class:`RejectMessage` delivery, or on
    #   outbound :class:`UnpullMessage` emission when the last local
    #   consumer drops away. Blocks drain.
    # - ``_active_resolve_obligation_exports``: future export IDs whose
    #   :attr:`FutureExportEntry.resolve_obligation_count` is non-zero. We
    #   still owe the peer a resolve — drop on outbound Resolve/Reject or
    #   when inbound :class:`UnpullMessage` drives the count back to zero.
    #   Blocks drain.
    _active_grant_imports: set[int]
    _active_grant_exports: set[int]
    _active_token_imports: set[int]
    _watched_token_exports: set[int]
    _active_resolve_obligation_imports: set[int]
    _active_resolve_obligation_exports: set[int]

    ################################################################################################
    # Construction / public surface
    ################################################################################################

    def __init__(
        self,
        transport: anyio.abc.ObjectStream[RPCMessage],
        our_main_interface: IRHandle[B_Ours],
        our_main_lease: Lease[B_Ours],
    ):
        """Initialize a connection (without yet running it).

        The connection is not yet usable until it has been entered via ``async with`` or
        started via ``start_in``. Entry is non-blocking — we send our ``HelloMessage``
        immediately but do not wait for the peer's before returning; the peer is free to
        start using our main interface before their Hello reaches us, and we only abort
        if the peer's *first* inbound message is anything other than ``HelloMessage``.
        By the time the CM has entered, the ``their_*`` properties are safe to access
        and outbound calls can be made right away.

        Args:
            transport: An object stream used to send and receive RPC messages. If the stream
                breaks, all imports on this connection are marked broken. The stream is
                automatically closed once the connection fully drains.
            our_main_interface: The main interface exposed by our side.
            our_main_lease: A lease on ``our_main_interface``'s lifetime. Transferred to the
                connection (ownership-wise, via ``.take()``) on construction and retained as
                the backing lease on the main grant (export ID 2) until the remote side
                discards it. If the caller never enters the async CM, the lease has still
                been consumed — leaks are cleaned up via the usual weakref-finalizer path
                on the wrapped ``UnconditionalLocalLifetime``.
        """
        self._transport = transport
        self._tg = None
        self._aborted_reason = None
        self._peer_hello_received = False
        self._drain_event = anyio.Event()

        self._imports = {}
        self._exports = {}
        self._grant_export_id_by_grant = {}
        self._token_export_id_by_token = {}
        self._handle_export_id_by_handle_id = {}
        self._future_export_id_by_future_id = {}
        self._broken_grant_export_tombstones = {}
        # Active sets: start empty. Populated below as main-interface entries
        # are registered. Maintained synchronously at every future state
        # transition; see class docstring for invariants.
        self._active_grant_imports = set()
        self._active_grant_exports = set()
        self._active_token_imports = set()
        self._watched_token_exports = set()
        self._active_resolve_obligation_imports = set()
        self._active_resolve_obligation_exports = set()
        self._next_positive_import_id = 3
        self._next_negative_export_id = -1
        self._next_peer_positive_export_id = 3

        # Proxy singleton caches — see field docstrings. Must exist before
        # ``_get_or_create_*_proxy`` or any ``Remote…`` construction runs.
        self._token_proxy_cache = weakref.WeakValueDictionary()
        self._handle_proxy_cache = weakref.WeakValueDictionary()
        self._future_proxy_cache = weakref.WeakValueDictionary()

        # Unbounded outbox: ``_enqueue_message`` must be synchronous for use by sync code
        # (``dispatch`` / ``push`` allocate import IDs atomically with enqueue).
        self._outbox_send, self._outbox_receive = anyio.create_memory_object_stream[RPCMessage](
            math.inf
        )
        # Enqueue the handshake first, so anything the caller does between
        # construction and ``async with`` (e.g. ``as_server``'s
        # ``their_main_lease.discard()``) enqueues behind it. The writer loop
        # hasn't started yet — messages just sit in the outbox until the CM
        # opens and spawns the writer.
        self._outbox_send.send_nowait(HelloMessage())

        # Main-interface export side: register ``our_main_*`` at IDs 0/1/2 and seed the
        # re-export dicts. Taking the caller's lease transfers ownership immediately —
        # downstream cleanup flows through ``_exports[MAIN_GRANT_ID].backing_lease``.
        our_main_token = our_main_interface.token
        our_backing_lease = our_main_lease.take()

        # Cache for the ``our_main_*`` properties; see field docstring for why
        # we don't just look these up through ``_exports``.
        self._our_main_interface_obj = our_main_interface
        self._our_main_grant_obj = our_backing_lease.grant

        token_export_entry = self._register_token_export(
            export_id=MAIN_TOKEN_ID, local_token=our_main_token
        )
        self._register_handle_export(
            export_id=MAIN_HANDLE_ID,
            token_export_id=MAIN_TOKEN_ID,
            local_handle=our_main_interface,
        )
        self._register_grant_export(
            export_id=MAIN_GRANT_ID,
            token_export_id=MAIN_TOKEN_ID,
            backing_lease=our_backing_lease,
        )
        # Pair-bump the subject token's ``outstanding_count`` for each of
        # the handle + grant registrations. This mirrors the protocol rule
        # "every ``Exported…Expr`` that carries a ``token_export_id`` is
        # one introduction of that token" — phase-1's dependent-import
        # helpers follow the rule naturally; the main-interface path
        # registers each entry directly, so we bump by hand. Without the
        # pair-bump, peer's ``token_export.outstanding_count`` would be
        # higher than ours and eventual discard accounting wouldn't
        # balance.
        token_export_entry.outstanding_count += 2  # one per dep

        # Main-interface import side: reserve IDs 0/1/2 symmetrically.
        # Three sequential ``_introduce_*_import`` calls; the handle and
        # grant helpers internally bump the subject token's
        # ``introduction_refcount``, so the token ends at the correct
        # refcount=3 (one implicit for the token itself, plus one each
        # from the handle + grant dep contributions).
        #
        # The main token/handle proxies are pinned by
        # ``_their_main_{token,interface}_proxy`` for the connection's
        # lifetime, so these counts are never retired over the wire on
        # a clean shutdown — the abort path short-circuits the GC
        # finalizers.
        self._introduce_token_import(MAIN_TOKEN_ID)
        self._introduce_handle_import(MAIN_HANDLE_ID, MAIN_TOKEN_ID)
        self._introduce_grant_import(MAIN_GRANT_ID, MAIN_TOKEN_ID)

        # Peer main-interface proxies. Go through the cache helpers so the
        # singleton invariant holds even for the reserved main-interface
        # ids — any subsequent call to ``_get_or_create_*_proxy(0)`` /
        # ``(1)`` returns these exact objects. Store strong refs on the
        # connection to pin ``live_proxy_count`` ≥ 1 for the lifetime of
        # the connection; together with the pin chain on the main lease's
        # grant, this keeps the whole main-interface proxy triple alive
        # and prevents their GC finalizers from firing until abort.
        main_token_proxy = self._get_or_create_token_proxy(MAIN_TOKEN_ID)
        self._their_main_token_proxy = cast("LifetimeToken[B_Theirs]", main_token_proxy)
        self._their_main_interface_proxy = cast(
            "RemoteIRHandle[B_Theirs]",
            self._get_or_create_handle_proxy(MAIN_HANDLE_ID),
        )
        # Construct the lease last so ``_register_lease`` can find the grant
        # import entry it was just registered in.
        self._their_main_lease_obj = cast(
            Lease[B_Theirs],
            LocalLifetimeLease(
                owner=self,
                grant=RemoteGrant(
                    conn=self, grant_import_id=MAIN_GRANT_ID, _token=main_token_proxy
                ),
            ),
        )

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self, None]:
        """Run the connection as an async context manager.

        Entry: opens the connection's task group, spawns reader + writer, and enqueues our
        ``HelloMessage`` immediately (we don't block waiting for the peer's — the peer is
        free to start using the connection before its Hello reaches us, and we only abort
        if the peer's *first* inbound message is anything other than ``HelloMessage``).
        Yields ``self`` to the user.

        Normal exit: signals the end of our usage of the peer's main interface by
        discarding ``their_main_lease`` (which emits a :class:`DiscardMessage` to the
        peer via the standard lease-owner path), then waits for the session to fully
        drain — no non-broken leases outstanding on either our grant imports or our
        grant exports. Once drained, closes the outbox so the writer emits ``send_eof``
        on the transport; the reader then unwinds on the peer's own ``send_eof``, and
        the task group exits naturally.

        If the peer never releases the grants we exported, the drain wait blocks
        indefinitely. That's the intended "I'm done but waiting for the remote to
        agree" pattern; callers who want to bound the wait should wrap the ``async
        with`` in their own cancel scope.

        Exceptional exit: calls ``_abort`` with the exception as the reason, re-raising
        after the abort completes its synchronous bookkeeping. ``_abort`` also sets
        ``_drain_event`` so a pending drain wait on this path unwinds promptly.
        """
        # Use the collapse-friendly task group so a single-cause failure inside the
        # connection's body propagates as the inner exception rather than wrapped in a
        # ``BaseExceptionGroup``. Callers that need to inspect concurrent failures can
        # still get a group when there are multiple distinct exceptions.
        try:
            async with _create_task_group(collapse_exc_group=True) as tg:
                self._tg = tg
                tg.start_soon(self._reader_loop)
                tg.start_soon(self._writer_loop)
                try:
                    yield self
                    # Normal-exit shutdown.
                    # (1) Release our implicit hold on the peer's main interface.
                    #     Idempotent if the user already discarded it explicitly.
                    #     ``_clean_up_lease_data`` emits the outbound
                    #     ``DiscardMessage(import_id=2, refcount=1)`` when this
                    #     drops the last lease on the main grant.
                    self._their_main_lease_obj.discard()
                    # (2) Wait for full drain — see :meth:`_check_drain`. Re-check
                    #     once synchronously now in case we already satisfied the
                    #     condition (e.g. server-side ``as_server`` discarded its
                    #     main lease at construction and the peer's discard already
                    #     arrived).
                    self._check_drain()
                    await self._drain_event.wait()
                    # (3) Close the outbox. The writer drains any remaining messages
                    #     (including the Hello we queued at entry), sends ``send_eof``
                    #     to signal the peer, and exits. Our reader then exits on the
                    #     peer's own ``send_eof``. The task group unwinds naturally
                    #     once both tasks finish.
                    self._outbox_send.close()
                except BaseException as exc:
                    # Any error — from the user body OR from the shutdown path above —
                    # triggers abort. ``_abort`` does local cleanup + closes the outbox;
                    # re-raising lets the exception propagate out of the async CM (and
                    # through the enclosing task group, which naturally cancels the
                    # reader/writer tasks if they haven't already unwound).
                    self._abort(
                        IRException(
                            message=f"exception in connection block: {exc}",
                            category=type(exc).__name__,
                        )
                    )
                    raise
        finally:
            self._tg = None

    async def start_in(self, task_group: anyio.abc.TaskGroup) -> Self:
        """Start the connection's background tasks inside an existing task group.

        Not yet implemented — Stage D currently only supports the ``async with`` entry
        pattern. Added in a later substage; the shape is sketched in the plan.
        """
        raise NotImplementedError("TODO: Stage D — start_in pattern not yet implemented")

    @property
    def our_main_interface(self) -> IRHandle[B_Ours]:
        """The main interface exposed by our side of the connection.

        Cached at construction; safe to access at any time during the connection's
        lifetime (including after the main-grant export has been torn down by a
        local revocation).
        """
        return self._our_main_interface_obj

    @property
    def our_main_grant(self) -> Grant[B_Ours]:
        """The grant on our main interface that backs export ID 2.

        Corresponds to ``our_main_lease`` passed at construction; the original lease has been
        transferred to the connection and is no longer directly accessible.
        """
        return self._our_main_grant_obj

    @property
    def their_main_interface(self) -> IRHandle[B_Theirs]:
        """The remote side's main interface, as a :class:`RemoteIRHandle` at import ID 1."""
        return self._their_main_interface_proxy

    @property
    def their_main_lease(self) -> Lease[B_Theirs]:
        """The lease on the remote main interface that the peer handed us during setup.

        A :class:`LocalLifetimeLease` backed by ``RemoteGrant(self, 2)``. Holding and using
        this lease is what keeps the connection's pipeline open; discarding it (and all
        clones) signals to the peer that we no longer need the remote main interface.
        """
        return self._their_main_lease_obj

    @classmethod
    def as_client(
        cls, transport: anyio.abc.ObjectStream[RPCMessage]
    ) -> LeasedCapRPCConnection[Any, B_Theirs]:
        """Convenience: create a client connection with a null main interface on our side.

        The connection exports a non-callable placeholder as ``our_main_interface``; any
        incoming call targeting it fails with an :class:`IRException`. The placeholder's
        lifetime is kept alive implicitly by the backing lease we store at export ID 2 —
        when the connection shuts down and that lease is discarded, the null lifetime's
        last live lease is gone and the token auto-closes.
        """
        null_lifetime = UnconditionalLocalLifetime[Any]("client_null_main")
        null_handle = NullMainInterface[Any](null_lifetime.token)
        null_lease = null_lifetime.root_lease.take()
        return cls(transport, null_handle, null_lease)

    @classmethod
    def as_server(
        cls,
        transport: anyio.abc.ObjectStream[RPCMessage],
        our_main_interface: IRHandle[B_Ours],
        our_main_lease: Lease[B_Ours],
    ) -> LeasedCapRPCConnection[B_Ours, Any]:
        """Convenience: create a server connection and discard the remote's main lease.

        Equivalent to constructing normally and then immediately calling
        ``conn.their_main_lease.discard()`` — suitable when we do not plan to call back into
        the client.
        """
        conn = cls(transport, our_main_interface, our_main_lease)
        conn.their_main_lease.discard()
        return cast(LeasedCapRPCConnection[B_Ours, Any], conn)

    def push[
        RB: FreshLifetime[Any]
    ](self, lease: Lease[B_Theirs], value: IRCallArg,) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        """Directly push a value to the remote side without processing it there.

        This method can be used to "prepare" some value on the remote side: sending it
        immediately, and then using it via :class:`UnwrapForPipelining` or
        :meth:`IRFuture.pipeline_dispatch` later.

        Note: Included primarily to demonstrate and test the ability to use the ``push``
        message without a top-level call. ``value`` may contain ``UnwrapForPipelining``
        objects if needed — the devaluator rewrites them as ``AwaitFutureExpr`` sub-expressions.

        Args:
            lease: A lease on the remote main interface (acts as authority to push on this
                connection — a closed remote main interface means we cannot push anymore).
                Consumed.
            value: The value to push. May contain any :class:`IRCallArg`, including nested
                leases, handles, futures, and ``UnwrapForPipelining`` wrappers.

        Returns:
            ``(future, lease)`` for the pushed value: ``future`` resolves to the final
            (unwrapped) value on the remote side, ``lease`` keeps the remote copy alive.
        """
        raise NotImplementedError("TODO: Stage D")

    ################################################################################################
    # _LeaseOwner protocol
    # ----------------------
    # The connection owns every imported lease. ``LocalLifetimeLease(owner=self, grant=…)`` is
    # used as the concrete lease type; the connection bookkeeps the per-grant refcount on its
    # ``_GrantImportEntry`` and emits ``DiscardMessage``s when an import's outstanding-lease
    # count reaches zero.
    ################################################################################################

    def _register_lease(self, lease: LocalLifetimeLease[Any]) -> None:
        """Register a newly-constructed imported lease with this connection.

        Validates that ``lease.grant`` is a :class:`RemoteGrant` naming this connection at
        an import ID that resolves to a live :class:`_GrantImportEntry`, then inserts the
        lease into the entry's ``live_leases`` dict under a ``weakref.finalize``-based
        tracking tuple (same pattern as :class:`UnconditionalLocalLifetime`).
        """
        grant = lease.grant
        if not isinstance(grant, RemoteGrant) or grant.conn is not self:
            raise RuntimeError(
                f"Lease {lease!r} does not belong to {self!r}: grant is not a"
                " RemoteGrant on this connection."
            )
        grant_import_id = grant.grant_import_id
        entry = self._imports.get(grant_import_id)
        if not isinstance(entry, GrantImportEntry):
            raise RuntimeError(
                f"Lease {lease!r} references grant import id {grant_import_id}, which is"
                f" not a live grant entry on {self!r}."
            )
        if entry.broken_reason is not None:
            raise LeaseBrokenError(
                f"Cannot issue lease on broken grant {grant_import_id} ({entry.broken_reason!r})."
            )
        lease_id = id(lease)
        info = ConnectionLeaseCleanupInfo(
            lease_repr=repr(lease),
            alloc_tb=_capture_alloc_tb(),
            grant_import_id=grant_import_id,
        )
        finalizer = weakref.finalize(lease, _on_lease_leaked, weakref.ref(self), lease_id, info)
        entry.live_leases[lease_id] = (info, finalizer)
        # Transitioned from "no lease"/"not broken" to "has a lease"/"not
        # broken" — mark the import grant as actively holding a peer-owned
        # resource. ``broken_reason is None`` was asserted above (we raise
        # :class:`LeaseBrokenError` if broken). ``set.add`` is idempotent,
        # so calling on every lease registration is fine.
        self._active_grant_imports.add(grant_import_id)

    def _deregister_lease(self, lease: LocalLifetimeLease[Any]) -> None:
        """Deregister an imported lease on explicit discard.

        Detaches the weakref finalizer (so the leak-warning path doesn't later fire on
        this lease) and then delegates to :meth:`_clean_up_lease_data`.
        """
        grant = lease.grant
        if not isinstance(grant, RemoteGrant) or grant.conn is not self:
            raise RuntimeError(
                f"Lease {lease!r} does not belong to {self!r}: grant is not a"
                " RemoteGrant on this connection."
            )
        grant_import_id = grant.grant_import_id
        entry = self._imports.get(grant_import_id)
        if not isinstance(entry, GrantImportEntry):
            # Entry already torn down (e.g. by abort). Idempotent no-op.
            return
        lease_id = id(lease)
        tracked = entry.live_leases.get(lease_id)
        if tracked is None:
            # Double-discard or race with finalizer. Idempotent no-op.
            return
        info, finalizer = tracked
        finalizer.detach()
        self._clean_up_lease_data(lease_id, info)

    def _clean_up_lease_data(self, lease_id: int, info: _LeaseCleanupInfo) -> None:
        """Shared cleanup path used by both explicit discard and the weakref-finalizer leak
        path. Removes the lease from its owning grant entry's ``live_leases`` dict.

        When the dict empties and the grant was not broken by an inbound
        :class:`BreakMessage`, emit a :class:`DiscardMessage` for the accumulated
        ``introduction_refcount`` and zero it — signalling the peer that all the
        introductions they've made of this grant can be released on their side. A
        subsequent re-introduction starts a fresh count. If the grant was already broken
        by the peer (``broken_reason is not None``), skip the discard emission: the peer
        has already torn the export entry down on their side, so there's nothing to
        refcount.

        Also signals ``_drain_event`` if this transition satisfies the drain condition
        (used by the normal-exit path in :meth:`__asynccontextmanager__`).
        """
        if not isinstance(info, ConnectionLeaseCleanupInfo):
            # Protocol shape satisfies _LeaseCleanupInfo but the connection only ever
            # registers its own info type. If anything else gets here it's a bug.
            raise RuntimeError(
                f"{self!r}._clean_up_lease_data received unexpected info of type"
                f" {type(info).__name__}: {info!r}"
            )
        entry = self._imports.get(info.grant_import_id)
        assert isinstance(entry, GrantImportEntry), (
            f"_clean_up_lease_data(grant_import_id={info.grant_import_id}):"
            " no live GrantImportEntry. The grant entry is only removed when"
            " live_leases drains to empty — a subsequent cleanup for the same"
            " lease shouldn't fire, because discard is idempotent and the"
            " weakref finalizer is detached on break."
        )
        entry.live_leases.pop(lease_id, None)
        if entry.live_leases:
            return
        # ``live_leases`` just emptied. Leave the active-imports set; a
        # subsequent re-register (rare but possible if the peer is
        # re-introducing grants faster than we're discarding) will re-add.
        self._active_grant_imports.discard(info.grant_import_id)
        # Emit the outbound DiscardMessage unless the grant was already broken
        # by an inbound BreakMessage — peer already released their grant
        # export in that case. The subject token's share of introductions
        # (one per ``ExportedLeaseExpr``) stays accumulated in
        # ``token_entry.introduction_refcount`` and is retired later, in one
        # go, when the cached token proxy GCs — see the "Proxy-GC tracking"
        # comment in ``lowering_remote_proxies.py``.
        if entry.introduction_refcount > 0 and entry.broken_reason is None:
            refcount = entry.introduction_refcount
            entry.introduction_refcount = 0
            self._enqueue_message(DiscardMessage(import_id=entry.import_id, refcount=refcount))
        self._maybe_remove_grant_import(entry)
        self._check_drain()

    def _maybe_remove_grant_import(self, entry: GrantImportEntry) -> None:
        """Remove a grant import entry from ``_imports`` iff it is fully
        drained: no live leases AND no pending wait-break subscriptions.

        Three call sites converge here:

        - :meth:`_clean_up_lease_data` after draining ``live_leases`` and
          (on the un-broken path) emitting the outbound
          :class:`DiscardMessage` that zeros ``introduction_refcount``.
        - The deregister closure returned by
          :meth:`_register_wait_break_callback`, after a wait's
          ``finally`` removes its subscription.
        - :meth:`_mark_grant_import_broken`, after an inbound
          :class:`BreakMessage` clears both dicts.

        On the un-broken paths, ``introduction_refcount`` is always zero
        at entry — :meth:`_clean_up_lease_data` emits the outbound
        :class:`DiscardMessage` and zeros the counter before routing
        here. On the broken path, the counter may still be non-zero
        because the peer already tore down their ``grant_export`` and
        we deliberately suppress the outbound discard; the leftover is
        orphan bookkeeping that disappears with the entry. The assert
        catches a bookkeeping bug on the un-broken path without
        interfering with the broken teardown.
        """
        if entry.live_leases:
            return
        if entry.wait_break_callbacks:
            return
        assert entry.introduction_refcount == 0 or entry.broken_reason is not None, (
            f"_maybe_remove_grant_import(id={entry.import_id}): un-broken"
            " grant entry has non-zero introduction_refcount"
            f" ({entry.introduction_refcount}) — the un-broken removal path"
            " should have zeroed it via the outbound DiscardMessage"
        )
        del self._imports[entry.import_id]

    ################################################################################################
    # ID allocators
    ################################################################################################

    def _allocate_push_import_ids(self) -> tuple[int, int, int]:
        """Atomically allocate three sequential positive import IDs for a push.

        Returns ``(token_id, future_id, grant_id)``. The three IDs are always consecutive
        — the recipient relies on that to mirror the allocation in its export table and
        to derive the grant ID as ``future_id + 1`` (see :class:`PullMessage`).
        """
        token_id = self._next_positive_import_id
        future_id = token_id + 1
        grant_id = future_id + 1
        self._next_positive_import_id = grant_id + 1
        return token_id, future_id, grant_id

    def _allocate_peer_push_export_ids(self) -> tuple[int, int, int]:
        """Mirror of :meth:`_allocate_push_import_ids` for the peer's push allocations.

        The peer's :class:`PushMessage` implicitly consumes three sequential positive IDs
        from *its* import allocator; those same IDs land in our export table at the same
        numeric values. We track the running counter locally (no wire signalling) and
        hand out ``(token_id, future_id, grant_id)`` triples as pushes arrive.
        """
        token_id = self._next_peer_positive_export_id
        future_id = token_id + 1
        grant_id = future_id + 1
        self._next_peer_positive_export_id = grant_id + 1
        return token_id, future_id, grant_id

    def _allocate_export_id(self) -> int:
        """Allocate the next negative export ID.

        Used when devaluating a locally-owned value we have not yet exported. Each call
        decrements by one, starting at -1.
        """
        export_id = self._next_negative_export_id
        self._next_negative_export_id -= 1
        return export_id

    def _check_not_aborted(self) -> None:
        """Raise :class:`RPCConnectionAborted` if the connection has aborted."""
        if self._aborted_reason is not None:
            raise RPCConnectionAborted(str(self._aborted_reason))

    def _abort(self, reason: IRException[Any], *, notify_peer: bool = True) -> None:
        """Abort the connection locally.

        Idempotent. Performs only local cleanup:
            - (if ``notify_peer``) enqueues an :class:`AbortMessage` carrying ``reason``
              onto the outbox so the writer flushes it before closing the transport;
            - marks the connection aborted,
            - breaks every imported grant (firing broken-callbacks on still-live
              imported leases),
            - marks every imported token ended (firing end-callbacks),
            - discards our backing leases on every grant export,
            - unsubscribes any active token-export watches,
            - closes the outbox (so the writer drains its queued messages, ``send_eof``s
              the transport, and exits).

        Deliberately does NOT cancel the connection's task group or raise an exception.
        The task group is the reader/writer's native signalling mechanism: when they
        detect an abort-worthy condition they call ``_abort`` and then raise an
        exception into the task group themselves, which anyio uses to cancel the
        cancel scope and propagate the error to the surrounding ``async with``. Code
        that initiates an abort from outside the loops (e.g. the CM body's exception
        handler) is expected to follow a similar "cleanup then raise" pattern.

        Args:
            reason: The :class:`IRException` describing why the connection is aborting.
            notify_peer: If True (the default), enqueue an outbound
                :class:`AbortMessage` so the peer learns of the abort via a graceful
                protocol message rather than only via transport close. Set False when
                we're aborting in response to the *peer's* :class:`AbortMessage` — the
                peer has already notified us and the protocol forbids replying.
        """
        if self._aborted_reason is not None:
            return
        # Enqueue the outbound AbortMessage BEFORE marking aborted so the
        # ``_aborted_reason`` gate in ``_enqueue_message`` doesn't drop it. The writer
        # will flush it and ``send_eof`` before exiting. ``reason`` is an
        # :class:`IRException`, which satisfies :class:`IRStatic` — wrapping it in a
        # :class:`StaticExpr` produces a well-formed, stub-free payload (as the
        # protocol requires for ``AbortMessage``).
        if notify_peer:
            self._enqueue_message(AbortMessage(expression=StaticExpr(reason)))
        self._aborted_reason = reason
        reason_str = reason.message

        # Tear down every still-live entry. The four ``_active_*`` sets are
        # exactly the entries that still have observable state (leases to
        # break, tokens to end, backing leases to discard, watches to
        # deregister); everything else has already been torn down by its own
        # teardown path. Delegate each iteration to the symmetric
        # ``_mark_*_import_*`` / ``_tear_down_*_export`` helpers — each
        # helper removes the entry from its set, so iterate over a snapshot
        # list to avoid mutating mid-walk.
        #
        # TODO: the ``assert``s inside the helpers are useful invariant-
        # breakage detectors during active development, but on a teardown
        # path that may run *because* things are already going sideways, a
        # failed assertion leaves the connection half-torn-down. Once the
        # implementation is more mature, consider downgrading the per-helper
        # asserts to ``logger.error`` + ``continue`` so abort is best-effort
        # under all conditions.
        for grant_import_id in list(self._active_grant_imports):
            entry = self._imports[grant_import_id]
            assert isinstance(entry, GrantImportEntry), (
                f"_active_grant_imports contains {grant_import_id!r}, but"
                f" _imports[{grant_import_id}] is {entry!r}"
            )
            self._mark_grant_import_broken(entry, reason_str)
        for token_import_id in list(self._active_token_imports):
            entry = self._imports[token_import_id]
            assert isinstance(entry, TokenImportEntry), (
                f"_active_token_imports contains {token_import_id!r}, but"
                f" _imports[{token_import_id}] is {entry!r}"
            )
            self._mark_token_import_ended(entry)
        for grant_export_id in list(self._active_grant_exports):
            entry = self._exports[grant_export_id]
            assert isinstance(entry, GrantExportEntry), (
                f"_active_grant_exports contains {grant_export_id!r}, but"
                f" _exports[{grant_export_id}] is {entry!r}"
            )
            self._tear_down_grant_export(entry)
        for token_export_id in list(self._watched_token_exports):
            entry = self._exports[token_export_id]
            assert isinstance(entry, TokenExportEntry), (
                f"_watched_token_exports contains {token_export_id!r}, but"
                f" _exports[{token_export_id}] is {entry!r}"
            )
            self._tear_down_token_export(entry)

        # Unblock any pending drain wait in the normal-exit path — abort satisfies
        # "done draining" by fiat.
        self._drain_event.set()

        # Close outbox — writer drains any pending messages, ``send_eof``s, and exits.
        # Our reader then unwinds on the peer's EOF (or on cancellation propagated from
        # a sibling task's raised exception).
        try:
            self._outbox_send.close()
        except anyio.ClosedResourceError:
            pass

    ################################################################################################
    # Reader / writer loops
    ################################################################################################

    async def _reader_loop(self) -> None:
        """Background task: pull messages off the transport, dispatch to handlers.

        Exits cleanly on peer EOF. On any other error (transport failure, handler
        exception — including ``NotImplementedError`` for not-yet-wired handlers in
        incremental Stage D bodies) calls ``_abort`` for local cleanup, then raises
        :class:`RPCConnectionAborted`. The raise propagates into the connection's
        task group, where anyio uses it to cancel sibling tasks and surface the
        error out of the enclosing ``async with``.
        """
        while True:
            try:
                msg = await self._transport.receive()
            except anyio.EndOfStream:
                # Peer closed the transport — clean connection end.
                return
            except (anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
                reason = IRException(
                    message=f"transport read error: {exc}",
                    category=type(exc).__name__,
                )
                self._abort(reason)
                raise RPCConnectionAborted(reason.message) from exc
            if self._aborted_reason is not None:
                return
            try:
                self._dispatch_message(msg)
            except RPCConnectionError:
                # Handler already initiated the abort (and possibly raised a specific
                # subclass such as ``HandshakeError``); propagate as-is rather than
                # re-wrapping.
                raise
            except Exception as exc:
                reason = IRException(
                    message=f"error dispatching {type(msg).__name__}: {exc}",
                    category=type(exc).__name__,
                )
                self._abort(reason)
                raise RPCConnectionAborted(reason.message) from exc

    async def _writer_loop(self) -> None:
        """Background task: drain the outbox to the transport, in order.

        Two phases:

        1. Main loop — receive the next outbox message and send it. Transport-write
           errors call ``_abort`` and raise :class:`RPCConnectionAborted` into the task
           group. Exits on :class:`anyio.EndOfStream` when the outbox has been closed
           and drained.
        2. Shielded cleanup — regardless of whether we exit via the normal drain path
           or via cancellation (e.g. from a sibling task raising an abort-worthy
           error), drain any messages still buffered in the outbox and ``send_eof`` on
           the transport so the peer's reader can unwind cleanly. Running this in a
           shielded cancel scope is what guarantees the peer sees our
           ``AbortMessage``: when the reader calls ``_abort`` and raises
           ``RPCConnectionAborted``, the task group cancels us before we've dequeued
           the ``AbortMessage`` that ``_abort`` enqueued, so the shielded cleanup is
           what actually gets it onto the wire.
        """
        try:
            while True:
                try:
                    msg = await self._outbox_receive.receive()
                except anyio.EndOfStream:
                    break
                # Shield the transport send: once we've dequeued a message from the
                # outbox, we need to actually put it onto the wire before accepting
                # cancellation. Otherwise a cancel arriving between ``receive`` and
                # ``send`` — e.g. because a sibling task raised an abort-worthy error —
                # drops the message on the floor, and we lose messages like the
                # :class:`HelloMessage` we queued at connection entry or any
                # ``AbortMessage`` enqueued by ``_abort``.
                with anyio.CancelScope(shield=True):
                    try:
                        await self._transport.send(msg)
                    except (
                        anyio.ClosedResourceError,
                        anyio.BrokenResourceError,
                    ) as exc:
                        reason = IRException(
                            message=f"transport write error: {exc}",
                            category=type(exc).__name__,
                        )
                        self._abort(reason)
                        raise RPCConnectionAborted(reason.message) from exc
        finally:
            # Shielded drain + EOF. Cancellation during cleanup is deferred until the
            # inner work finishes so the peer always sees our final ``AbortMessage`` +
            # EOF when one was pending.
            with anyio.CancelScope(shield=True):
                await self._drain_and_eof()

    async def _drain_and_eof(self) -> None:
        """Drain any remaining buffered messages and signal EOF to the peer.

        Invoked from the writer's shielded finally block. Any transport-level errors
        here are swallowed — the transport may be half-closed by the peer already, and
        we're on the shutdown path regardless.
        """
        # Close our send half of the outbox if it isn't already, so we see EndOfStream
        # when the buffer runs out (otherwise receive_nowait's only way to signal empty
        # is WouldBlock).
        try:
            self._outbox_send.close()
        except anyio.ClosedResourceError:
            pass
        while True:
            try:
                msg = self._outbox_receive.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream):
                break
            # OSError alongside the anyio errors: anyio's raw UNIXSocketStream
            # (unlike its TCP SocketStream) does not translate socket errors —
            # send_eof's shutdown() on a peer-closed UDS raises ENOTCONN on
            # macOS, and sends can leak BrokenPipeError. All equally benign on
            # the shutdown path.
            try:
                await self._transport.send(msg)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                break
        try:
            await self._transport.send_eof()
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            pass

    def _dispatch_message(self, msg: RPCMessage) -> None:
        dispatch_message(self, msg)

    def _enqueue_message(self, msg: RPCMessage) -> None:
        """Synchronously enqueue a message onto the outbox.

        Used by sync code that just allocated import IDs for a push: the message is committed
        to the queue atomically with the ID allocation, and the writer will send it in order.

        If the connection has aborted (or the outbox has been closed by shutdown), the
        message is silently dropped.
        """
        if self._aborted_reason is not None:
            return
        try:
            self._outbox_send.send_nowait(msg)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.WouldBlock):
            # Unbounded buffer never gets WouldBlock in practice; closed/broken can happen
            # during shutdown. Either way, silently drop: we're tearing down.
            pass

    ################################################################################################
    # Drain accounting
    ################################################################################################

    def _check_drain(self) -> None:
        """Set ``_drain_event`` if the drain condition is satisfied right now.

        Drain condition — all four active sets empty:

        - :attr:`_active_grant_imports`: no live imported leases we're holding on
          peer-owned resources.
        - :attr:`_active_grant_exports`: no un-discarded grants the peer still holds
          on our resources.
        - :attr:`_active_resolve_obligation_imports`: the peer owes us no pending
          resolves for futures we imported.
        - :attr:`_active_resolve_obligation_exports`: we owe the peer no pending
          resolves for futures we exported.

        The two grant sets cover holding peer-owned resources and vice-versa; the
        two obligation sets cover in-flight pull-style obligations in both
        directions so a cancelled wait can't drop its lease and let the connection
        drain before the peer has either delivered its resolve or been explicitly
        un-pulled. Called after every state transition that might empty one of
        them (outbound :class:`DiscardMessage` / :class:`UnpullMessage` emission,
        outbound :class:`ResolveMessage` / :class:`RejectMessage`, inbound
        :class:`BreakMessage` / :class:`DiscardMessage` / :class:`ResolveMessage`
        / :class:`RejectMessage` / :class:`UnpullMessage`, and :meth:`_abort`).
        Setting the event is idempotent.
        """
        if self._drain_event.is_set():
            return
        if (
            not self._active_grant_imports
            and not self._active_grant_exports
            and not self._active_resolve_obligation_imports
            and not self._active_resolve_obligation_exports
        ):
            self._drain_event.set()

    ################################################################################################
    # Resolve-obligation + wait-break helpers
    ################################################################################################

    def _bump_resolve_obligation_refcount(self, future_entry: FutureImportEntry) -> None:
        """Increment ``future_entry.resolve_obligation_refcount`` and ensure membership
        in :attr:`_active_resolve_obligation_imports`.

        Called from two places:

        - :meth:`RemoteIRFuture.wait` on the 0→1 transition that accompanies an outbound
          :class:`PullMessage`.
        - Phase-1 substitution when an inbound ``AwaitFutureExpr`` references a future
          we've imported (the expr creates one implicit obligation per receipt, per the
          :class:`UnpullMessage` protocol rule).

        Idempotent for set membership; the count always bumps by exactly one.
        """
        future_entry.resolve_obligation_refcount += 1
        self._active_resolve_obligation_imports.add(future_entry.import_id)

    def _increment_resolve_obligation_count(
        self, future_entry: FutureExportEntry, *, count: int = 1
    ) -> None:
        """Add ``count`` to ``future_entry.resolve_obligation_count`` and, on a
        0 → positive crossing, spawn the resolver task that will deliver the
        :class:`ResolveMessage` / :class:`RejectMessage`.

        Together with :meth:`_decrement_resolve_obligation_count`, this is the
        *only* mutator of ``resolve_obligation_count`` outside the
        :meth:`_zero_future_export_obligation` send-path. Both call sites that bump
        the count — :func:`_on_pull` (one bump per inbound :class:`PullMessage`)
        and the devaluator's ``_bump_await_future_obligation`` (one bump per
        outbound ``AwaitFutureExpr`` referencing this export) — route through here,
        which mechanically enforces the load-bearing invariant from
        :class:`FutureExportEntry`:

            ``resolve_obligation_count > 0 ∧ ¬resolved_sent ⟺
            resolve_task_cancel_scope is not None``

        Late references on an already-resolved future (``resolved_sent is True``)
        must short-circuit at the *caller* — see :func:`_on_pull`'s ``resolved_sent``
        guard — because we still want this primitive to be safe for the rollback
        path (which never increments through a resolved entry). Concretely, after
        ``resolved_sent`` is set this method must never be called.
        """
        assert (
            not future_entry.resolved_sent
        ), "increment after resolved_sent: caller must short-circuit"
        assert count >= 1
        was = future_entry.resolve_obligation_count
        future_entry.resolve_obligation_count = was + count
        self._active_resolve_obligation_exports.add(future_entry.export_id)
        if was == 0:
            self._ensure_resolver_running_for(future_entry)

    def _decrement_resolve_obligation_count(
        self, future_entry: FutureExportEntry, *, count: int = 1
    ) -> None:
        """Subtract ``count`` from ``future_entry.resolve_obligation_count`` and, on a
        positive → 0 crossing, cancel the resolver task so it stops before
        emitting anything.

        Symmetric inverse of :meth:`_increment_resolve_obligation_count`. Used by:

        - :func:`_on_unpull` to apply an inbound :class:`UnpullMessage`'s
          ``refcount`` (the over-decrement clamp lives here — see below).
        - The devaluator's rollback path to undo a prior bump (always with
          ``count=1``, no clamp needed).

        Overshoot tolerance (mirrors :func:`_on_unpull`'s historical tolerance
        rules): if ``count`` exceeds the outstanding obligation, this is a
        protocol violation by the peer — unpulls should never desync until after
        the resolve is delivered. Escalate via
        :func:`phase1_protocol_violation`. (The devaluator rollback path never
        triggers this clamp because every undo is paired with a prior increment.)

        Cancelling a resolver leaves ``resolved_sent`` False; if a later inbound
        :class:`PullMessage` / outbound ``AwaitFutureExpr`` re-introduces the
        obligation, :meth:`_increment_resolve_obligation_count` will spawn a
        fresh resolver. The cancelled resolver's underlying work (e.g. the
        local future's computation) keeps running per the protocol's
        "wait is a read, not a control op" contract — we just suppress the
        delivery of its result.
        """
        assert count >= 1
        new_count = future_entry.resolve_obligation_count - count
        if new_count < 0:
            phase1_protocol_violation(
                self,
                f"Resolve-obligation decrement overshot for future export"
                f" {future_entry.export_id}: count was"
                f" {future_entry.resolve_obligation_count}, decrement"
                f" requested {count}",
            )
        future_entry.resolve_obligation_count = new_count
        if new_count > 0:
            return
        self._active_resolve_obligation_exports.discard(future_entry.export_id)
        scope = future_entry.resolve_task_cancel_scope
        if scope is not None:
            future_entry.resolve_task_cancel_scope = None
            scope.cancel()
        self._check_drain()

    def _ensure_resolver_running_for(self, future_entry: FutureExportEntry) -> None:
        """Spawn a resolver task for ``future_entry`` and store its cancel scope.

        Called from :meth:`_increment_resolve_obligation_count` on a 0 → positive
        crossing. Asserts that no resolver is currently running (the scope is
        ``None``) and that the future entry has a paired grant export
        (``grant_export_id is not None``) — both preconditions hold by
        construction at every legitimate spawn site.

        The actual resolver coroutine body lives in
        :func:`fxtr2.rpc.lowering_message_handlers.spawn_resolver_for_future_export`
        (it needs the chain-walk / forwarding logic that's already there for
        :func:`_on_pull`); this method is the thin coupling point between the
        count-mutating primitive and that worker.
        """
        assert (
            future_entry.resolve_task_cancel_scope is None
        ), "resolver already running for this future export"
        assert (
            future_entry.grant_export_id is not None
        ), "grant_export_id must be paired with the future before any spawn"
        spawn_resolver_for_future_export(self, future_entry)

    def _decrement_local_consumer(self, future_entry: FutureImportEntry) -> None:
        """Decrement ``future_entry.local_consumer_count`` for a returning wait or
        phase-2 spawn.

        When the count drops to zero while ``resolve_obligation_refcount`` is still
        non-zero (no :class:`ResolveMessage` / :class:`RejectMessage` has arrived yet),
        emit an :class:`UnpullMessage` carrying the full outstanding count, zero the
        obligation refcount, drop from :attr:`_active_resolve_obligation_imports`, and
        re-check drain. This is the "last consumer releases all pending obligations"
        pattern described in the :class:`UnpullMessage` docstring.

        Also drives the box-discard gate: a count drop to zero while ``token_ended``
        and no in-flight delivery is the trigger for discharging a box that was
        deferred because waiters were still draining (see
        :meth:`_maybe_discard_future_box`).
        """
        future_entry.local_consumer_count -= 1
        if future_entry.local_consumer_count < 0:
            raise RuntimeError(
                f"local_consumer_count for future import id {future_entry.import_id}"
                f" dropped below zero on {self!r}"
            )
        if future_entry.local_consumer_count == 0 and future_entry.resolve_obligation_refcount > 0:
            count = future_entry.resolve_obligation_refcount
            future_entry.resolve_obligation_refcount = 0
            self._active_resolve_obligation_imports.discard(future_entry.import_id)
            self._enqueue_message(
                UnpullMessage(future_import_id=future_entry.import_id, refcount=count)
            )
            self._check_drain()
        # If the discard was deferred while this consumer was draining, fire it
        # now. No-ops when the conditions don't hold (see method docstring).
        self._maybe_discard_future_box(future_entry)

    def _materialize_future_box(self, future_entry: FutureImportEntry) -> PendingResultBox[Any]:
        """Lazy-init ``future_entry.resolution`` and install the token-end subscription.

        Used by :meth:`RemoteIRFuture.wait`'s setup, the inbound resolve / reject
        handlers, and any other site that needs the box materialized. Idempotent: returns
        the existing box if one is already present.

        On first materialization, installs a token-end subscription whose callback
        flips ``future_entry.token_ended`` and re-checks the discard gate. The callback
        holds the connection and entry by *weak* reference — they're already strongly
        referenced from the connection's import table, and weak references avoid a
        ``conn → _imports → TokenImportEntry → end_callbacks → closure → conn`` cycle
        through the closure. If either weak reference has gone stale by the time the
        token ends (connection torn down without deregistering, entry already removed
        via :func:`_maybe_remove_future_import`), the callback no-ops. Mirrors the
        weakref-on-box pattern :class:`LocalPendingResultBox` uses internally — see
        ``plans/extras/extra5_future_box_race_fix.md``.
        """
        if future_entry.resolution is not None:
            return future_entry.resolution
        box = PendingResultBox[Any]()
        future_entry.resolution = box
        token_proxy = self._get_or_create_token_proxy(future_entry.token_import_id)
        weak_conn: weakref.ref[LeasedCapRPCConnection[Any, Any]] = weakref.ref(self)
        weak_entry: weakref.ref[FutureImportEntry] = weakref.ref(future_entry)

        def on_token_end() -> None:
            c = weak_conn()
            e = weak_entry()
            if c is None or e is None:
                return
            e.token_ended = True
            c._maybe_discard_future_box(e)

        future_entry.box_token_unsubscribe = token_proxy.register_end_callback(on_token_end)
        return box

    def _maybe_discard_future_box(self, future_entry: FutureImportEntry) -> None:
        """Discard ``future_entry.resolution`` iff it is safe to do so.

        Discard gate (full rationale in ``plans/extras/extra5_future_box_race_fix.md``):

        - The token must have ended. Otherwise the future is still live; deferring the
          discard preserves the existing protocol behaviour.
        - No delivery may be in flight. ``_on_resolve``'s spawned ``deliver`` task runs
          ``eval_forwardable`` asynchronously; if the peer's Close races ahead of
          ``set_result``, discarding mid-delivery would surface :class:`LeaseBrokenError`
          to the parked waiter — that's the original bug.
        - If the box has already resolved/rejected/forwarded, no consumer may still be
          draining. ``set_result`` wakes waiters via ``_resolved_event``; they read
          ``s.value`` synchronously inside ``_descend_and_copy``. Discarding the box
          between the wake and the read would null out the storage and surface
          :class:`LeaseBrokenError` instead of the value.

        When all three conditions allow it, transitions the box to ``"discarded"``,
        deregisters the token-end subscription, and clears the bookkeeping handle. The
        entry itself stays in ``_imports`` until its other refcounts drain through
        :func:`_maybe_remove_future_import`.
        """
        box = future_entry.resolution
        if box is None:
            return
        if not future_entry.token_ended:
            return
        if future_entry.delivery_in_flight:
            return
        if box.is_resolved() and future_entry.local_consumer_count > 0:
            return
        box.discard()
        if future_entry.box_token_unsubscribe is not None:
            future_entry.box_token_unsubscribe()
            future_entry.box_token_unsubscribe = None

    def _register_wait_break_callback(
        self, grant_import_id: int, callback: Callable[[str], None]
    ) -> Callable[[], None]:
        """Register a break-notification callback on an imported grant.

        Used by :meth:`RemoteIRFuture.wait` (and phase-2 evaluators that await an
        imported future) after the user-visible lease has been discarded: we need a
        signal if the grant breaks mid-wait, but we no longer hold a :class:`Lease`
        to register against. The grant import entry carries a dedicated
        ``wait_break_callbacks`` dict for exactly this purpose (see
        :class:`GrantImportEntry` docstring).

        If the grant entry is missing or already broken, fires ``callback``
        synchronously (with ``"grant entry no longer live"`` or the entry's
        recorded ``broken_reason``, respectively) and returns a no-op deregister —
        matching the :meth:`Lease.register_broken_callback` convention.

        Returns a deregister callable; calling it more than once is a no-op.
        """
        entry = self._imports.get(grant_import_id)
        if not isinstance(entry, GrantImportEntry):
            callback(f"grant import id {grant_import_id} not live")
            return _noop_deregister
        if entry.broken_reason is not None:
            callback(entry.broken_reason)
            return _noop_deregister
        sub_id = id(callback)
        entry.wait_break_callbacks[sub_id] = callback

        def deregister() -> None:
            cur = self._imports.get(grant_import_id)
            if not isinstance(cur, GrantImportEntry):
                return
            cur.wait_break_callbacks.pop(sub_id, None)
            # Clearing the last wait-break can complete drainage if
            # ``_clean_up_lease_data`` already drained ``live_leases`` while
            # this subscription was still in place. Try the removal now.
            self._maybe_remove_grant_import(cur)

        return deregister

    def _zero_future_import_obligation(self, future_entry: FutureImportEntry) -> None:
        """Zero ``resolve_obligation_refcount`` and drop from the active import set.

        Called on inbound :class:`ResolveMessage` / :class:`RejectMessage` delivery
        and on abort teardown. A single resolve discharges every outstanding
        obligation (both the ones we pulled for and the ones we received via
        inbound ``AwaitFutureExpr``), so we zero the whole count atomically — no
        :class:`UnpullMessage` is emitted because the peer is already doing the
        right thing. Safe to call while ``local_consumer_count`` is non-zero:
        in-flight waits/spawns will finish via the resolved box and decrement
        normally.
        """
        future_entry.resolve_obligation_refcount = 0
        self._active_resolve_obligation_imports.discard(future_entry.import_id)
        self._check_drain()

    def _zero_future_export_obligation(self, future_entry: FutureExportEntry) -> None:
        """Zero ``resolve_obligation_count`` and drop from the active export set.

        Called when we emit our :class:`ResolveMessage` / :class:`RejectMessage`.
        The single outbound resolve discharges all outstanding obligations the
        peer placed on us (including those from outbound ``AwaitFutureExpr``
        occurrences we emitted ourselves, which are released on the peer side by
        the same resolve). Late inbound pulls or ``AwaitFutureExpr`` references
        against this future are silently ignored after zero (see
        :attr:`FutureExportEntry.resolved_sent`).
        """
        future_entry.resolve_obligation_count = 0
        future_entry.resolved_sent = True
        self._active_resolve_obligation_exports.discard(future_entry.export_id)
        self._check_drain()

    ################################################################################################
    # Thin wrappers forwarding to submodule functions
    ################################################################################################

    def _devaluate(self, value: IRCallArg) -> tuple[Expression[Any], list[Lease[Any]]]:
        return devaluate(self, value)

    def _phase1_substitute(
        self, expr: Expression[Any]
    ) -> tuple[Expression[Any], list[FutureImportEntry]]:
        return phase1_substitute(self, expr)

    def _eval_static(self, expr: Expression[Any]) -> IRStatic:
        return eval_static(expr)

    async def _eval_as_value(self, expr: Expression[Any]) -> IRValue:
        return await eval_as_value(self, expr)

    async def _eval_as_call_arg(self, expr: Expression[Any]) -> IRCallArg:
        return await eval_as_call_arg(self, expr)

    async def _eval_forwardable(
        self, expr: Expression[Any]
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        return await eval_forwardable(self, expr)

    def _phase1_protocol_violation(self, reason: str) -> NoReturn:
        return phase1_protocol_violation(self, reason)

    ################################################################################################
    # Import / export table registration helpers
    # ------------------------------------------
    #
    # Own the full "create entry + register in _imports/_exports + update reverse-lookup
    # dict + update active set" transition for each kind of table slot. Used by the
    # non-speculative paths (``__init__``, ``_on_push``, ``RemoteIRHandle.dispatch``,
    # ``_phase1_register_*_import``). The devaluator (``lowering_devaluate.py``) does
    # not use these — it performs the same mutations under a speculative-mutation
    # context with an undo log, which doesn't fit the "commit atomically" shape of
    # these helpers.
    #
    # Contract: every entry in ``_imports`` / ``_exports`` has a matching entry in its
    # reverse-lookup dict (if any) and in the relevant active set (if any). These
    # helpers are the single source of truth for that invariant on the non-speculative
    # side; the devaluator's helpers mirror it.
    ################################################################################################

    def _register_token_export(
        self, *, export_id: int, local_token: LifetimeToken[Any], outstanding_count: int = 1
    ) -> TokenExportEntry:
        """Create and register a :class:`TokenExportEntry`."""
        entry = TokenExportEntry(
            export_id=export_id,
            local_token=local_token,
            outstanding_count=outstanding_count,
            watch_deregister=None,
        )
        self._exports[export_id] = entry
        self._token_export_id_by_token[local_token] = export_id
        return entry

    def _register_handle_export(
        self,
        *,
        export_id: int,
        token_export_id: int,
        local_handle: IRHandle[Any],
        outstanding_count: int = 1,
    ) -> HandleExportEntry:
        """Create and register a :class:`HandleExportEntry`."""
        entry = HandleExportEntry(
            export_id=export_id,
            token_export_id=token_export_id,
            local_handle=local_handle,
            outstanding_count=outstanding_count,
        )
        self._exports[export_id] = entry
        self._handle_export_id_by_handle_id[id(local_handle)] = export_id
        return entry

    def _register_future_export(
        self,
        *,
        export_id: int,
        token_export_id: int,
        local_future: IRFuture[Any, Any],
        outstanding_count: int = 1,
        resolve_obligation_count: int = 0,
        grant_export_id: int | None = None,
    ) -> FutureExportEntry:
        """Create and register a :class:`FutureExportEntry`.

        ``grant_export_id`` may be ``None`` at registration time if no grant export has
        been paired with this future yet (the devaluator's ``_devaluate_future`` path
        registers the future before its accompanying lease has been devaluated); it must
        be populated before any code path can cause a 0 → positive crossing of
        ``resolve_obligation_count``. See :attr:`FutureExportEntry.grant_export_id`.
        """
        entry = FutureExportEntry(
            export_id=export_id,
            token_export_id=token_export_id,
            local_future=local_future,
            outstanding_count=outstanding_count,
            resolve_obligation_count=resolve_obligation_count,
            resolve_task_cancel_scope=None,
            grant_export_id=grant_export_id,
        )
        self._exports[export_id] = entry
        self._future_export_id_by_future_id[id(local_future)] = export_id
        return entry

    def _register_grant_export(
        self,
        *,
        export_id: int,
        token_export_id: int,
        backing_lease: Lease[Any],
        outstanding_count: int = 1,
    ) -> GrantExportEntry:
        """Create and register a :class:`GrantExportEntry`.

        Adds to ``_active_grant_exports`` (newly-created entries always have
        ``outstanding_count >= 1``) and subscribes the backing lease's broken
        callback so local revocation propagates as a :class:`BreakMessage`.
        """
        entry = GrantExportEntry(
            export_id=export_id,
            token_export_id=token_export_id,
            backing_lease=backing_lease,
            outstanding_count=outstanding_count,
        )
        self._exports[export_id] = entry
        self._grant_export_id_by_grant[backing_lease.grant] = export_id
        self._active_grant_exports.add(export_id)
        self._subscribe_backing_lease_break(entry)
        return entry

    # -------- Introduce helpers (imports) --------
    #
    # One helper per import kind, covering both first-introduction (create
    # fresh entry at ``introduction_refcount=1``) and re-introduction (bump
    # existing entry's refcount by 1). For the dep kinds, each call also
    # increments the subject token's ``introduction_refcount`` by 1 — the
    # protocol rule is "every ``Exported…Expr`` that carries a
    # ``token_export_id`` is one introduction of both the dep and its
    # subject token," and these helpers are the single place that rule is
    # encoded on the import side.
    #
    # Raises :class:`ImportEntryKindMismatch` if an existing entry at the
    # given id has the wrong kind, or if a dep's existing entry names a
    # different ``token_import_id`` than the one passed in. Phase-1
    # translates the exception into :func:`phase1_protocol_violation`; other
    # callers (main-init, push-allocators) pass args that can't trigger
    # it, so it propagates as a bug.

    def _introduce_token_import(self, token_import_id: int) -> TokenImportEntry:
        """Register (or bump) a token import entry. Returns the entry.

        New token imports go into ``_active_token_imports`` (they start
        un-ended).
        """
        existing = self._imports.get(token_import_id)
        if existing is not None:
            if not isinstance(existing, TokenImportEntry):
                raise ImportEntryKindMismatch(
                    f"token import id {token_import_id} is already registered as a"
                    f" {type(existing).__name__}"
                )
            existing.introduction_refcount += 1
            return existing
        entry = TokenImportEntry(
            import_id=token_import_id,
            introduction_refcount=1,
            ended=False,
            end_callbacks=[],
            watch_sent=False,
        )
        self._imports[token_import_id] = entry
        self._active_token_imports.add(token_import_id)
        return entry

    def _introduce_handle_import(
        self, handle_import_id: int, token_import_id: int
    ) -> HandleImportEntry:
        """Register (or bump) a handle import entry plus its subject
        token's share of the introduction. Returns the entry.

        Validates subject-token consistency on re-introduction — peer is
        not allowed to silently change a handle's subject token.
        """
        existing = self._imports.get(handle_import_id)
        if existing is not None:
            if not isinstance(existing, HandleImportEntry):
                raise ImportEntryKindMismatch(
                    f"handle import id {handle_import_id} is already registered as a"
                    f" {type(existing).__name__}"
                )
            if existing.token_import_id != token_import_id:
                raise ImportEntryKindMismatch(
                    f"handle import id {handle_import_id}: token_import_id mismatch"
                    f" (existing {existing.token_import_id}, new {token_import_id})"
                )
            self._introduce_token_import(token_import_id)
            existing.introduction_refcount += 1
            return existing
        self._introduce_token_import(token_import_id)
        entry = HandleImportEntry(
            import_id=handle_import_id,
            token_import_id=token_import_id,
            introduction_refcount=1,
        )
        self._imports[handle_import_id] = entry
        return entry

    def _introduce_future_import(
        self, future_import_id: int, token_import_id: int
    ) -> FutureImportEntry:
        """Register (or bump) a future import entry plus its subject
        token's share of the introduction. Returns the entry.

        Validates subject-token consistency on re-introduction.
        """
        existing = self._imports.get(future_import_id)
        if existing is not None:
            if not isinstance(existing, FutureImportEntry):
                raise ImportEntryKindMismatch(
                    f"future import id {future_import_id} is already registered as a"
                    f" {type(existing).__name__}"
                )
            if existing.token_import_id != token_import_id:
                raise ImportEntryKindMismatch(
                    f"future import id {future_import_id}: token_import_id mismatch"
                    f" (existing {existing.token_import_id}, new {token_import_id})"
                )
            self._introduce_token_import(token_import_id)
            existing.introduction_refcount += 1
            return existing
        self._introduce_token_import(token_import_id)
        entry = FutureImportEntry(
            import_id=future_import_id,
            token_import_id=token_import_id,
            introduction_refcount=1,
            resolve_obligation_refcount=0,
            local_consumer_count=0,
            resolution=None,
        )
        self._imports[future_import_id] = entry
        return entry

    def _introduce_grant_import(
        self, grant_import_id: int, token_import_id: int
    ) -> GrantImportEntry:
        """Register (or bump) a grant import entry plus its subject
        token's share of the introduction. Returns the entry.

        Validates subject-token consistency on re-introduction.

        ``_active_grant_imports`` membership is NOT touched here — it's
        driven by ``_register_lease`` (the first lease on this grant
        transitions the set).
        """
        existing = self._imports.get(grant_import_id)
        if existing is not None:
            if not isinstance(existing, GrantImportEntry):
                raise ImportEntryKindMismatch(
                    f"grant import id {grant_import_id} is already registered as a"
                    f" {type(existing).__name__}"
                )
            if existing.token_import_id != token_import_id:
                raise ImportEntryKindMismatch(
                    f"grant import id {grant_import_id}: token_import_id mismatch"
                    f" (existing {existing.token_import_id}, new {token_import_id})"
                )
            self._introduce_token_import(token_import_id)
            existing.introduction_refcount += 1
            return existing
        self._introduce_token_import(token_import_id)
        entry = GrantImportEntry(
            import_id=grant_import_id,
            token_import_id=token_import_id,
            introduction_refcount=1,
            live_leases={},
            wait_break_callbacks={},
            broken_reason=None,
        )
        self._imports[grant_import_id] = entry
        return entry

    ################################################################################################
    # Proxy singleton caches
    # ----------------------
    #
    # Every ``Remote…`` proxy produced anywhere in the connection goes through one
    # of these helpers. The cache is a :class:`weakref.WeakValueDictionary`, so
    # cached proxies don't pin themselves — the moment external references drop,
    # the weakref evicts and the proxy's finalizer fires.
    #
    # The singleton invariant collapses the churn from repeated ``.token`` /
    # ``.subject`` property accesses (and phase-1 re-emissions for the same id
    # across consecutive messages) into a single proxy object per id, so
    # ``live_proxy_count`` on the entry tracks "is anyone still referencing this
    # import" rather than counting transient reconstructions.
    #
    # ``RemoteIRFuture`` with a non-empty ``attr_path`` is *not* cached — each walk
    # path is a distinct local view of the same future import, and the user may
    # hold several walked views independently. Those proxies bump/decrement
    # ``live_proxy_count`` directly via ``__post_init__`` without going through a
    # cache lookup.
    ################################################################################################

    def _get_or_create_token_proxy(self, token_import_id: int) -> RemoteLifetimeToken[Any]:
        """Return the singleton :class:`RemoteLifetimeToken` for this connection
        and the given import id, constructing and caching it on first access.

        The cache is a :class:`weakref.WeakValueDictionary` — subsequent calls
        resolve to the same object only as long as at least one external
        reference keeps the proxy alive. Once the last reference drops, the
        cache evicts and the proxy's ``__post_init__``-installed finalizer
        fires; a subsequent lookup then constructs a fresh proxy.
        """
        existing = self._token_proxy_cache.get(token_import_id)
        if existing is not None:
            return existing
        new_proxy = RemoteLifetimeToken[Any](conn=self, token_import_id=token_import_id)
        self._token_proxy_cache[token_import_id] = new_proxy
        return new_proxy

    def _get_or_create_handle_proxy(self, handle_import_id: int) -> RemoteIRHandle[Any]:
        """Return the singleton :class:`RemoteIRHandle` for this connection and
        the given import id, constructing and caching it on first access.

        Looks up the handle entry's ``token_import_id`` and eagerly resolves
        its subject token proxy (via :meth:`_get_or_create_token_proxy`) to
        feed the ``_token`` field — that strong reference is what pins the
        token proxy for the duration of the handle proxy's life, per the
        pin-chain invariant.

        See :meth:`_get_or_create_token_proxy` for cache-eviction semantics.
        """
        existing = self._handle_proxy_cache.get(handle_import_id)
        if existing is not None:
            return existing
        entry = self._imports.get(handle_import_id)
        assert isinstance(
            entry, HandleImportEntry
        ), f"_get_or_create_handle_proxy({handle_import_id}): no live HandleImportEntry"
        token_proxy = self._get_or_create_token_proxy(entry.token_import_id)
        new_proxy = RemoteIRHandle[Any](
            conn=self, handle_import_id=handle_import_id, _token=token_proxy
        )
        self._handle_proxy_cache[handle_import_id] = new_proxy
        return new_proxy

    def _get_or_create_future_proxy(self, future_import_id: int) -> RemoteIRFuture[Any, Any]:
        """Return the singleton base :class:`RemoteIRFuture` (``attr_path=()``)
        for this connection and the given import id, constructing and caching
        it on first access.

        Feeds ``_token`` from :meth:`_get_or_create_token_proxy` — same pin-
        chain rationale as :meth:`_get_or_create_handle_proxy`.

        Walked proxies (non-empty ``attr_path``) are deliberately *not* cached:
        each walk path is a distinct local view that the user may hold
        independently. :meth:`RemoteIRFuture.walk` forwards its parent's
        ``_token`` directly. See :meth:`_get_or_create_token_proxy` for
        cache-eviction semantics.
        """
        existing = self._future_proxy_cache.get(future_import_id)
        if existing is not None:
            return existing
        entry = self._imports.get(future_import_id)
        assert isinstance(
            entry, FutureImportEntry
        ), f"_get_or_create_future_proxy({future_import_id}): no live FutureImportEntry"
        token_proxy = self._get_or_create_token_proxy(entry.token_import_id)
        new_proxy = RemoteIRFuture[Any, Any](
            conn=self, future_import_id=future_import_id, _token=token_proxy
        )
        self._future_proxy_cache[future_import_id] = new_proxy
        return new_proxy

    ################################################################################################
    # Import / export table teardown helpers
    # --------------------------------------
    #
    # Symmetric counterparts to the ``_register_*`` helpers. Used by the non-
    # speculative teardown paths (``_on_discard`` / ``on_break`` / ``_abort`` /
    # ``_on_break`` / ``_on_closed``) to keep the table + reverse-lookup + active
    # set invariants in sync during teardown, the same way the register helpers
    # own them during setup.
    #
    # Note: imports are NEVER removed from ``_imports`` (entries persist for the
    # connection's lifetime). The ``_mark_*_import_*`` helpers only do *state
    # transitions* within the entry (broken_reason, ended, clearing collections
    # and sets).
    ################################################################################################

    def _tear_down_grant_export(self, entry: GrantExportEntry) -> None:
        """Fully tear down a :class:`GrantExportEntry`: deregister the broken
        callback (if still attached), discard the backing lease, and remove the
        entry from ``_exports`` + ``_grant_export_id_by_grant`` + the
        ``_active_grant_exports`` set. Re-checks drain.

        Callers that are themselves firing inside the broken-callback (the
        ``on_break`` closure in :meth:`_subscribe_backing_lease_break`) must
        null ``entry.broken_callback_deregister`` before calling us — the
        callback can't deregister itself mid-fire. The ``is not None`` guard
        below then no-ops the deregister branch.
        """
        if entry.broken_callback_deregister is not None:
            entry.broken_callback_deregister()
            entry.broken_callback_deregister = None
        # ``entry.backing_lease.discard()`` is safe whether the lease is still
        # active (owner-deregister path) or already broken (transitions to
        # DISCARDED, no deregister). In the on_break flow the lease is already
        # broken; in the _on_discard flow it's still active — in neither case
        # does the order relative to the table deletions matter.
        entry.backing_lease.discard()
        del self._exports[entry.export_id]
        del self._grant_export_id_by_grant[entry.backing_lease.grant]
        self._active_grant_exports.discard(entry.export_id)
        self._check_drain()

    def _tear_down_token_export(self, entry: TokenExportEntry) -> None:
        """Fully tear down a :class:`TokenExportEntry`: deregister the watch
        (if still attached — the peer may have discarded the token without
        waiting for our ClosedMessage), remove the entry from ``_exports`` +
        ``_token_export_id_by_token`` + the ``_watched_token_exports`` set.
        """
        if entry.watch_deregister is not None:
            entry.watch_deregister()
            entry.watch_deregister = None
        del self._exports[entry.export_id]
        del self._token_export_id_by_token[entry.local_token]
        self._watched_token_exports.discard(entry.export_id)

    def _tear_down_handle_export(self, entry: HandleExportEntry) -> None:
        """Fully tear down a :class:`HandleExportEntry`."""
        del self._exports[entry.export_id]
        del self._handle_export_id_by_handle_id[id(entry.local_handle)]

    def _tear_down_future_export(self, entry: FutureExportEntry) -> None:
        """Fully tear down a :class:`FutureExportEntry`."""
        del self._exports[entry.export_id]
        del self._future_export_id_by_future_id[id(entry.local_future)]

    def _mark_grant_import_broken(self, entry: GrantImportEntry, reason: str) -> None:
        """Mark a grant import broken, clear ``live_leases`` + ``wait_break_callbacks``,
        fire both the broken-callbacks on every live lease and the break callbacks
        registered by in-flight waits, and remove from ``_active_grant_imports``.
        Re-checks drain.

        No outbound :class:`DiscardMessage` is emitted: peer sent the
        :class:`BreakMessage`, so their ``grant_export`` is already gone.
        The grant's share of the subject token's introductions stays
        accumulated in ``token_entry.introduction_refcount`` and will be
        retired, in one go, when the cached token proxy GCs — see the
        "Proxy-GC tracking" comment in ``lowering_remote_proxies.py``.

        Precondition: ``entry.broken_reason is None`` — the helper is not
        idempotent, because re-firing broken-callbacks on already-broken leases /
        already-fired wait-break subscriptions would be wrong. Callers
        (``_on_break`` and ``_abort``) check first.
        """
        entry.broken_reason = reason
        self._active_grant_imports.discard(entry.import_id)
        tracked_items = list(entry.live_leases.items())
        entry.live_leases.clear()
        for _lease_id, (_info, finalizer) in tracked_items:
            peek = finalizer.peek()
            finalizer.detach()
            if peek is None:
                continue
            lease = peek[0]
            if isinstance(lease, LocalLifetimeLease):
                lease._mark_broken(reason)
        # Fire any wait-break callbacks registered by in-flight no-lease waits
        # (see :attr:`GrantImportEntry.wait_break_callbacks`). Snapshot then
        # clear before firing so a callback that re-enters (e.g. via a chained
        # discard path) sees a clean dict.
        wait_callbacks = list(entry.wait_break_callbacks.values())
        entry.wait_break_callbacks.clear()
        for cb in wait_callbacks:
            try:
                cb(reason)
            except Exception:
                logger.exception(
                    "Error in wait-break callback for grant import id %s on %r",
                    entry.import_id,
                    self,
                )
        # Remove the entry now that everything it tracked has been fired
        # or cleared. The peer has already torn down their grant_export,
        # so any leftover ``introduction_refcount`` is orphan bookkeeping.
        self._maybe_remove_grant_import(entry)
        self._check_drain()

    def _mark_token_import_ended(self, entry: TokenImportEntry) -> None:
        """Mark a token import ended, fire ``end_callbacks``, and remove from
        ``_active_token_imports``.

        Precondition: ``not entry.ended`` — the helper is not idempotent.
        Callers (``_on_closed`` and ``_abort``) check first.
        """
        entry.ended = True
        self._active_token_imports.discard(entry.import_id)
        callbacks = entry.end_callbacks
        entry.end_callbacks = []
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception(
                    "Error in end-callback for token import id %s on %r",
                    entry.import_id,
                    self,
                )

    ################################################################################################
    # Grant-export break subscription
    ################################################################################################

    def _subscribe_backing_lease_break(self, entry: GrantExportEntry) -> None:
        """Subscribe to the grant's backing-lease broken-callback so local revocation
        of the lease's subject lifetime propagates to the peer as a
        :class:`BreakMessage` AND the export entry is torn down locally.

        Stores the deregister handle on the entry so we can detach it cleanly when
        the entry is torn down by the peer (inbound :class:`DiscardMessage` draining
        ``outstanding_count`` to zero, or on abort). If ``backing_lease`` is already
        broken at subscribe-time, :meth:`Lease.register_broken_callback` fires
        synchronously and returns a no-op deregister.

        Why tear down locally on break: once we've sent :class:`BreakMessage` the
        peer is free — but not obligated — to respond with a :class:`DiscardMessage`.
        If we kept the export entry around waiting for that response, drain could
        block indefinitely on a broken grant. Instead we remove the entry
        immediately; a racing :class:`DiscardMessage` from the peer is a harmless
        no-op in :func:`_on_discard`.

        Called from (1) the main-grant registration in :meth:`__init__`, (2)
        ``_devaluate_lease`` when it registers a fresh grant export, and (3)
        ``_on_push`` when it mirrors a peer's push-allocated grant.
        """
        export_id = entry.export_id

        def on_break(reason: str) -> None:
            # Invariant: every teardown path for a ``GrantExportEntry`` (``_on_discard``,
            # ``_abort``, the devaluator's rollback, and ``on_break`` itself) detaches
            # this callback *before* the backing lease is discarded. So if ``on_break``
            # runs at all, ``entry`` is still in ``_exports`` at ``export_id``. Assert
            # the invariant loudly — masking a violation here would silently drop a
            # ``BreakMessage`` and leave peer-side leases live indefinitely.
            current = self._exports.get(export_id)
            assert current is entry, (
                "_subscribe_backing_lease_break: on_break fired for export id"
                f" {export_id} but _exports[{export_id}] is {current!r}, not the"
                f" expected {entry!r}. Teardown ordering invariant violated — some"
                " path discarded backing_lease without detaching broken_callback_deregister."
            )
            # Self-deregister: the callback can't fire again (it's already
            # firing), so null the handle before calling ``_tear_down_grant_export``
            # — its ``if broken_callback_deregister is not None`` guard then
            # no-ops the deregister branch, and any later teardown path sees
            # "no subscription."
            entry.broken_callback_deregister = None
            # Tombstone the id before teardown so phase 1 can tell this
            # "no longer have" apart from "never had" when a peer request
            # crosses our BreakMessage on the wire — see the field docstring
            # on ``_broken_grant_export_tombstones``.
            self._broken_grant_export_tombstones[export_id] = (
                entry.backing_lease.grant,
                reason,
            )
            self._enqueue_message(BreakMessage(grant_export_id=export_id, reason=reason))
            self._tear_down_grant_export(entry)

        entry.broken_callback_deregister = entry.backing_lease.register_broken_callback(on_break)
