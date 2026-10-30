# Step 3: GraphQL Backend + Debug Triggers

Implementation spec for the GraphQL API, HTTP server, and debug trigger mechanism.

## Overview

Add a Starlette HTTP server with Ariadne GraphQL endpoint that allows querying the watcher state. Also add REST endpoints for manual tick triggering and debug triggers for testing.

## Design Decisions

- **GraphQL library**: Ariadne (schema-first, good async support)
- **HTTP framework**: Starlette (async, lightweight)
- **Subscriptions**: WebSockets at `/graphql`
- **Async primitives**: anyio (not asyncio) for portability
- **Key encoding**: Base64-encoded CBOR bytes (String in schema)
- **Data encoding**: JSON scalar (native objects, not JSON-encoded strings)
- **CBOR→JSON limitation**: CBOR types without JSON equivalents (bytes, datetime, etc.) raise a GraphQL error with a helpful message
- **Change detection**: MODIFIED if either data OR links changed
- **thingHistory**: Returns reconstructed data chunk chains for each historical state
- **triggerTick defaults**: type="remote_client", desc=""

---

## GraphQL Schema

```graphql
# Custom scalar for arbitrary JSON objects (decoded CBOR data)
scalar JSON

type Query {
  # Timeline
  latestTick: Int!
  ticks(fromTick: Int, toTick: Int, levels: [String!]): [Tick!]!

  # Point-in-time (for drawing graph)
  thingsAtTick(tick: Int!): [Thing!]!

  # Delta (for efficient updates)
  changedThings(fromTick: Int!, toTick: Int!): [ThingChange!]!

  # Detail
  thingData(tick: Int!, key: String!): ThingData
  thingHistory(key: String!, fromTick: Int, toTick: Int): [ThingData!]!
  dataChunk(id: Int!): DataChunk  # Fetch single chunk by thingstate_id

  # Config
  renderingConfig: RenderingConfig!
}

type Subscription {
  newTick: Tick!
}

type Mutation {
  triggerTick(level: String = "info", desc: String = ""): Tick!
}

# --- Core Types ---

type Tick {
  tick: Int!
  timestamp: Float!
  level: String!
  type: String!
  data: JSON  # Arbitrary JSON object, nullable
}

type Thing {
  key: String!        # Base64-encoded CBOR
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
}

type Link {
  linkType: String!
  linkKey: String!    # Base64-encoded CBOR
  toThingKey: String! # Base64-encoded CBOR
}

type DataChunk {
  id: Int!            # thingstate_id - stable identifier for caching
  data: JSON          # Native JSON object, null if includeData=false
}

type ThingData {
  tick: Int!          # The tick this data is valid at (or valid_from for history)
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  dataChunks: [DataChunk!]!  # Full chain from base to current, ordered oldest-first
}

enum ChangeKind {
  ADDED
  MODIFIED
  REMOVED
}

type ThingChange {
  kind: ChangeKind!
  thing: Thing!       # For REMOVED, the last known state before removal
}

# --- Config Types ---

type RenderingConfig {
  thingTypes: [ThingTypeConfig!]!
}

type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
}

type LinkColorConfig {
  linkType: String!
  color: String!
}
```

---

## REST Endpoints

### POST /debug_trigger/{name}

Fire a named debug trigger for test synchronization.

- **Body**: CBOR-encoded payload (can be empty)
- **Response**: `{"ok": true}` or `{"ok": false, "error": "..."}` (400 if no waiter)

---

## File Structure

```
thingwatcher/
├── server.py         # Starlette app, routes, WebSocket handling
├── schema.graphql    # GraphQL schema (above)
├── resolvers.py      # Query/Mutation/Subscription resolvers
└── db.py             # Add read methods for queries
```

---

## File: `thingwatcher/db.py` Additions

Add read methods for GraphQL resolvers:

