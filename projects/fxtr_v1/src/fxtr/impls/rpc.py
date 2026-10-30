"""Entity RPC implementation for hub protocols.

Loosely inspired by JSON-RPC and by the Git pack-file negotiation protocol, but specialized to
sending DAG-CBOR objects in the fxtr object model (rather than JSON objects or Git objects). Used
as the low-level wrapper protocol by which hub-level protocol implementations actually communicate.

A server implementation exposing fxtr RPC capabilities must expose a WebSocket endpoint at:
- `WS /.well_known/org.transluce.fxtr.rpc/ws`: WebSocket endpoint for bidirectional RPC communication

All RPC communication between the server and client should be encoded as DAG-CBOR messages sent over
the WebSocket connection.

Note that we allow fxtr RPC calls to co-exist with other endpoints on the same server. In
particular, it is possible for a fxtr protocol implementation to expose _both_ a fxtr RPC endpoint
at `/.well_known/org.transluce.fxtr.rpc` and also custom endpoints at other URLs. It is also NOT
required for every fxtr protocol implementation to expose a fxtr RPC endpoint. However, accessing
or serving other endpoints requires manually configuring a connection, and will not be automatically
translated to Python calls by the fxtr protocol wrappers.

## WebSocket Protocol

The WebSocket connection carries CBOR-encoded messages. Both client and server can send messages
at any time. There are three types of messages:

### Info Message

Sent by both sides immediately after connection establishment. Contains information about RPC
capabilities and known entities.

CBOR-encoded object with fields:
- `type`: "info"
- `fxtr_versions`: List of supported protocol versions (currently ["0.1"])
- `known`: List of CIDs of entities that this side considers "well known". The other side does not
  need to send these entities or their children.

Note: To discover what protocol an implementation supports, call the `interface_spec()` method via RPC.

### Call Message

Sent to request a method call on a target object. The sender waits for a corresponding response
message with the same call_id.

CBOR-encoded object with fields:
- `type`: "call"
- `call_id`: Integer identifier for this call, unique per sender
- `target_handle`: Integer handle identifying the target object (0 = root implementation)
- `method`: Name of the method to call
- `args`: (optional) List of positional arguments. Defaults to empty list if missing.
- `kwargs`: (optional) Dict of keyword arguments. Defaults to empty dict if missing.
- `entities`: Map from base32-encoded CID string to CBOR representation of entities needed by
  args/kwargs. Should exclude entities in the receiver's known list.

Arguments in `args` and `kwargs` are arbitrary DAG-CBOR values where CIDs are interpreted as
entity references.

### Response Message

Sent in reply to a call message, with matching call_id.

CBOR-encoded object with fields:
- `type`: "response"
- `call_id`: Integer identifier matching the original call
- `success`: Boolean indicating whether the call succeeded
- `result`: The result value (if success=True) or error info (if success=False), as a DAG-CBOR value
- `entities`: Map from base32-encoded CID string to CBOR representation of entities needed by
  the result. Should exclude entities in the receiver's known list.

## Implementation Notes

Which entities are provided in the "known" lists is not specified as part of the contract. A
reasonable option is to include entities referencing schemas that the client or server expects to
receive as a request or response, or to include entities involved in defining the protocol.

In the future, if the set of entities is very large, it may be useful to send them in a more
compressed form, e.g. as a Bloom filter, with a failure probability that is small. Then if it turns
out that there is a false-positive in the Bloom filter, the sender could either replay the request
with more entities, or request the missing entities from the other side directly.

Method call semantics are defined by the protocol implemented by the target object. Protocols may
specify required signatures, argument passing conventions, etc.
"""

from __future__ import annotations

import functools
import logging
import typing
import warnings
import weakref
from typing import Any, Mapping, Sequence

import anyio
import anyio.abc
import cbrrr
import logfire
from frozendict import frozendict

from ..object_model import cbor_util, core, dag_util, locator

logger = logging.getLogger(__name__)

FXTR_VERSION = "0.1"


def _sendable_value_to_cbor(
    value: core.SendableValue,
    *,
    entity_collector: dag_util.RecursiveSubgraphCollector | None = None,
    connection: FxtrRPCConnection | None = None,
) -> cbrrr.DagCborTypes:
    """Convert a SendableValue to an escaped CBOR representation.

    Args:
        value: The SendableValue to convert
        entity_collector: Optional collector to gather entities encountered during conversion
        connection: Optional RPC connection for registering FxtrCallTarget handles

    Returns:
        The CBOR representation. Dicts, lists, frozendicts, tuples, and RPC handles all serialize as
        lists of the form [type_specifier, inner_value] where the type specifier identifies the
        type of the inner value.
    """
    if value is None or isinstance(value, (str, bytes, int, bool, float)):
        return value
    elif isinstance(value, frozendict):
        return [
            "f",
            {
                k: _sendable_value_to_cbor(
                    v, entity_collector=entity_collector, connection=connection
                )
                for k, v in value.items()
            },
        ]
    elif isinstance(value, dict | Mapping):
        # dicts and all non-frozen mappings serialize as dicts.
        # this works because we don't allow any other specific type of mapping in
        # core.assert_sendable_value_type other than frozendict and dict.
        return [
            "d",
            {
                k: _sendable_value_to_cbor(
                    v, entity_collector=entity_collector, connection=connection
                )
                for k, v in value.items()
            },
        ]
    elif isinstance(value, tuple):
        return [
            "t",
            [
                _sendable_value_to_cbor(
                    item, entity_collector=entity_collector, connection=connection
                )
                for item in value
            ],
        ]
    elif isinstance(value, list | Sequence):
        # lists and all non-frozen sequences serialize as lists.
        # this works because we don't allow any other specific type of sequence in
        # core.assert_sendable_value_type other than tuple and list.
        return [
            "l",
            [
                _sendable_value_to_cbor(
                    item, entity_collector=entity_collector, connection=connection
                )
                for item in value
            ],
        ]
    elif isinstance(value, core.EntityBase):
        if entity_collector is not None:
            entity_collector.visit_entity(entity=value)
        return core.entity_cid(value)
    elif isinstance(value, cbrrr.CID):
        return ["c", value]
    elif isinstance(value, core.FxtrCallTarget):
        if connection is None:
            raise TypeError("Cannot serialize FxtrCallTarget without a connection")
        # Register the impl and get a handle
        handle_id = connection.register(value)
        interface_spec = value.interface_spec()
        if entity_collector is not None:
            entity_collector.visit_entity(entity=interface_spec)
        return ["h", [handle_id, core.entity_cid(interface_spec)]]
    else:
        raise TypeError(f"Unexpected type in SendableValue: {type(value)}")