```python
async def get_latest_tick(self, conn: AsyncConnection) -> int | None:
    """Get the highest tick number, or None if no ticks."""
    result = await conn.execute(
        select(ticks.c.tick).order_by(ticks.c.tick.desc()).limit(1)
    )
    row = result.fetchone()
    return row.tick if row else None

async def get_ticks(
    self, conn: AsyncConnection,
    from_tick: int | None = None,
    to_tick: int | None = None,
    levels: list[str] | None = None,
) -> list[Row]:
    """Get ticks in range, optionally filtered by level."""
    query = select(ticks).order_by(ticks.c.tick)
    if from_tick is not None:
        query = query.where(ticks.c.tick >= from_tick)
    if to_tick is not None:
        query = query.where(ticks.c.tick <= to_tick)
    if levels:
        query = query.where(ticks.c.level.in_(levels))
    result = await conn.execute(query)
    return result.fetchall()

async def get_things_at_tick(self, conn: AsyncConnection, tick: int) -> list[Row]:
    """Get all thing_states valid at the given tick."""
    query = select(thing_states).where(
        thing_states.c.valid_from <= tick,
        (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > tick)
    )
    result = await conn.execute(query)
    return result.fetchall()

async def get_links_at_tick(self, conn: AsyncConnection, tick: int) -> list[Row]:
    """Get all links valid at the given tick."""
    query = select(links).where(
        links.c.valid_from <= tick,
        (links.c.valid_until.is_(None)) | (links.c.valid_until > tick)
    )
    result = await conn.execute(query)
    return result.fetchall()

async def get_thing_data(
    self, conn: AsyncConnection, tick: int, key_bytes: bytes
) -> Row | None:
    """Get the thing_state for a specific key at a tick."""
    query = select(thing_states).where(
        thing_states.c.thing_key == key_bytes,
        thing_states.c.valid_from <= tick,
        (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > tick)
    )
    result = await conn.execute(query)
    return result.fetchone()

async def get_thing_state_by_id(self, conn: AsyncConnection, state_id: int) -> Row | None:
    """Get a thing_state by its ID (for following data_extends chain)."""
    result = await conn.execute(
        select(thing_states).where(thing_states.c.id == state_id)
    )
    return result.fetchone()

async def get_thing_history(
    self, conn: AsyncConnection,
    key_bytes: bytes,
    from_tick: int | None = None,
    to_tick: int | None = None,
) -> list[Row]:
    """Get all states for a thing key that were valid at any point in the range.

    A state is included if it overlaps with [from_tick, to_tick]:
    - valid_from <= to_tick (started before or at the end of range)
    - valid_until is None or valid_until > from_tick (still valid after start of range)
    """
    query = select(thing_states).where(
        thing_states.c.thing_key == key_bytes
    ).order_by(thing_states.c.valid_from)
    if from_tick is not None:
        # State must still be valid after from_tick
        query = query.where(
            (thing_states.c.valid_until.is_(None)) | (thing_states.c.valid_until > from_tick)
        )
    if to_tick is not None:
        # State must have started by to_tick
        query = query.where(thing_states.c.valid_from <= to_tick)
    result = await conn.execute(query)
    return result.fetchall()

async def get_links_for_thing(
    self, conn: AsyncConnection, tick: int, from_key_bytes: bytes
) -> list[Row]:
    """Get all links from a specific thing at a tick."""
    query = select(links).where(
        links.c.from_thing_key == from_key_bytes,
        links.c.valid_from <= tick,
        (links.c.valid_until.is_(None)) | (links.c.valid_until > tick)
    )
    result = await conn.execute(query)
    return result.fetchall()
```

---

## File: `thingwatcher/server.py`