def _cbor_to_sendable_value(
    cbor_value: cbrrr.DagCborTypes,
    *,
    lookup: core.EntityLookup,
    connection: FxtrRPCConnection | None = None,
    decref_counter: dict[int, int] | None = None,
) -> core.SendableValue:
    """Convert CBOR representation back to SendableValue.

    Args:
        cbor_value: The CBOR value to convert. Should be escaped in the same format as was produced
            by _sendable_value_to_cbor.
        lookup: Function to lookup entities by CID
        connection: Optional RPC connection for creating FxtrCallTarget proxies
        decref_counter: Optional dict to accumulate decrefs for handles we've seen

    Returns:
        The reconstructed SendableValue
    """
    if cbor_value is None or isinstance(cbor_value, (str, bytes, int, bool, float)):
        return cbor_value
    elif isinstance(cbor_value, list):
        assert len(cbor_value) == 2, "Expected RPC serialization to have list of length 2"
        tag, inner = cbor_value
        if tag == "d":
            assert isinstance(inner, dict)
            return {
                k: _cbor_to_sendable_value(
                    v, lookup=lookup, connection=connection, decref_counter=decref_counter
                )
                for k, v in inner.items()
            }
        elif tag == "f":
            assert isinstance(inner, dict)
            return frozendict(
                {
                    k: _cbor_to_sendable_value(
                        v, lookup=lookup, connection=connection, decref_counter=decref_counter
                    )
                    for k, v in inner.items()
                }
            )
        elif tag == "t":
            assert isinstance(inner, list)
            return tuple(
                _cbor_to_sendable_value(
                    item, lookup=lookup, connection=connection, decref_counter=decref_counter
                )
                for item in inner
            )
        elif tag == "l":
            assert isinstance(inner, list)
            return [
                _cbor_to_sendable_value(
                    item, lookup=lookup, connection=connection, decref_counter=decref_counter
                )
                for item in inner
            ]
        elif tag == "c":
            assert isinstance(inner, cbrrr.CID)
            return inner
        elif tag == "h":
            if connection is None:
                raise TypeError("Cannot deserialize rpc handle without a connection")
            if decref_counter is None:
                raise TypeError("Cannot deserialize rpc handle without a decref counter")

            assert isinstance(inner, list) and len(inner) == 2
            handle_id, interface_spec_cid = inner
            assert isinstance(handle_id, int)
            assert isinstance(interface_spec_cid, cbrrr.CID)
            interface_spec = lookup(interface_spec_cid)
            assert isinstance(interface_spec, core.InterfaceSpec)

            # Check if we already have a proxy for this handle
            if handle_id in connection._their_handle_proxies:
                # Increment decref counter since we're reusing an existing proxy
                decref_counter[handle_id] = decref_counter.get(handle_id, 0) + 1
                return connection._their_handle_proxies[handle_id]

            # Create a new proxy with the interface spec from the handle
            proxy = RemoteSubtargetImpl(
                connection=connection, handle_id=handle_id, interface_spec=interface_spec
            )

            # Store in weak-valued dictionary
            connection._their_handle_proxies[handle_id] = proxy

            # Set up finalization to decref when proxy is garbage collected
            def on_proxy_finalize(handle_id: int, connection_ref: weakref.ref) -> None:
                conn = connection_ref()
                if conn is not None and not conn._disconnected.is_set():
                    assert isinstance(conn, FxtrRPCConnection)
                    # Schedule async decref in the background task group
                    conn._background_task_group.start_soon(conn._decref_theirs, {handle_id: 1})

            weakref.finalize(proxy, on_proxy_finalize, handle_id, weakref.ref(connection))

            return proxy

        else:
            raise TypeError(f"Unexpected tag: {tag}")
    elif isinstance(cbor_value, cbrrr.CID):
        entity = lookup(cbor_value)
        return entity
    else:
        raise TypeError(f"Unexpected CBOR type in RPC: {type(cbor_value)}")


class RPCCallErrorInfo(
    core.DataEntityBase, type_id="RPCCallErrorInfo^1", namespace_id="!builtin.rpc"
):
    """Information about an error that occurred during an RPC call."""

    error: str
    type: str