```python
"""HTTP server with GraphQL endpoint for thingwatcher."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

import anyio
import cbor2

from ariadne import (
    EnumType,
    MutationType,
    QueryType,
    ScalarType,
    SubscriptionType,
    load_schema_from_path,
    make_executable_schema,
)
from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLWSHandler
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection
    from .watcher import Watcher

# Schema will be loaded from schema.graphql
type_defs = None  # Loaded at module init or lazily

query = QueryType()
mutation = MutationType()
subscription = SubscriptionType()
change_kind = EnumType("ChangeKind", {"ADDED": "added", "MODIFIED": "modified", "REMOVED": "removed"})

# JSON scalar - passes through Python objects directly (dicts, lists, primitives)
json_scalar = ScalarType("JSON")

@json_scalar.serializer
def serialize_json(value):
    """JSON scalar serializer - pass through as-is."""
    return value

@json_scalar.value_parser
def parse_json_value(value):
    """JSON scalar value parser - pass through as-is."""
    return value


def encode_key(key_bytes: bytes) -> str:
    """Encode CBOR key bytes as base64 for GraphQL."""
    return base64.b64encode(key_bytes).decode("ascii")


def decode_key(key_str: str) -> bytes:
    """Decode base64 key string back to CBOR bytes.

    Raises GraphQL error if the key is not valid base64.
    """
    from graphql import GraphQLError
    try:
        return base64.b64decode(key_str, validate=True)
    except Exception as e:
        raise GraphQLError(f"Invalid base64 key: {e}")


logger = logging.getLogger(__name__)


def _is_json_compatible(obj: Any) -> bool:
    """Check if an object is JSON-compatible (recursively)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return True
    if isinstance(obj, dict):
        return all(_is_json_compatible(k) and _is_json_compatible(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_is_json_compatible(item) for item in obj)
    return False


class CborJsonError(Exception):
    """Raised when CBOR data cannot be converted to JSON."""
    pass


def decode_cbor_data(data_bytes: bytes, raise_on_incompatible: bool = False) -> Any:
    """Decode CBOR data to JSON-compatible Python object.

    Args:
        data_bytes: CBOR-encoded bytes.
        raise_on_incompatible: If True, raise CborJsonError for non-JSON types.
            If False, log warning and return None.

    Returns:
        JSON-compatible Python object, or None if incompatible and not raising.

    Raises:
        CborJsonError: If raise_on_incompatible=True and data contains non-JSON types.
    """
    raw = cbor2.loads(data_bytes)
    if _is_json_compatible(raw):
        return raw
    if raise_on_incompatible:
        raise CborJsonError("CBOR data contains non-JSON-compatible types (bytes, datetime, etc.)")
    logger.warning(
        f"CBOR data contains non-JSON-compatible types - returning None"
    )
    return None


# --- Query Resolvers ---

@query.field("latestTick")
async def resolve_latest_tick(_, info) -> int:
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        result = await watcher._db.get_latest_tick(conn)
        return result if result is not None else 0

@query.field("ticks")
async def resolve_ticks(_, info, fromTick=None, toTick=None, levels=None) -> list[dict]:
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        rows = await watcher._db.get_ticks(conn, fromTick, toTick, levels)
        return [
            {
                "tick": row.tick,
                "timestamp": row.timestamp,
                "level": row.level,
                "type": row.type,
                "data": decode_cbor_data(row.data) if row.data else None,
            }
            for row in rows
        ]

@query.field("thingsAtTick")
async def resolve_things_at_tick(_, info, tick: int) -> list[dict]:
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        things = await watcher._db.get_things_at_tick(conn, tick)
        all_links = await watcher._db.get_links_at_tick(conn, tick)

        # Group links by from_thing_key
        links_by_key: dict[bytes, list[dict]] = {}
        for link in all_links:
            key = link.from_thing_key
            if key not in links_by_key:
                links_by_key[key] = []
            links_by_key[key].append({
                "linkType": link.link_type,
                "linkKey": encode_key(link.link_key),
                "toThingKey": encode_key(link.to_thing_key),
            })

        return [
            {
                "key": encode_key(thing.thing_key),
                "thingType": thing.thing_type,
                "triggerable": bool(thing.triggerable),
                "links": links_by_key.get(thing.thing_key, []),
            }
            for thing in things
        ]

@query.field("changedThings")
async def resolve_changed_things(_, info, fromTick: int, toTick: int) -> list[dict]:
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        # Get things at both ticks
        things_at_from = await watcher._db.get_things_at_tick(conn, fromTick)
        things_at_to = await watcher._db.get_things_at_tick(conn, toTick)
        links_at_from = await watcher._db.get_links_at_tick(conn, fromTick)
        links_at_to = await watcher._db.get_links_at_tick(conn, toTick)

        # Build maps: key -> (state_id, thing_row)
        from_map = {t.thing_key: (t.id, t) for t in things_at_from}
        to_map = {t.thing_key: (t.id, t) for t in things_at_to}

        # Build link sets for comparison: key -> set of (link_type, link_key, to_key)
        def link_set(link_rows, key):
            return {
                (l.link_type, l.link_key, l.to_thing_key)
                for l in link_rows if l.from_thing_key == key
            }

        from_links = {k: link_set(links_at_from, k) for k in from_map}
        to_links = {k: link_set(links_at_to, k) for k in to_map}

        changes = []

        # ADDED: in to but not in from
        for key in to_map:
            if key not in from_map:
                state_id, thing = to_map[key]
                changes.append({
                    "kind": "added",
                    "thing": _thing_to_dict(thing, to_links.get(key, set())),
                })

        # MODIFIED: in both but state_id changed OR links changed
        for key in to_map:
            if key in from_map:
                from_id, from_thing = from_map[key]
                to_id, to_thing = to_map[key]
                data_changed = (from_id != to_id)
                links_changed = (from_links.get(key, set()) != to_links.get(key, set()))
                if data_changed or links_changed:
                    changes.append({
                        "kind": "modified",
                        "thing": _thing_to_dict(to_thing, to_links.get(key, set())),
                    })

        # REMOVED: in from but not in to
        for key in from_map:
            if key not in to_map:
                state_id, thing = from_map[key]
                changes.append({
                    "kind": "removed",
                    "thing": _thing_to_dict(thing, from_links.get(key, set())),
                })

        return changes


def _thing_to_dict(thing_row, link_tuples: set) -> dict:
    """Convert a thing row and link tuples to GraphQL Thing dict."""
    return {
        "key": encode_key(thing_row.thing_key),
        "thingType": thing_row.thing_type,
        "triggerable": bool(thing_row.triggerable),
        "links": [
            {
                "linkType": lt,
                "linkKey": encode_key(lk),
                "toThingKey": encode_key(tk),
            }
            for lt, lk, tk in link_tuples
        ],
    }


async def _get_data_chunks_chain(
    watcher: "Watcher", conn: "AsyncConnection", thing_state
) -> list[tuple[int, Any]]:
    """Follow data_extends chain, return list of (state_id, data) tuples, oldest first.

    Uses decode_cbor_data to ensure JSON compatibility. Raises GraphQL error if
    any chunk contains non-JSON-compatible data.
    """
    from graphql import GraphQLError

    chunks = []
    current = thing_state
    while current is not None:
        try:
            data = decode_cbor_data(current.data, raise_on_incompatible=True)
        except CborJsonError as e:
            raise GraphQLError(f"Data chunk {current.id} contains non-JSON-compatible CBOR: {e}")
        chunks.append((current.id, data))
        if current.data_extends is not None:
            current = await watcher._db.get_thing_state_by_id(conn, current.data_extends)
        else:
            current = None
    chunks.reverse()  # Oldest first
    return chunks


@query.field("thingData")
async def resolve_thing_data(_, info, tick: int, key: str) -> dict | None:
    watcher: Watcher = info.context["watcher"]
    key_bytes = decode_key(key)

    async with watcher._db.begin() as conn:
        thing = await watcher._db.get_thing_data(conn, tick, key_bytes)
        if thing is None:
            return None

        # Get the full data_extends chain
        chunks = await _get_data_chunks_chain(watcher, conn, thing)
        links = await watcher._db.get_links_for_thing(conn, tick, key_bytes)

        return {
            "tick": tick,
            "key": encode_key(thing.thing_key),
            "thingType": thing.thing_type,
            "triggerable": bool(thing.triggerable),
            "links": [
                {
                    "linkType": l.link_type,
                    "linkKey": encode_key(l.link_key),
                    "toThingKey": encode_key(l.to_thing_key),
                }
                for l in links
            ],
            "dataChunks": [
                {"id": chunk_id, "data": data}
                for chunk_id, data in chunks
            ],
        }


@query.field("thingHistory")
async def resolve_thing_history(_, info, key: str, fromTick=None, toTick=None) -> list[dict]:
    watcher: Watcher = info.context["watcher"]
    key_bytes = decode_key(key)

    async with watcher._db.begin() as conn:
        states = await watcher._db.get_thing_history(conn, key_bytes, fromTick, toTick)

        result = []
        for state in states:
            # Get full data chain for this state
            chunks = await _get_data_chunks_chain(watcher, conn, state)
            # Get links valid at this state's valid_from tick
            links = await watcher._db.get_links_for_thing(conn, state.valid_from, key_bytes)
            result.append({
                "tick": state.valid_from,
                "key": encode_key(state.thing_key),
                "thingType": state.thing_type,
                "triggerable": bool(state.triggerable),
                "links": [
                    {
                        "linkType": l.link_type,
                        "linkKey": encode_key(l.link_key),
                        "toThingKey": encode_key(l.to_thing_key),
                    }
                    for l in links
                ],
                "dataChunks": [
                    {"id": chunk_id, "data": data}
                    for chunk_id, data in chunks
                ],
            })

        return result


@query.field("dataChunk")
async def resolve_data_chunk(_, info, id: int) -> dict | None:
    """Fetch a single data chunk by its thingstate_id."""
    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        state = await watcher._db.get_thing_state_by_id(conn, id)
        if state is None:
            return None

        return {
            "id": state.id,
            "data": decode_cbor_data(state.data),
        }


@query.field("renderingConfig")
async def resolve_rendering_config(_, info) -> dict:
    from .registry import get_rendering_configs
    configs = get_rendering_configs()
    return {
        "thingTypes": [
            {
                "thingType": thing_type,
                "color": cfg["color"],
                "linkColors": [
                    {"linkType": lt, "color": c}
                    for lt, c in cfg.get("link_colors", {}).items()
                ],
            }
            for thing_type, cfg in configs.items()
        ]
    }


# --- Mutation Resolvers ---

@mutation.field("triggerTick")
async def resolve_trigger_tick(_, info, level: str = "info", desc: str = "") -> dict:
    watcher: Watcher = info.context["watcher"]
    return await watcher._async_tick(level, desc or None, "remote_client")


# --- Subscription Resolvers ---

@subscription.source("newTick")
async def new_tick_source(_, info):
    """Generator that yields when new ticks occur."""
    watcher: Watcher = info.context["watcher"]

    # Create a queue and register a callback that feeds it
    queue: anyio.abc.ObjectReceiveStream
    send, queue = anyio.create_memory_object_stream[dict](max_buffer_size=100)

    def on_tick(tick_data: dict) -> None:
        try:
            send.send_nowait(tick_data)
        except anyio.WouldBlock:
            pass  # Buffer full, drop

    watcher._add_tick_callback(on_tick)
    try:
        async for tick_data in queue:
            yield tick_data
    finally:
        watcher._remove_tick_callback(on_tick)
        await send.aclose()


@subscription.field("newTick")
def new_tick_resolver(tick_data, info):
    return tick_data


# --- REST Routes ---

async def handle_debug_trigger(request: Request) -> JSONResponse:
    """Handle POST /debug_trigger/{name}."""
    from .watcher import DebugTriggerError

    name = request.path_params["name"]
    watcher: Watcher = request.app.state.watcher

    body = await request.body()
    payload = cbor2.loads(body) if body else None

    try:
        watcher._fire_debug_trigger(name, payload)
        return JSONResponse({"ok": True})
    except DebugTriggerError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def create_app(watcher: "Watcher") -> Starlette:
    """Create the Starlette application with GraphQL endpoint."""
    import pathlib

    schema_path = pathlib.Path(__file__).parent / "schema.graphql"
    type_defs = load_schema_from_path(str(schema_path))

    schema = make_executable_schema(
        type_defs, query, mutation, subscription, change_kind, json_scalar
    )

    graphql_app = GraphQL(
        schema,
        context_value=lambda request: {"watcher": watcher, "request": request},
        websocket_handler=GraphQLWSHandler(),
    )

    # REST routes only - GraphQL is mounted separately to handle both HTTP and WebSocket
    routes = [
        Route("/debug_trigger/{name}", handle_debug_trigger, methods=["POST"]),
    ]

    app = Starlette(routes=routes)
    app.state.watcher = watcher

    # Mount GraphQL app (handles both HTTP POST and WebSocket subscriptions)
    app.mount("/graphql", graphql_app)

    return app
```

---

## File: `thingwatcher/watcher.py` Changes

### New Imports
```python
import anyio
from anyio import Event
from typing import Callable
```

### New State in `__init__`
```python
# Subscription support: simple callback functions
self._tick_callbacks: list[Callable[[dict], None]] = []
self._tick_callbacks_lock = threading.Lock()

# Debug trigger support: only one waiter per trigger name allowed
self._debug_triggers: dict[str, Event] = {}
self._debug_payloads: dict[str, Any] = {}
self._debug_lock = threading.Lock()
```

### Subscription Methods
```python
def _add_tick_callback(self, callback: Callable[[dict], None]) -> None:
    """Register a callback to be called on each new tick."""
    with self._tick_callbacks_lock:
        self._tick_callbacks.append(callback)

def _remove_tick_callback(self, callback: Callable[[dict], None]) -> None:
    """Unregister a tick callback."""
    with self._tick_callbacks_lock:
        if callback in self._tick_callbacks:
            self._tick_callbacks.remove(callback)

def _notify_tick_subscribers(self, tick_data: dict) -> None:
    """Notify all subscribers of a new tick (called from background writer)."""
    with self._tick_callbacks_lock:
        callbacks = list(self._tick_callbacks)  # Copy to avoid holding lock during calls
    for callback in callbacks:
        try:
            callback(tick_data)
        except Exception:
            pass  # Don't let one bad callback break others
```