class FxtrRPCConnection:
    """Manages a bidirectional WebSocket RPC connection.

    This class can act as either the client or server side of a WebSocket connection,
    handling message routing, entity serialization, and call/response correlation.
    """

    def __init__(
        self,
        websocket: Any,  # WebSocket connection (starlette WebSocket or similar)
        known_loader: locator.CachingEntityLoader,
        background_task_group: anyio.abc.TaskGroup,
        root_target: "core.FxtrCallTarget | None" = None,
    ):
        """Initialize the RPC connection.

        Args:
            websocket: The WebSocket connection object
            known_loader: Loader for entities this side considers "well known"
            background_task_group: Task group for background tasks like deferred decrefs
            root_target: Optional root implementation to serve (registered as handle 0)
        """
        self._websocket = websocket
        self._known_loader = known_loader
        self._background_task_group = background_task_group

        # Target management (handles we're serving)
        self._targets: dict[int, core.FxtrCallTarget] = {}
        self._target_to_handle: dict[int, int] = {}  # Maps id(target) -> handle
        self._target_refcounts: dict[int, int] = {}  # Refcount for each handle
        self._next_handle = 1

        if root_target is not None:
            self._targets[0] = root_target
            self._target_to_handle[id(root_target)] = 0
            self._target_refcounts[0] = 1  # Root starts with refcount 1

        # Call management (calls we've initiated)
        self._next_call_id = 0
        self._pending_calls: dict[int, anyio.Event] = {}
        self._call_results: dict[int, tuple[bool, core.SendableValue]] = {}

        # Proxy management (handles from counterparty that we're calling)
        self._their_handle_proxies: weakref.WeakValueDictionary[int, core.FxtrCallTarget] = (
            weakref.WeakValueDictionary()
        )

        # Info from counterparty
        self._counterparty_info_received = anyio.Event()
        self._counterparty_known_cids: set[cbrrr.CID] = set()
        self._counterparty_root_protocol: core.InterfaceSpec | None = None

        # Background task management
        self._disconnected = anyio.Event()
        self._disconnect_error: Exception | None = None

    def register(self, target: core.FxtrCallTarget) -> int:
        """Register a target object and get back a handle.

        If this object has been seen before, returns the existing handle and increments
        its refcount. Otherwise creates a new handle.

        Args:
            target: The protocol implementation to register

        Returns:
            The handle ID for this target
        """
        target_id = id(target)

        # Check if we've seen this object before
        if target_id in self._target_to_handle:
            handle = self._target_to_handle[target_id]
            self._target_refcounts[handle] += 1
            return handle

        # Create new handle
        handle = self._next_handle
        self._next_handle += 1

        self._targets[handle] = target
        self._target_to_handle[target_id] = handle
        self._target_refcounts[handle] = 1

        return handle

    def _decref_mine(self, my_handles: dict[int, int]) -> None:
        """Decrement reference counts for our own handles.

        When a handle's refcount reaches zero, it is removed from the handle dict.

        Args:
            my_handles: Dict mapping handle IDs to decrement counts
        """
        for handle_id, decr_count in my_handles.items():
            if handle_id not in self._target_refcounts:
                continue

            self._target_refcounts[handle_id] -= decr_count

            if self._target_refcounts[handle_id] <= 0:
                # Warn if root handle reaches zero
                if handle_id == 0:
                    warnings.warn("Root target (handle 0) refcount reached zero. This is unusual.")

                # Clean up
                target = self._targets[handle_id]
                del self._targets[handle_id]
                del self._target_to_handle[id(target)]
                del self._target_refcounts[handle_id]

    async def _decref_theirs(self, their_handles: dict[int, int]) -> None:
        """Send a decref message to the counterparty.

        Args:
            their_handles: Dict mapping handle IDs to decrement counts
        """
        if not their_handles:
            return

        # Convert dict to list of [handle_id, count] pairs for CBOR encoding
        handles_list = [[handle_id, count] for handle_id, count in their_handles.items()]
        decref_msg = {
            "type": "decref",
            "handles": handles_list,
        }
        try:
            await self._send_message(decref_msg)
        except Exception as e:
            if self._disconnected.is_set():
                # It is OK not to decrement refcounts if the connection is already disconnected,
                # because the other side will free them anyway. This can happen if this side's
                # refcounts drop to zero right when the connection is being closed.
                return
            else:
                raise

    async def start(self) -> None:
        """Start the connection and send initial info message."""
        # Include root protocol spec if we have a root target
        root_protocol_cbor = None
        entity_data = {}

        if 0 in self._targets:
            root_protocol = self._targets[0].interface_spec()

            # Collect entities needed for the protocol spec
            visitor = dag_util.RecursiveSubgraphCollector(
                include_if=lambda ent: True  # Include all entities for info message
            )
            root_protocol_cbor = _sendable_value_to_cbor(root_protocol, entity_collector=visitor)

            # Serialize collected entities
            entity_data_by_cid = dag_util.entities_to_cid_ipld_map(visitor.visited_entities)
            entity_data = {
                cid.encode(base="base32"): data for cid, data in entity_data_by_cid.items()
            }

        # Send our info message
        info_msg: dict[str, Any] = {
            "type": "info",
            "fxtr_versions": [FXTR_VERSION],
            "known": list(self._known_loader.locator.advertised_entities()),
            "entities": entity_data,
        }

        if root_protocol_cbor is not None:
            info_msg["root_protocol"] = root_protocol_cbor

        await self._send_message(info_msg)

    async def _send_message(self, message: dict[str, Any]) -> None:
        """Send a CBOR-encoded message over the WebSocket."""
        logger.debug(f"WS {id(self)} sending: {repr(message)[:100]}")
        encoded = cbor_util.encode_cbor(message)
        logger.debug(f"WS {id(self)} sending {len(encoded) / (1024 * 1024)} MB")
        await self._websocket.send_bytes(encoded)

    async def _receive_loop(self) -> None:
        """Background task that receives and processes messages."""
        try:
            while True:
                logger.debug(f"WS {id(self)} waiting")
                try:
                    data = await self._websocket.receive_bytes()
                except Exception as e:
                    self._disconnect_error = e
                    # Don't raise immediately; we will error at the _pending_calls site if anybody
                    # is waiting for the result. But shut down the receive loop.
                    return
                message = cbor_util.decode_cbor(data)
                logger.debug(f"WS {id(self)} received {id(message)}: {repr(message)[:100]}")
                assert isinstance(message, dict)
                await self._handle_message(message)
                logger.debug(f"WS {id(self)} done handling {id(message)}")
        finally:
            self._disconnected.set()
            # Wake up all pending calls with error
            for event in self._pending_calls.values():
                event.set()

    async def _receive_until_cancelled(self, shutdown_event: anyio.Event) -> None:
        """Run receive loop until shutdown event is set, then cancel gracefully.

        Args:
            shutdown_event: Event that signals when to stop receiving
        """
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._receive_loop)
            await shutdown_event.wait()
            tg.cancel_scope.cancel()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message based on its type."""
        msg_type = message["type"]

        if msg_type == "info":
            await self._handle_info(message)
        elif msg_type == "call":
            await self._handle_call(message)
        elif msg_type == "response":
            await self._handle_response(message)
        elif msg_type == "decref":
            await self._handle_decref(message)
        else:
            raise ValueError(f"Unknown message type: {msg_type}")

    async def _handle_info(self, message: dict[str, Any]) -> None:
        """Handle an info message from the counterparty."""
        fxtr_versions = message["fxtr_versions"]
        assert isinstance(fxtr_versions, list)
        assert FXTR_VERSION in fxtr_versions

        known = message["known"]
        assert isinstance(known, list)
        self._counterparty_known_cids = set(typing.cast(list[cbrrr.CID], known))

        # Extract root protocol spec if present
        if "root_protocol" in message:
            root_protocol_cbor = message["root_protocol"]
            entities = message.get("entities", {})

            # Build lookup for entities in this message
            entity_data_by_cid = {cbrrr.CID.decode(cid): data for cid, data in entities.items()}

            local_cache = {}

            def lookup(cid: cbrrr.CID) -> core.AnyEntity:
                if cid in local_cache:
                    return local_cache[cid]
                elif cid in entity_data_by_cid:
                    entity = core.ipld_entity_data_to_entity(entity_data_by_cid[cid], lookup)
                    local_cache[cid] = entity
                    return entity
                else:
                    return self._known_loader(cid)

            # Deserialize protocol spec (no connection or decref_counter needed for info message)
            root_protocol_value = _cbor_to_sendable_value(root_protocol_cbor, lookup=lookup)
            assert isinstance(root_protocol_value, core.InterfaceSpec)
            self._counterparty_root_protocol = root_protocol_value

        self._counterparty_info_received.set()

    async def _handle_decref(self, message: dict[str, Any]) -> None:
        """Handle a decref message from the counterparty."""
        handles = message["handles"]
        assert isinstance(handles, list)
        # Convert list of [handle_id, count] pairs to dict
        handles_dict = {handle_id: count for handle_id, count in handles}
        self._decref_mine(handles_dict)

    async def _handle_call(self, message: dict[str, Any]) -> None:
        """Handle a call message from the counterparty."""
        call_id = message["call_id"]
        target_handle = message["target_handle"]
        method = message["method"]
        args_cbor = message.get("args", [])
        kwargs_cbor = message.get("kwargs", {})
        entities = message.get("entities", {})

        # Build lookup for entities in this message
        entity_data_by_cid = {cbrrr.CID.decode(cid): data for cid, data in entities.items()}

        local_cache = {}

        def lookup(cid: cbrrr.CID) -> core.AnyEntity:
            if cid in local_cache:
                return local_cache[cid]
            elif cid in entity_data_by_cid:
                entity = core.ipld_entity_data_to_entity(entity_data_by_cid[cid], lookup)
                local_cache[cid] = entity
                return entity
            else:
                return self._known_loader(cid)

        # Convert args/kwargs from CBOR to SendableValue
        # Track decrefs for handles we encounter
        decref_counter: dict[int, int] = {}
        args = [
            _cbor_to_sendable_value(
                arg, lookup=lookup, connection=self, decref_counter=decref_counter
            )
            for arg in args_cbor
        ]
        kwargs = {
            k: _cbor_to_sendable_value(
                v, lookup=lookup, connection=self, decref_counter=decref_counter
            )
            for k, v in kwargs_cbor.items()
        }

        # Send decrefs for any handles we reused
        if decref_counter:
            await self._decref_theirs(decref_counter)

        # Asynchronously dispatch the call
        self._background_task_group.start_soon(
            self._run_call, call_id, target_handle, method, args, kwargs
        )

    async def _run_call(
        self,
        call_id: int,
        target_handle: int,
        method: str,
        args: list[core.SendableValue],
        kwargs: dict[str, core.SendableValue],
    ) -> None:
        # Call the target
        try:
            target = self._targets.get(target_handle)
            if target is None:
                raise ValueError(f"Unknown target handle: {target_handle}")

            result = await getattr(target, method)(*args, **kwargs)
            success = True
        except Exception as e:
            import traceback

            result = RPCCallErrorInfo(error=traceback.format_exc(), type=type(e).__name__)
            success = False

        # Send response
        await self._send_call_response(call_id, result, success)

    async def _send_call_response(
        self, call_id: int, result: core.SendableValue, success: bool
    ) -> None:
        """Send a response message for a call."""
        # Convert result to CBOR, collecting entities
        visitor = dag_util.RecursiveSubgraphCollector(
            include_if=lambda ent: core.entity_cid(ent) not in self._counterparty_known_cids
        )
        result_cbor = _sendable_value_to_cbor(result, entity_collector=visitor, connection=self)

        # Serialize collected entities
        entity_data_by_cid = dag_util.entities_to_cid_ipld_map(visitor.visited_entities)
        entity_data = {cid.encode(base="base32"): data for cid, data in entity_data_by_cid.items()}

        response = {
            "type": "response",
            "call_id": call_id,
            "success": success,
            "result": result_cbor,
            "entities": entity_data,
        }
        await self._send_message(response)

    async def _handle_response(self, message: dict[str, Any]) -> None:
        """Handle a response message for one of our calls."""
        call_id = message["call_id"]
        success = message["success"]
        result_cbor = message["result"]
        entities = message.get("entities", {})

        # Build lookup for entities in this message
        entity_data_by_cid = {cbrrr.CID.decode(cid): data for cid, data in entities.items()}

        local_cache = {}

        def lookup(cid: cbrrr.CID) -> core.AnyEntity:
            if cid in local_cache:
                return local_cache[cid]
            elif cid in entity_data_by_cid:
                entity = core.ipld_entity_data_to_entity(entity_data_by_cid[cid], lookup)
                local_cache[cid] = entity
                return entity
            else:
                return self._known_loader(cid)

        # Convert result from CBOR to SendableValue
        # Track decrefs for handles we encounter
        decref_counter: dict[int, int] = {}
        result = _cbor_to_sendable_value(
            result_cbor, lookup=lookup, connection=self, decref_counter=decref_counter
        )

        # Send decrefs for any handles we reused
        if decref_counter:
            await self._decref_theirs(decref_counter)

        # Store result and wake up the waiting call
        self._call_results[call_id] = (success, result)
        if call_id in self._pending_calls:
            self._pending_calls[call_id].set()

    async def call(
        self,
        target_handle: int,
        method: str,
        *args: core.SendableValue,
        **kwargs: core.SendableValue,
    ) -> core.SendableValue:
        """Make an RPC call to a target object.

        Args:
            target_handle: Handle identifying the target object (0 = root)
            method: Name of the method to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            The result of the method call

        Raises:
            RemoteRPCError: If the call fails or connection is lost
        """
        # Wait for counterparty info if we don't have it yet
        await self._counterparty_info_received.wait()

        # Check if disconnected
        if self._disconnected.is_set():
            raise RemoteRPCError(
                error_info={"error": "WebSocket disconnected"}, message="WebSocket disconnected"
            )

        # Allocate call ID
        call_id = self._next_call_id
        self._next_call_id += 1

        # Create event for this call
        event = anyio.Event()
        self._pending_calls[call_id] = event

        try:
            # Convert args/kwargs to CBOR, collecting entities
            visitor = dag_util.RecursiveSubgraphCollector(
                include_if=lambda ent: core.entity_cid(ent) not in self._counterparty_known_cids
            )

            args_cbor = [
                _sendable_value_to_cbor(arg, entity_collector=visitor, connection=self)
                for arg in args
            ]
            kwargs_cbor = {
                k: _sendable_value_to_cbor(v, entity_collector=visitor, connection=self)
                for k, v in kwargs.items()
            }

            # Serialize collected entities
            entity_data_by_cid = dag_util.entities_to_cid_ipld_map(visitor.visited_entities)
            entity_data = {
                cid.encode(base="base32"): data for cid, data in entity_data_by_cid.items()
            }

            # Send call message
            call_msg = {
                "type": "call",
                "call_id": call_id,
                "target_handle": target_handle,
                "method": method,
                "args": args_cbor,
                "kwargs": kwargs_cbor,
                "entities": entity_data,
            }
            await self._send_message(call_msg)

            # Wait for response
            await event.wait()

            # Check if disconnected while waiting
            if self._disconnected.is_set():
                raise RemoteRPCError(
                    error_info={"error": "WebSocket disconnected"}, message="WebSocket disconnected"
                )

            # Get result
            if call_id not in self._call_results:
                raise RemoteRPCError(
                    error_info={"error": "Response not received"}, message="Response not received"
                )

            success, result = self._call_results[call_id]

            if not success:
                raise RemoteRPCError(error_info=result)

            return result

        finally:
            # Clean up
            self._pending_calls.pop(call_id, None)
            self._call_results.pop(call_id, None)


class RemoteRPCError(Exception):
    """Error raised when a remote RPC call fails."""

    def __init__(self, error_info: Any, message: str | None = None):
        """Initialize the error.

        Args:
            error_info: The raw error information returned by the remote server
            message: Optional custom error message. If not provided, will be derived from error_info
        """
        self.error_info = error_info

        if message is None:
            if isinstance(error_info, RPCCallErrorInfo):
                message = f"Remote RPC error ({error_info.type}): {error_info.error}"
            elif isinstance(error_info, dict) and "error" in error_info:
                message = f"Remote RPC error: {error_info['error']}"
            else:
                message = f"Remote RPC error: {error_info}"

        super().__init__(message)


class RemoteSubtargetImpl(core.FxtrCallTarget):
    """Proxy for a sub-target on a remote RPC connection.

    This class is simpler than RemoteImpl - it attaches to an existing connection and handle ID,
    and proxies calls to that specific target without managing connection lifecycle.
    """

    def __init__(
        self, connection: FxtrRPCConnection, handle_id: int, interface_spec: core.InterfaceSpec
    ):
        """Initialize the remote subtarget proxy.

        Args:
            connection: The RPC connection to use
            handle_id: The handle ID of the target on the remote side
            interface_spec: The interface spec for this implementation
        """
        self._connection = connection
        self._handle_id = handle_id
        self._interface_spec = interface_spec

    def interface_spec(self) -> core.InterfaceSpec:
        return self._interface_spec

    def __getattr__(self, method: str) -> Any:
        """Proxy method calls to the remote target."""
        # Only allow public methods and __call__
        if not ((not method.startswith("_")) or (method == "__call__")):
            raise AttributeError(f"Method {method} is not exposed to RPC!")
        return functools.partial(self._method_call, method)

    async def _method_call(self, method: str, *args, **kwargs) -> Any:
        """Call a method on the remote target."""
        defn_name = self._interface_spec.defn.interface_id
        with logfire.span(
            "{defn_name}.{method}(...) [handle={handle_id}]",
            defn_name=defn_name,
            method=method,
            handle_id=self._handle_id,
        ):
            result = await self._connection.call(self._handle_id, method, *args, **kwargs)
        return result