### Debug Trigger Methods
```python
class DebugTriggerError(Exception):
    """Raised when debug trigger operations fail."""
    pass

def _fire_debug_trigger(self, name: str, payload: Any) -> None:
    """Fire a debug trigger, waking any waiters.

    Raises:
        DebugTriggerError: If no waiter is registered for this trigger name.
    """
    with self._debug_lock:
        if name not in self._debug_triggers:
            raise DebugTriggerError(f"No waiter registered for debug trigger: {name}")
        self._debug_payloads[name] = payload
        self._debug_triggers[name].set()

async def debug_receive(self, name: str, timeout: float | None = None) -> Any:
    """Wait for a debug trigger and return its payload.

    Only one waiter per trigger name is allowed at a time.

    Args:
        name: The trigger name to wait for.
        timeout: Optional timeout in seconds.

    Returns:
        The payload sent with the trigger.

    Raises:
        DebugTriggerError: If another waiter is already registered for this name.
        TimeoutError: If timeout expires.
    """
    with self._debug_lock:
        if name in self._debug_triggers:
            raise DebugTriggerError(f"Another waiter already registered for: {name}")
        event = Event()
        self._debug_triggers[name] = event

    try:
        if timeout is not None:
            with anyio.fail_after(timeout):
                await event.wait()
        else:
            await event.wait()

        with self._debug_lock:
            return self._debug_payloads.pop(name, None)
    finally:
        # Always clean up the trigger registration
        with self._debug_lock:
            self._debug_triggers.pop(name, None)
```

### Async Remote Tick Method
```python
async def _async_tick(self, level: str, desc: str | None, tick_type: str) -> dict:
    """Trigger a tick and wait for it to be written. Returns tick data.

    Unlike the public tick() method which is sync and fire-and-forget,
    this method waits for the write to complete and returns the tick info.

    Args:
        level: The level of the tick.
        desc: Optional description.
        tick_type: The tick type (e.g., "remote_client").

    Returns:
        Dict with tick info: {tick, timestamp, level, type, data}
    """
    # Create an event to wait on
    done_event = Event()
    result_holder: dict = {}

    def on_write_complete(tick_data: dict) -> None:
        result_holder.update(tick_data)
        done_event.set()

    # Build the WriteJob with completion callback
    # (Requires adding optional callback to WriteJob dataclass)
    with self._lock:
        tick_number = self._next_tick
        self._next_tick += 1
        timestamp = time.time()

        # Snapshot all tracked objects (same as tick())
        snapshots = self._snapshot_all_things()

    job = WriteJob(
        tick_number=tick_number,
        timestamp=timestamp,
        level=level,
        tick_type=tick_type,
        tick_data=desc,
        snapshots=snapshots,
        on_complete=on_write_complete,  # New field
    )

    self._write_stream.send_nowait(job)
    await done_event.wait()

    return result_holder
```

**Modification to WriteJob dataclass:**
```python
@dataclass
class WriteJob:
    tick_number: int
    timestamp: float
    level: str
    tick_type: str
    tick_data: Any
    snapshots: list[SnapshotResult]
    on_complete: Callable[[dict], None] | None = None  # Optional callback
```

**Modification to _process_write_job:**
```python
async def _process_write_job(self, job: WriteJob) -> None:
    # ... existing write logic ...

    # After transaction commits
    tick_data = {
        "tick": job.tick_number,
        "timestamp": job.timestamp,
        "level": job.level,
        "type": job.tick_type,
        "data": job.tick_data,
    }

    # Notify subscribers
    self._notify_tick_subscribers(tick_data)

    # Call completion callback if present
    if job.on_complete is not None:
        job.on_complete(tick_data)
```

### Modified `watch_and_serve_until`
Start the HTTP server:
```python
async def watch_and_serve_until(self, done: Callable[[], Awaitable[Any]]) -> Any:
    token = set_current_watcher(self)
    try:
        # Initialize database
        self._db = Database(self.db_path)
        await self._db.connect()

        # Create write stream and start writer task
        send_stream, receive_stream = anyio.create_memory_object_stream[WriteJob](
            max_buffer_size=100
        )
        self._write_stream = send_stream

        # Create HTTP server
        from .server import create_app
        import uvicorn

        app = create_app(self)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        server = uvicorn.Server(config)

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._db_writer, receive_stream)
            tg.start_soon(server.serve)

            try:
                result = await done()
            finally:
                server.should_exit = True
                await send_stream.aclose()

        return result
    finally:
        if self._db:
            await self._db.close()
        self._write_stream = None
        self._current_things.clear()
        self._tick_callbacks.clear()
        reset_current_watcher(token)
```

---

## File: `thingwatcher/registry.py` Addition

Add method to retrieve rendering configs:
```python
def get_rendering_configs() -> dict[str, dict]:
    """Get all rendering configurations."""
    return dict(_rendering_configs)
```

---

## Dependencies to Add

```bash
uv add ariadne uvicorn starlette
```

---

## Tests: `tests/test_graphql.py`

### Test: latestTick query
```
- Create watcher, tick once
- Query latestTick, verify returns 1
```

### Test: ticks query
```
- Create multiple ticks with different levels
- Query all ticks, verify count and order
- Query with level filter, verify filtering works
```

### Test: thingsAtTick query
```
- Track objects, tick
- Query thingsAtTick, verify things returned with correct keys/types
- Verify links are embedded correctly
```

### Test: changedThings query
```
- Track A and B, tick
- Modify A, tick
- Untrack B, tick
- Query changedThings(1, 2): A=MODIFIED
- Query changedThings(2, 3): B=REMOVED
- Query changedThings(1, 3): A=MODIFIED, B=REMOVED
```

### Test: link-only change is MODIFIED
```
- Track object with link to X, tick
- Change link to Y (same data), tick
- Query changedThings: should show MODIFIED
```

### Test: thingData query
```
- Track object, tick
- Query thingData, verify dataChunks returned with id and data
```

### Test: thingData with data_extends chain
```
- Track object that uses append deltas, tick multiple times
- Query thingData, verify dataChunks is ordered list of all chunks in chain
- Verify each chunk has correct id and data payload
```

### Test: dataChunk query
```
- Track object, tick multiple times with appends
- Query dataChunk for a specific id
- Verify returns correct data payload
```

### Test: thingHistory query
```
- Track object, tick
- Modify, tick
- Modify, tick
- Query thingHistory, verify 3 ThingData entries
- Each entry has full dataChunks chain at that point in time
```

### Test: renderingConfig query
```
- Configure rendering for a type
- Query renderingConfig, verify config returned
```

### Test: newTick subscription
```
- Subscribe to newTick
- Trigger tick
- Verify subscription receives tick data
```

### Test: triggerTick mutation
```
- Call triggerTick mutation
- Verify tick was created with type="remote_client"
- Verify returns tick data immediately (no polling needed)
```

### Test: debug_trigger and debug_receive
```
- Start task that calls debug_receive("test", timeout=5)
- POST to /debug_trigger/test with payload
- Verify task completes with payload
- Verify trigger is cleaned up (can register again)
```

### Test: debug_receive timeout
```
- Call debug_receive with short timeout
- Verify TimeoutError raised
- Verify trigger is cleaned up after timeout
```

### Test: debug_receive duplicate waiter error
```
- Start task that calls debug_receive("test")
- Try to call debug_receive("test") again
- Verify DebugTriggerError raised
```

### Test: debug_trigger without waiter error
```
- POST to /debug_trigger/test without any waiter
- Verify 400 response with error message
```

### Test: triggerTick creates remote_client type
```
- Call triggerTick mutation
- Verify tick type is "remote_client", not "api_tick"
```

### Test: invalid base64 key returns GraphQL error
```
- Query thingData with invalid base64 key (e.g., "not-valid-base64!")
- Verify GraphQL error returned with helpful message
```

### Test: non-JSON CBOR data returns GraphQL error
```
- Track object with bytes or datetime in data
- Tick
- Query thingData
- Verify GraphQL error returned explaining the incompatibility
```

---

## Implementation Order

1. Add dependencies (ariadne, uvicorn, starlette)
2. Create `schema.graphql`
3. Add read methods to `db.py`
4. Add `get_rendering_configs` to `registry.py`
5. Create `server.py` with resolvers
6. Add subscription/debug trigger support to `watcher.py`
7. Modify `watch_and_serve_until` to start server
8. Write tests
9. Run tests, iterate

---

## Exit Criteria

- [ ] GraphQL endpoint at /graphql responds to queries
- [ ] latestTick returns current tick number
- [ ] ticks query returns tick list with filtering
- [ ] thingsAtTick returns things with embedded links
- [ ] changedThings returns ADDED/MODIFIED/REMOVED correctly
- [ ] Link-only changes detected as MODIFIED
- [ ] thingData returns dataChunks with full chain (id + data)
- [ ] dataChunk(id) fetches single chunk payload
- [ ] thingHistory returns list of ThingData with full chains
- [ ] renderingConfig returns color mappings
- [ ] newTick subscription pushes on new tick via WebSocket
- [ ] triggerTick mutation creates tick with type="remote_client" and returns immediately
- [ ] POST /debug_trigger/{name} fires trigger (or 400 if no waiter)
- [ ] debug_receive waits and returns payload, then cleans up
- [ ] debug_receive raises DebugTriggerError if duplicate waiter
- [ ] debug_receive raises TimeoutError on timeout and cleans up
- [ ] CBOR bytes/datetime values raise GraphQL error with helpful message
- [ ] Invalid base64 keys raise GraphQL error
- [ ] All existing tests still pass
- [ ] New GraphQL tests pass

---

## Implementation Status (complete)

### Completed
- [x] Dependencies added (ariadne, hypercorn, starlette, httpx, gql, websockets for testing)
- [x] `schema.graphql` created with full schema
- [x] Read methods added to `db.py`
- [x] `server.py` created with all resolvers
- [x] Subscription support added to `watcher.py` (`_tick_callbacks`, `_notify_tick_subscribers`)
- [x] Debug trigger support added to `watcher.py` (`debug_receive`, `_fire_debug_trigger`, `DebugTriggerError`)
- [x] `_async_tick` method added for remote tick mutation
- [x] `watch_and_serve_until` modified to optionally start HTTP server (port=None disables)
- [x] Existing tests updated to use `port=None` to avoid server startup
- [x] Server changed from uvicorn to hypercorn (better startup signaling via lifespan)
- [x] Link-only change detection test added
- [x] WebSocket subscription test added (using `gql` client library)
- [x] debug_receive timeout and duplicate waiter tests added
- [x] Non-JSON CBOR data error handling test added
- [x] All 70 tests pass (49 existing + 21 new GraphQL tests)

### Notes
- HTTP server uses hypercorn with a lifespan handler that signals when server is ready
- GraphQL endpoint is at `/graphql/` (with trailing slash; Starlette mount redirects without it)
- Tests use `/graphql/` to avoid redirect
- WebSocket subscriptions use `graphql-ws` protocol (legacy/Apollo), not `graphql-transport-ws`
- The `gql` library is used for cleaner WebSocket subscription testing
