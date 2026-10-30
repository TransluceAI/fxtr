"""HTTP server with GraphQL endpoint for thingwatcher."""

from __future__ import annotations

import base64
import contextlib
import logging
import pathlib
import subprocess
from typing import TYPE_CHECKING, Any, AsyncGenerator

import anyio
import cbor2
from ariadne import (
    EnumType,
    MutationType,
    ObjectType,
    QueryType,
    ScalarType,
    SubscriptionType,
    load_schema_from_path,
    make_executable_schema,
)
from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLTransportWSHandler
from graphql import GraphQLError, GraphQLResolveInfo
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from .api import ThingState
    from .watcher import Watcher

logger = logging.getLogger(__name__)

# GraphQL type bindings
query = QueryType()
mutation = MutationType()
subscription = SubscriptionType()
tick_type = ObjectType("Tick")
thing_type = ObjectType("Thing")
link_type = ObjectType("Link")
root_type = ObjectType("Root")
trigger_record_type = ObjectType("TriggerRecord")
change_kind = EnumType(
    "ChangeKind", {"ADDED": "added", "MODIFIED": "modified", "REMOVED": "removed"}
)

# JSON scalar - passes through Python objects directly (dicts, lists, primitives)
json_scalar = ScalarType("JSON")


@json_scalar.serializer
def serialize_json(value: Any) -> Any:
    """JSON scalar serializer - pass through as-is."""
    return value


@json_scalar.value_parser
def parse_json_value(value: Any) -> Any:
    """JSON scalar value parser - pass through as-is."""
    return value


# =============================================================================
# Helpers
# =============================================================================


def encode_key(key_bytes: bytes) -> str:
    """Encode CBOR key bytes as base64 for GraphQL."""
    return base64.b64encode(key_bytes).decode("ascii")


def decode_key(key_str: str) -> bytes:
    """Decode base64 key string back to CBOR bytes.

    Raises GraphQL error if the key is not valid base64.
    """
    try:
        return base64.b64decode(key_str, validate=True)
    except Exception as e:
        raise GraphQLError(f"Invalid base64 key: {e}")


class CborJsonError(Exception):
    """Raised when CBOR data cannot be converted to JSON."""


def _is_json_compatible(obj: Any) -> bool:
    """Check if an object is JSON-compatible (recursively)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return True
    if isinstance(obj, dict):
        return all(_is_json_compatible(k) and _is_json_compatible(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_is_json_compatible(item) for item in obj)
    return False


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
    logger.warning("CBOR data contains non-JSON-compatible types - returning None")
    return None


def decode_key_to_json(key_bytes: bytes) -> Any | None:
    """Decode CBOR key bytes to JSON if compatible, else None.

    Unlike decode_cbor_data, this never raises - it silently returns None
    for non-JSON-compatible keys since the base64 key is always available.
    """
    try:
        raw = cbor2.loads(key_bytes)
        if _is_json_compatible(raw):
            return raw
        return None
    except Exception:
        return None


def encode_key_from_json(key_json: Any) -> bytes:
    """Convert JSON value to CBOR key bytes for lookup.

    Args:
        key_json: JSON-compatible value to encode as a key.

    Returns:
        CBOR-encoded bytes with canonical=True (matches storage format).

    Raises:
        GraphQLError: If key_json is None (explicit null rejection) or
            if CBOR encoding fails.
    """
    if key_json is None:
        raise GraphQLError(
            "keyJSON cannot be null. Use 'key' (base64) or 'shortId' instead. "
            "Note: if you received null from a keyJSON output field, that key "
            "is not JSON-compatible and must be referenced by 'key' or 'shortId'."
        )
    try:
        return cbor2.dumps(key_json, canonical=True)
    except Exception as e:
        raise GraphQLError(f"Failed to encode keyJSON as CBOR: {e}")


# Sentinel for "parameter not provided" vs "parameter is None"
_NOT_PROVIDED = object()


async def resolve_key_bytes_from_params(
    watcher: "Watcher",
    conn: "AsyncConnection",
    shortId: int | None,
    key: str | None,
    keyJSON: Any,
) -> bytes | None:
    """Resolve key_bytes from one of three mutually exclusive parameters.

    Args:
        watcher: The Watcher instance (for short ID resolution).
        conn: Database connection.
        shortId: Integer short ID, or None.
        key: Base64-encoded key string, or None.
        keyJSON: JSON value to encode as key, or _NOT_PROVIDED if not given.

    Returns:
        The resolved key_bytes, or None if shortId was provided but not found.

    Raises:
        GraphQLError: If validation fails (wrong number of params, invalid key, etc.)
    """
    # Count how many are provided
    provided = []
    if shortId is not None:
        provided.append("shortId")
    if key is not None:
        provided.append("key")
    if keyJSON is not _NOT_PROVIDED:
        provided.append("keyJSON")

    if len(provided) != 1:
        raise GraphQLError(
            f"Provide exactly one of 'shortId', 'key', or 'keyJSON'. "
            f"Got: {', '.join(provided) if provided else 'none'}"
        )

    if shortId is not None:
        key_bytes = await watcher._db.resolve_short_id(conn, shortId)
        return key_bytes  # May be None if not found
    elif key is not None:
        return decode_key(key)
    else:
        return encode_key_from_json(keyJSON)


async def _get_data_chunks_chain(
    watcher: "Watcher", conn: "AsyncConnection", data_chunk_id: int
) -> list[tuple[int, Any]]:
    """Follow extends chain in data_chunks, return list of (chunk_id, data) tuples, oldest first.

    Uses decode_cbor_data to ensure JSON compatibility. Raises GraphQL error if
    any chunk contains non-JSON-compatible data.
    """
    chunks = []
    current_id: int | None = data_chunk_id
    while current_id is not None:
        chunk = await watcher._db.get_data_chunk_by_id(conn, current_id)
        if chunk is None:
            break
        try:
            data = decode_cbor_data(chunk.data, raise_on_incompatible=True)
        except CborJsonError as e:
            raise GraphQLError(f"Data chunk {current_id} contains non-JSON-compatible CBOR: {e}")
        chunks.append((chunk.id, data))
        current_id = chunk.extends
    chunks.reverse()  # Oldest first
    return chunks


def _get_first_chunk_data(chunks: list[tuple[int, Any]]) -> Any:
    """Get data from the first chunk (oldest in the chain).

    Returns just the first chunk's data. If apps need custom merging
    logic across multiple chunks, they should use dataChunks instead.
    """
    if not chunks:
        return None
    return chunks[0][1]


async def resolve_thing_with_sources(
    watcher: "Watcher",
    conn: "AsyncConnection",
    key_bytes: bytes,
    tick: int,
) -> dict[str, Any] | None:
    """Resolve a thing by key, checking DB first then immutable stores.

    This unified helper is used by thing queries, Link.toThing, Link.fromThing,
    and other resolvers that need to resolve things by key.

    Args:
        watcher: The Watcher instance
        conn: Active database connection
        key_bytes: The CBOR-encoded thing key
        tick: The tick to query at

    Returns:
        Thing dict with _sources field, or None if not found anywhere.
        The _sources field is a list of dicts with:
        - kind: "SNAPSHOT" or "STORE"
        - storeName: store name (only for STORE kind)
        - overridden: whether this source differs from resolved state
    """
    # Decode key for store lookups
    key = cbor2.loads(key_bytes)

    # Check DB first
    db_thing = await watcher._db.get_thing_data(conn, tick, key_bytes)

    # Query all immutable stores
    store_results = await watcher.lookup_in_stores(key)

    # Build sources list
    sources: list[dict[str, Any]] = []

    if db_thing is not None:
        # Snapshot wins - it provides the resolved state
        sources.append({"kind": "SNAPSHOT", "storeName": None, "overridden": False})

        # All store sources are marked as overridden when snapshot exists
        # (snapshot is authoritative regardless of whether states match)
        for store_name, _store_state in store_results:
            sources.append(
                {
                    "kind": "STORE",
                    "storeName": store_name,
                    "overridden": True,
                }
            )

        short_id = await watcher._db.get_short_id(conn, key_bytes) or 0
        return {
            "shortId": short_id,
            "key": encode_key(db_thing.thing_key),
            "thingType": db_thing.thing_type,
            "thingstateId": f"db:{db_thing.id}",
            "_key_bytes": key_bytes,
            "_tick": tick,
            "_watcher": watcher,
            "_sources": sources,
            "_from_store": False,
        }

    if not store_results:
        return None

    # No snapshot - verify all store results are identical
    first_name, first_state = store_results[0]

    for store_name, store_state in store_results[1:]:
        if not _store_states_equal(first_state, store_state):
            # Conflict - create broken thing
            logger.warning(
                f"Store conflict for key {key!r}: inconsistent states from "
                f"{first_name} and {store_name}"
            )
            short_id = await watcher._db.get_short_id(conn, key_bytes) or 0
            ext_key = encode_key(key_bytes)
            return {
                "shortId": short_id,
                "key": ext_key,
                "thingType": "__broken__",
                "thingstateId": f"ext:{ext_key}",
                "_key_bytes": key_bytes,
                "_tick": tick,
                "_watcher": watcher,
                "_sources": [{"kind": "STORE", "storeName": first_name, "overridden": False}],
                "_from_store": True,
                "_store_state": first_state,  # Even though broken, keep for data access
                "_error": f"Store conflict: inconsistent states from {first_name} and {store_name}",
            }

    # All stores agree - return thing from first store
    for store_name, _ in store_results:
        sources.append({"kind": "STORE", "storeName": store_name, "overridden": False})

    short_id = await watcher._db.get_short_id(conn, key_bytes) or 0
    ext_key = encode_key(key_bytes)
    return {
        "shortId": short_id,
        "key": ext_key,
        "thingType": first_state.thing_type,
        "thingstateId": f"ext:{ext_key}",
        "_key_bytes": key_bytes,
        "_tick": tick,
        "_watcher": watcher,
        "_sources": sources,
        "_from_store": True,
        "_store_state": first_state,  # Store the state for data/links access
    }


def _store_states_equal(a: "ThingState", b: "ThingState") -> bool:
    """Check if two ThingStates from stores are equal."""
    if a.key != b.key:
        return False
    if a.thing_type != b.thing_type:
        return False
    if a.thing_data != b.thing_data:
        return False
    if len(a.links) != len(b.links):
        return False
    for la, lb in zip(a.links, b.links):
        if la.link_type != lb.link_type:
            return False
        if la.link_key != lb.link_key:
            return False
        if la.to_thing_key != lb.to_thing_key:
            return False
    return True


# =============================================================================
# Query Resolvers
# =============================================================================


@query.field("latestTick")
async def resolve_latest_tick(_: Any, info: GraphQLResolveInfo) -> dict[str, Any]:
    """Return the latest tick as a Tick object."""
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        tick_num = await watcher._db.get_latest_tick(conn)
        if tick_num is None:
            # No ticks yet - return a synthetic tick 0
            return {
                "index": 0,
                "timestamp": 0.0,
                "level": "none",
                "type": "none",
                "desc": None,
                "_watcher": watcher,
            }

        rows = await watcher._db.get_ticks(conn, tick_num, tick_num, None)
        if not rows:
            return {
                "index": tick_num,
                "timestamp": 0.0,
                "level": "none",
                "type": "none",
                "desc": None,
                "_watcher": watcher,
            }

        row = rows[0]
        return {
            "index": row.tick,
            "timestamp": row.timestamp,
            "level": row.level,
            "type": row.type,
            "desc": decode_cbor_data(row.data) if row.data else None,
            "_watcher": watcher,
        }


@query.field("tick")
async def resolve_tick(_: Any, info: GraphQLResolveInfo, index: int) -> dict[str, Any] | None:
    """Return a specific tick by index."""
    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        rows = await watcher._db.get_ticks(conn, index, index, None)
        if not rows:
            return None

        row = rows[0]
        return {
            "index": row.tick,
            "timestamp": row.timestamp,
            "level": row.level,
            "type": row.type,
            "desc": decode_cbor_data(row.data) if row.data else None,
            "_watcher": watcher,
        }


@query.field("ticks")
async def resolve_ticks(
    _: Any,
    info: GraphQLResolveInfo,
    from_: int | None = None,
    to: int | None = None,
    levels: list[str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    # Handle 'from' being a reserved word - ariadne passes it as from_
    from_tick = kwargs.get("from", from_)

    watcher: Watcher = info.context["watcher"]
    async with watcher._db.begin() as conn:
        rows = await watcher._db.get_ticks(conn, from_tick, to, levels)
        return [
            {
                "index": row.tick,
                "timestamp": row.timestamp,
                "level": row.level,
                "type": row.type,
                "desc": decode_cbor_data(row.data) if row.data else None,
                "_watcher": watcher,
            }
            for row in rows
        ]


@query.field("thing")
async def resolve_thing(
    _: Any,
    info: GraphQLResolveInfo,
    shortId: int | None = None,
    key: str | None = None,
    keyJSON: Any = _NOT_PROVIDED,
    atTick: int | None = None,
) -> dict[str, Any] | None:
    """Look up a thing by shortId, key, or keyJSON, optionally at a specific tick.

    Checks the database first, then falls back to immutable stores.
    """
    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        # Resolve to key_bytes (validates exactly-one-of)
        key_bytes = await resolve_key_bytes_from_params(watcher, conn, shortId, key, keyJSON)
        if key_bytes is None:
            return None

        # Get tick to query at
        tick = atTick
        if tick is None:
            tick = await watcher._db.get_latest_tick(conn)
            if tick is None:
                return None

        # Use unified helper that checks DB then stores
        return await resolve_thing_with_sources(watcher, conn, key_bytes, tick)


@query.field("things")
async def resolve_things(
    _: Any,
    info: GraphQLResolveInfo,
    keys: list[str],
    atTick: int,
) -> list[dict[str, Any] | None]:
    """Batch fetch things by keys. Returns array with null for keys that don't resolve."""
    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        results: list[dict[str, Any] | None] = []
        for key_b64 in keys:
            try:
                key_bytes = base64.b64decode(key_b64)
                thing = await resolve_thing_with_sources(watcher, conn, key_bytes, atTick)
                results.append(thing)
            except Exception:
                # If decoding fails or any other error, return null for this key
                results.append(None)
        return results


@query.field("thingHistory")
async def resolve_thing_history(
    _: Any,
    info: GraphQLResolveInfo,
    shortId: int | None = None,
    key: str | None = None,
    keyJSON: Any = _NOT_PROVIDED,
    from_: int | None = None,
    to: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    from_tick = kwargs.get("from", from_)

    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        # Resolve to key_bytes (validates exactly-one-of)
        key_bytes = await resolve_key_bytes_from_params(watcher, conn, shortId, key, keyJSON)
        if key_bytes is None:
            return []

        states = await watcher._db.get_thing_history(conn, key_bytes, from_tick, to)
        short_id = await watcher._db.get_short_id(conn, key_bytes) or 0

        result = []
        for state in states:
            result.append(
                {
                    "shortId": short_id,
                    "key": encode_key(state.thing_key),
                    "thingType": state.thing_type,
                    "thingstateId": f"db:{state.id}",
                    "_key_bytes": key_bytes,
                    "_tick": state.valid_from,
                    "_watcher": watcher,
                }
            )

        return result


@query.field("dataChunk")
async def resolve_data_chunk(_: Any, info: GraphQLResolveInfo, id: str) -> dict[str, Any] | None:
    """Fetch a single data chunk by its ID.

    Supports both formats:
    - "db:{numeric_id}" for chunks from the snapshot DB
    - "ext:{b64_key}" for chunks from immutable stores (returns the store thing's data)
    """
    watcher: Watcher = info.context["watcher"]

    if id.startswith("db:"):
        # Database chunk
        try:
            numeric_id = int(id[3:])
        except ValueError:
            return None

        async with watcher._db.begin() as conn:
            chunk = await watcher._db.get_data_chunk_by_id(conn, numeric_id)
            if chunk is None:
                return None

            try:
                data = decode_cbor_data(chunk.data, raise_on_incompatible=True)
            except CborJsonError as e:
                raise GraphQLError(f"Data chunk {id} contains non-JSON-compatible CBOR: {e}")

            return {
                "id": f"db:{chunk.id}",
                "data": data,
            }

    elif id.startswith("ext:"):
        # Store chunk - look up by thing key
        b64_key = id[4:]
        try:
            key_bytes = decode_key(b64_key)
            key = cbor2.loads(key_bytes)
        except Exception:
            return None

        # Query all stores for this key
        store_results = await watcher.lookup_in_stores(key)
        if not store_results:
            return None

        # Return data from first store (they should all agree for valid lookups)
        _, store_state = store_results[0]
        return {
            "id": id,
            "data": store_state.thing_data,
        }

    else:
        # Unknown format
        return None


def _renderer_mapping_to_ref(mapping: Any) -> dict[str, Any] | None:
    """Convert a RendererMapping to a RendererReference dict, or None if no mapping."""
    if mapping is None:
        return None
    return {
        "moduleId": mapping.module_id,
        "rendererKey": mapping.renderer_key,
    }


@query.field("renderingConfig")
async def resolve_rendering_config(_: Any, info: GraphQLResolveInfo) -> list[dict[str, Any]]:
    from .registry import get_all_renderer_mappings, get_all_rendering_configs

    configs = get_all_rendering_configs()
    mappings = get_all_renderer_mappings()

    # Collect all thing types that have either a config or a renderer mapping
    all_thing_types = set(configs.keys()) | set(mappings.keys())

    return [
        {
            "thingType": thing_type_name,
            "color": (configs[thing_type_name].color if thing_type_name in configs else "#888888"),
            "linkColors": [
                {"linkType": lt, "color": c}
                for lt, c in (
                    configs[thing_type_name].link_colors.items()
                    if thing_type_name in configs
                    else []
                )
            ],
            "renderer": _renderer_mapping_to_ref(mappings.get(thing_type_name)),
        }
        for thing_type_name in all_thing_types
    ]


@query.field("thingTypeConfig")
async def resolve_thing_type_config(
    _: Any, info: GraphQLResolveInfo, thingType: str
) -> dict[str, Any] | None:
    """Fetch rendering config for a single thing type."""
    from .registry import get_renderer_mapping, get_rendering_config

    config = get_rendering_config(thingType)
    mapping = get_renderer_mapping(thingType)

    # Check if this type has any config or renderer mapping
    if config is None and mapping is None:
        return None

    return {
        "thingType": thingType,
        "color": config.color if config else "#888888",
        "linkColors": [
            {"linkType": lt, "color": c} for lt, c in (config.link_colors.items() if config else [])
        ],
        "renderer": _renderer_mapping_to_ref(mapping),
    }


@query.field("rendererModule")
async def resolve_renderer_module(
    _: Any, info: GraphQLResolveInfo, id: str
) -> dict[str, Any] | None:
    """Fetch a renderer module by its content-addressed ID."""
    from .registry import get_renderer_module

    module = get_renderer_module(id)
    if module is None:
        return None

    return {
        "id": id,
        "main": module.main,
        "imports": module.imports,
    }


@query.field("triggerRecords")
async def resolve_trigger_records(
    _: Any,
    info: GraphQLResolveInfo,
    key: str | None = None,
    keyJSON: Any = _NOT_PROVIDED,
    from_: int | None = None,
    to: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Get trigger records, optionally filtered by key or keyJSON."""
    from_tick = kwargs.get("from", from_)

    watcher: Watcher = info.context["watcher"]

    # Both key and keyJSON are optional filters, but can't provide both
    if key is not None and keyJSON is not _NOT_PROVIDED:
        raise GraphQLError("Provide at most one of 'key' or 'keyJSON' for filtering")

    key_bytes: bytes | None = None
    if key is not None:
        key_bytes = decode_key(key)
    elif keyJSON is not _NOT_PROVIDED:
        key_bytes = encode_key_from_json(keyJSON)

    async with watcher._db.begin() as conn:
        rows = await watcher._db.get_trigger_records(conn, key_bytes, from_tick, to)
        return [
            {
                "id": row.id,
                "_thing_key_bytes": row.thing_key,
                "_thingstate_id": row.thingstate_id,
                "triggerTick": row.trigger_tick,
                "payload": decode_cbor_data(row.payload) if row.payload else None,
                "_watcher": watcher,
            }
            for row in rows
        ]


@query.field("triggerRecord")
async def resolve_trigger_record(
    _: Any, info: GraphQLResolveInfo, id: int
) -> dict[str, Any] | None:
    """Get a single trigger record by ID."""
    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        row = await watcher._db.get_trigger_record_by_id(conn, id)
        if row is None:
            return None
        return {
            "id": row.id,
            "_thing_key_bytes": row.thing_key,
            "_thingstate_id": row.thingstate_id,
            "triggerTick": row.trigger_tick,
            "payload": decode_cbor_data(row.payload) if row.payload else None,
            "_watcher": watcher,
        }


@query.field("root")
async def resolve_root(
    _: Any, info: GraphQLResolveInfo, name: str, atTick: int | None = None
) -> dict[str, Any] | None:
    """Get a specific root by name, optionally at a specific tick."""
    watcher: Watcher = info.context["watcher"]

    async with watcher._db.begin() as conn:
        # Get tick to query at
        tick = atTick
        if tick is None:
            tick = await watcher._db.get_latest_tick(conn)
            if tick is None:
                return None

        root_row = await watcher._db.get_root_at_tick(conn, name, tick)
        if root_row is None:
            return None

        return {
            "name": root_row.root_name,
            "_thing_key_bytes": root_row.thing_key,
            "_tick": tick,
            "_watcher": watcher,
        }


@query.field("triggerableKeys")
def resolve_triggerable_keys(_: Any, info: GraphQLResolveInfo) -> list[str]:
    """Get all currently triggerable thing keys (b64-encoded)."""
    watcher: Watcher = info.context["watcher"]
    triggerable_things = watcher.get_triggerable_things()
    return [encode_key(key_bytes) for key_bytes, _ in triggerable_things]


# =============================================================================
# Tick Field Resolvers
# =============================================================================


@tick_type.field("things")
async def resolve_tick_things(
    tick: dict[str, Any], info: GraphQLResolveInfo, triggerable: bool | None = None
) -> list[dict[str, Any]]:
    """Resolve things at this tick, optionally filtered by triggerable."""
    watcher: Watcher = tick["_watcher"]
    tick_index = tick["index"]

    async with watcher._db.begin() as conn:
        things = await watcher._db.get_things_at_tick(conn, tick_index)
        latest_tick = await watcher._db.get_latest_tick(conn)

        # Compute triggerability (only for latest tick)
        triggerable_keys: set[bytes] = set()
        if tick_index == latest_tick:
            for key_bytes, thing_type in watcher.get_triggerable_things():
                triggerable_keys.add(key_bytes)

        result = []
        for thing in things:
            is_triggerable = thing.thing_key in triggerable_keys

            # Apply filter if specified
            if triggerable is not None and is_triggerable != triggerable:
                continue

            short_id = await watcher._db.get_short_id(conn, thing.thing_key)
            result.append(
                {
                    "shortId": short_id or 0,
                    "key": encode_key(thing.thing_key),
                    "thingType": thing.thing_type,
                    "thingstateId": f"db:{thing.id}",
                    "_key_bytes": thing.thing_key,
                    "_tick": tick_index,
                    "_watcher": watcher,
                }
            )
        return result


@tick_type.field("changes")
async def resolve_tick_changes(
    tick: dict[str, Any], info: GraphQLResolveInfo, since: int
) -> list[dict[str, Any]]:
    """Resolve changes between `since` tick and this tick."""
    watcher: Watcher = tick["_watcher"]
    to_tick = tick["index"]
    from_tick = since

    async with watcher._db.begin() as conn:
        # Get things at both ticks
        things_at_from = await watcher._db.get_things_at_tick(conn, from_tick)
        things_at_to = await watcher._db.get_things_at_tick(conn, to_tick)
        links_at_from = await watcher._db.get_links_at_tick(conn, from_tick)
        links_at_to = await watcher._db.get_links_at_tick(conn, to_tick)

        # Build maps: key -> (state_id, thing_row)
        from_map = {t.thing_key: (t.id, t) for t in things_at_from}
        to_map = {t.thing_key: (t.id, t) for t in things_at_to}

        # Build link sets for comparison
        def link_set(link_rows: list[Any], key: bytes) -> set[tuple[str, bytes, bytes]]:
            return {
                (lnk.link_type, lnk.link_key, lnk.to_thing_key)
                for lnk in link_rows
                if lnk.from_thing_key == key
            }

        from_links = {k: link_set(links_at_from, k) for k in from_map}
        to_links = {k: link_set(links_at_to, k) for k in to_map}

        changes: list[dict[str, Any]] = []

        # ADDED: in to but not in from
        for key in to_map:
            if key not in from_map:
                to_id, thing = to_map[key]
                short_id = await watcher._db.get_short_id(conn, key) or 0
                changes.append(
                    {
                        "kind": "added",
                        "thing": {
                            "shortId": short_id,
                            "key": encode_key(thing.thing_key),
                            "thingType": thing.thing_type,
                            "thingstateId": f"db:{to_id}",
                            "_key_bytes": key,
                            "_tick": to_tick,
                            "_watcher": watcher,
                        },
                    }
                )

        # MODIFIED: in both but state_id changed OR links changed
        for key in to_map:
            if key in from_map:
                from_id, _ = from_map[key]
                to_id, to_thing = to_map[key]
                data_changed = from_id != to_id
                links_changed = from_links.get(key, set()) != to_links.get(key, set())
                if data_changed or links_changed:
                    short_id = await watcher._db.get_short_id(conn, key) or 0
                    changes.append(
                        {
                            "kind": "modified",
                            "thing": {
                                "shortId": short_id,
                                "key": encode_key(to_thing.thing_key),
                                "thingType": to_thing.thing_type,
                                "thingstateId": f"db:{to_id}",
                                "_key_bytes": key,
                                "_tick": to_tick,
                                "_watcher": watcher,
                            },
                        }
                    )

        # REMOVED: in from but not in to
        for key in from_map:
            if key not in to_map:
                from_id, thing = from_map[key]
                short_id = await watcher._db.get_short_id(conn, key) or 0
                changes.append(
                    {
                        "kind": "removed",
                        "thing": {
                            "shortId": short_id,
                            "key": encode_key(thing.thing_key),
                            "thingType": thing.thing_type,
                            "thingstateId": f"db:{from_id}",
                            "_key_bytes": key,
                            "_tick": from_tick,  # Use from_tick for removed things
                            "_watcher": watcher,
                        },
                    }
                )

        return changes


@tick_type.field("roots")
async def resolve_tick_roots(
    tick: dict[str, Any], info: GraphQLResolveInfo
) -> list[dict[str, Any]]:
    """Resolve roots at this tick."""
    watcher: Watcher = tick["_watcher"]
    tick_index = tick["index"]

    async with watcher._db.begin() as conn:
        roots = await watcher._db.get_roots_at_tick(conn, tick_index)
        return [
            {
                "name": root.root_name,
                "_thing_key_bytes": root.thing_key,
                "_tick": tick_index,
                "_watcher": watcher,
            }
            for root in roots
        ]


# =============================================================================
# Root Field Resolvers
# =============================================================================


@root_type.field("thing")
async def resolve_root_thing(root: dict[str, Any], info: GraphQLResolveInfo) -> dict[str, Any]:
    """Resolve the thing for this root.

    Note: We only check the DB here, not immutable stores. This is correct because:
    - Roots are created via add_root() with an AnchorCapturer
    - The capturer returns either ThingState (stored in DB) or ThingRef (a key reference)
    - If ThingRef points to a key not in any snapshot, a broken thing is created
      with that same key and stored in the DB
    - So a root's target is ALWAYS in the DB (either real thing or broken thing)
    - Store things can never be the primary source for a root
    """
    watcher: Watcher = root["_watcher"]
    key_bytes: bytes = root["_thing_key_bytes"]
    tick: int = root["_tick"]

    async with watcher._db.begin() as conn:
        thing = await watcher._db.get_thing_data(conn, tick, key_bytes)

        if thing is None:
            # Root's target should always exist in DB - this is an error
            key = encode_key(key_bytes)
            raise GraphQLError(f"Root target thing not found: {key}")

        short_id = await watcher._db.get_short_id(conn, key_bytes) or 0
        return {
            "shortId": short_id,
            "key": encode_key(thing.thing_key),
            "thingType": thing.thing_type,
            "thingstateId": f"db:{thing.id}",
            "_key_bytes": key_bytes,
            "_tick": tick,
            "_watcher": watcher,
        }


# =============================================================================
# Thing Field Resolvers
# =============================================================================


@thing_type.field("keyJSON")
def resolve_thing_key_json(thing: dict[str, Any], info: GraphQLResolveInfo) -> Any | None:
    """Resolve keyJSON - the key decoded as JSON if compatible."""
    key_bytes: bytes = thing["_key_bytes"]
    return decode_key_to_json(key_bytes)


@thing_type.field("links")
async def resolve_thing_links(
    thing: dict[str, Any], info: GraphQLResolveInfo
) -> list[dict[str, Any]]:
    """Resolve links for this thing."""
    watcher: Watcher = thing["_watcher"]
    key_bytes: bytes = thing["_key_bytes"]
    tick: int = thing["_tick"]

    # If thing is from a store, use the stored state links
    if thing.get("_from_store") and "_store_state" in thing:
        store_state = thing["_store_state"]
        return [
            {
                "linkType": link.link_type,
                "linkKey": encode_key(cbor2.dumps(link.link_key, canonical=True)),
                "_link_key_bytes": cbor2.dumps(link.link_key, canonical=True),
                "_from_thing_key_bytes": key_bytes,
                "_to_thing_key_bytes": cbor2.dumps(link.to_thing_key, canonical=True),
                "_tick": tick,
                "_watcher": watcher,
            }
            for link in store_state.links
        ]

    async with watcher._db.begin() as conn:
        links = await watcher._db.get_links_for_thing(conn, tick, key_bytes)
        return [
            {
                "linkType": lnk.link_type,
                "linkKey": encode_key(lnk.link_key),
                "_link_key_bytes": lnk.link_key,  # For linkKeyJSON resolver
                "_from_thing_key_bytes": lnk.from_thing_key,  # For fromThing resolver
                "_to_thing_key_bytes": lnk.to_thing_key,
                "_tick": tick,
                "_watcher": watcher,
            }
            for lnk in links
        ]


@thing_type.field("backlinks")
async def resolve_thing_backlinks(
    thing: dict[str, Any], info: GraphQLResolveInfo
) -> list[dict[str, Any]]:
    """Resolve backlinks (links pointing TO this thing).

    Combines backlinks from the database (captured things) and from
    immutable stores.
    """
    watcher: Watcher = thing["_watcher"]
    key_bytes: bytes = thing["_key_bytes"]
    tick: int = thing["_tick"]

    # Decode key for store lookups
    key = cbor2.loads(key_bytes)

    result: list[dict[str, Any]] = []
    # Track seen backlinks for deduplication: (from_key_bytes, link_type, link_key_bytes)
    seen: set[tuple[bytes, str, bytes]] = set()

    async with watcher._db.begin() as conn:
        # Get backlinks from database (captured things) - DB is authoritative
        db_backlinks = await watcher._db.get_backlinks_for_thing(conn, tick, key_bytes)
        for lnk in db_backlinks:
            seen.add((lnk.from_thing_key, lnk.link_type, lnk.link_key))
            result.append(
                {
                    "linkType": lnk.link_type,
                    "linkKey": encode_key(lnk.link_key),
                    "_link_key_bytes": lnk.link_key,
                    "_from_thing_key_bytes": lnk.from_thing_key,
                    "_to_thing_key_bytes": lnk.to_thing_key,
                    "_tick": tick,
                    "_watcher": watcher,
                }
            )

    # Get backlinks from immutable stores (dedupe against DB)
    store_backlinks = await watcher.get_store_backlinks(key)
    for _store_name, backlink_info in store_backlinks:
        from_key_bytes = cbor2.dumps(backlink_info.from_thing_key, canonical=True)
        link_key_bytes = cbor2.dumps(backlink_info.link_key, canonical=True)
        backlink_key = (from_key_bytes, backlink_info.link_type, link_key_bytes)
        if backlink_key in seen:
            continue  # Skip duplicate - DB version already added
        seen.add(backlink_key)
        result.append(
            {
                "linkType": backlink_info.link_type,
                "linkKey": encode_key(link_key_bytes),
                "_link_key_bytes": link_key_bytes,
                "_from_thing_key_bytes": from_key_bytes,
                "_to_thing_key_bytes": key_bytes,
                "_tick": tick,
                "_watcher": watcher,
            }
        )

    return result


@thing_type.field("dataChunks")
async def resolve_thing_data_chunks(
    thing: dict[str, Any], info: GraphQLResolveInfo
) -> list[dict[str, Any]] | None:
    """Resolve data chunks for this thing (lazy loaded)."""
    watcher: Watcher = thing["_watcher"]
    key_bytes: bytes = thing["_key_bytes"]
    tick: int = thing["_tick"]

    # If thing is from a store, return single chunk with the store data
    if thing.get("_from_store") and "_store_state" in thing:
        store_state = thing["_store_state"]
        # Store things are single-chunk, use ext:{key} as ID
        return [{"id": f"ext:{thing['key']}", "data": store_state.thing_data}]

    async with watcher._db.begin() as conn:
        thing_state = await watcher._db.get_thing_data(conn, tick, key_bytes)
        if thing_state is None:
            return None

        chunks = await _get_data_chunks_chain(watcher, conn, thing_state.data_chunk_id)
        return [{"id": f"db:{chunk_id}", "data": data} for chunk_id, data in chunks]


@thing_type.field("data")
async def resolve_thing_data(thing: dict[str, Any], info: GraphQLResolveInfo) -> Any:
    """Resolve merged data for this thing (convenience field)."""
    watcher: Watcher = thing["_watcher"]
    key_bytes: bytes = thing["_key_bytes"]
    tick: int = thing["_tick"]

    # If thing is from a store, use the stored state data
    if thing.get("_from_store") and "_store_state" in thing:
        store_state = thing["_store_state"]
        return store_state.thing_data

    async with watcher._db.begin() as conn:
        thing_state = await watcher._db.get_thing_data(conn, tick, key_bytes)
        if thing_state is None:
            return None

        chunks = await _get_data_chunks_chain(watcher, conn, thing_state.data_chunk_id)
        return _get_first_chunk_data(chunks)


@thing_type.field("sources")
def resolve_thing_sources(thing: dict[str, Any], info: GraphQLResolveInfo) -> list[dict[str, Any]]:
    """Resolve sources for this thing (where the data came from)."""
    # _sources is set by resolve_thing_with_sources
    # If not present, this thing was resolved the old way - assume SNAPSHOT
    sources = thing.get("_sources")
    if sources is None:
        return [{"kind": "SNAPSHOT", "storeName": None, "overridden": False}]
    return sources


# =============================================================================
# Link Field Resolvers
# =============================================================================


@link_type.field("linkKeyJSON")
def resolve_link_key_json(link: dict[str, Any], info: GraphQLResolveInfo) -> Any | None:
    """Resolve linkKeyJSON - the link key decoded as JSON if compatible."""
    link_key_bytes: bytes = link["_link_key_bytes"]
    return decode_key_to_json(link_key_bytes)


@link_type.field("fromThing")
async def resolve_link_from_thing(link: dict[str, Any], info: GraphQLResolveInfo) -> dict[str, Any]:
    """Resolve the source thing for this link.

    Uses unified resolution that checks DB first, then immutable stores.
    The source thing should always exist since the link was captured from it.
    """
    watcher: Watcher = link["_watcher"]
    from_key_bytes: bytes = link["_from_thing_key_bytes"]
    tick: int = link["_tick"]

    async with watcher._db.begin() as conn:
        # Use unified helper that checks DB then stores
        result = await resolve_thing_with_sources(watcher, conn, from_key_bytes, tick)

        if result is not None:
            return result

        # Source thing should always exist - this is an error
        key = encode_key(from_key_bytes)
        raise GraphQLError(f"Link source thing not found: {key}")


@link_type.field("toThing")
async def resolve_link_to_thing(
    link: dict[str, Any], info: GraphQLResolveInfo
) -> dict[str, Any] | None:
    """Resolve the target thing for this link.

    Uses unified resolution that checks DB first, then immutable stores.
    Returns null if the target thing doesn't exist (dangling link).
    """
    watcher: Watcher = link["_watcher"]
    to_key_bytes: bytes = link["_to_thing_key_bytes"]
    tick: int = link["_tick"]

    async with watcher._db.begin() as conn:
        # Use unified helper that checks DB then stores
        # Returns None if thing not found anywhere
        return await resolve_thing_with_sources(watcher, conn, to_key_bytes, tick)


# =============================================================================
# TriggerRecord Field Resolvers
# =============================================================================


@trigger_record_type.field("thing")
async def resolve_trigger_record_thing(
    record: dict[str, Any], info: GraphQLResolveInfo
) -> dict[str, Any]:
    """Resolve the thing that was triggered (at trigger time).

    The thing should always exist since the trigger was recorded for it.
    """
    watcher: Watcher = record["_watcher"]
    key_bytes: bytes = record["_thing_key_bytes"]
    trigger_tick: int = record["triggerTick"]

    async with watcher._db.begin() as conn:
        # Get thing state at trigger time
        thing = await watcher._db.get_thing_data(conn, trigger_tick, key_bytes)

        if thing is None:
            # Thing should exist - this is an error (possible data corruption)
            key = encode_key(key_bytes)
            raise GraphQLError(f"Triggered thing not found at tick {trigger_tick}: {key}")

        short_id = await watcher._db.get_short_id(conn, key_bytes) or 0
        return {
            "shortId": short_id,
            "key": encode_key(thing.thing_key),
            "thingType": thing.thing_type,
            "thingstateId": f"db:{thing.id}",
            "_key_bytes": key_bytes,
            "_tick": trigger_tick,
            "_watcher": watcher,
        }


# =============================================================================
# Mutation Resolvers
# =============================================================================


@mutation.field("triggerTick")
async def resolve_trigger_tick(
    _: Any, info: GraphQLResolveInfo, level: str = "info", desc: str = ""
) -> dict[str, Any]:
    watcher: Watcher = info.context["watcher"]
    tick_data = await watcher._async_tick(level, desc or None, "remote_client")
    return {
        "index": tick_data["tick"],
        "timestamp": tick_data["timestamp"],
        "level": tick_data["level"],
        "type": tick_data["type"],
        "desc": tick_data["data"],
        "_watcher": watcher,
    }


@mutation.field("triggerPause")
async def resolve_trigger_pause(
    _: Any,
    info: GraphQLResolveInfo,
    key: str | None = None,
    shortId: int | None = None,
    keyJSON: Any = _NOT_PROVIDED,
    payload: Any = None,
) -> dict[str, Any]:
    """Trigger a paused thing to resume."""
    watcher: Watcher = info.context["watcher"]

    # Count how many key params are provided
    provided = []
    if shortId is not None:
        provided.append("shortId")
    if key is not None:
        provided.append("key")
    if keyJSON is not _NOT_PROVIDED:
        provided.append("keyJSON")

    if len(provided) != 1:
        return {
            "ok": False,
            "triggerId": None,
            "error": f"Provide exactly one of 'shortId', 'key', or 'keyJSON'. "
            f"Got: {', '.join(provided) if provided else 'none'}",
        }

    # Resolve to key_bytes
    if shortId is not None:
        async with watcher._db.begin() as conn:
            key_bytes = await watcher._db.resolve_short_id(conn, shortId)
        if key_bytes is None:
            return {"ok": False, "triggerId": None, "error": f"Unknown shortId: {shortId}"}
    elif key is not None:
        try:
            key_bytes = decode_key(key)
        except GraphQLError as e:
            return {"ok": False, "triggerId": None, "error": str(e)}
    else:
        try:
            key_bytes = encode_key_from_json(keyJSON)
        except GraphQLError as e:
            return {"ok": False, "triggerId": None, "error": str(e)}

    result = await watcher.trigger_pause(key_bytes, payload)
    # Convert TriggerResult dataclass to dict for GraphQL
    return {
        "ok": result.ok,
        "triggerId": result.trigger_id,
        "error": result.error,
    }


@mutation.field("hardRecapture")
async def resolve_hard_recapture(_: Any, info: GraphQLResolveInfo) -> dict[str, Any]:
    """Hard recapture: reset all state and take fresh snapshot."""
    watcher: Watcher = info.context["watcher"]
    tick_data = await watcher.hard_recapture()
    return {
        "index": tick_data["tick"],
        "timestamp": tick_data["timestamp"],
        "level": tick_data["level"],
        "type": tick_data["type"],
        "desc": tick_data["data"],
        "_watcher": watcher,
    }


# =============================================================================
# Subscription Resolvers
# =============================================================================


@subscription.source("newTick")
async def new_tick_source(
    _: Any, info: GraphQLResolveInfo, includeStreamingUpdates: bool = False
) -> Any:
    """Generator that yields when new ticks occur.

    Args:
        includeStreamingUpdates: If True, also yield streaming tick updates.
            By default, only non-streaming ticks are yielded.
    """
    watcher: Watcher = info.context["watcher"]

    # Create a stream and register with watcher (watcher sends tick data directly)
    send, receive = anyio.create_memory_object_stream[dict](max_buffer_size=100)

    watcher._add_tick_stream(send)
    try:
        async for tick_data in receive:
            # Filter out streaming updates unless requested
            is_streaming = tick_data.get("is_streaming_update", False)
            if is_streaming and not includeStreamingUpdates:
                continue

            # Get current triggerable keys
            triggerable_things = watcher.get_triggerable_things()
            triggerable_keys = [encode_key(key_bytes) for key_bytes, _ in triggerable_things]

            # Convert to NewTickPayload format
            yield {
                "tick": {
                    "index": tick_data["tick"],
                    "timestamp": tick_data["timestamp"],
                    "level": tick_data["level"],
                    "type": tick_data["type"],
                    "desc": tick_data["data"],
                    "_watcher": watcher,
                },
                "triggerableKeys": triggerable_keys,
            }
    finally:
        watcher._remove_tick_stream(send)


@subscription.field("newTick")
def new_tick_resolver(
    payload: dict[str, Any], info: GraphQLResolveInfo, includeStreamingUpdates: bool = False
) -> dict[str, Any]:
    """Resolver for newTick subscription - returns the NewTickPayload as-is."""
    # includeStreamingUpdates is handled in the source, not the resolver
    return payload


# =============================================================================
# REST Routes
# =============================================================================


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


# =============================================================================
# Frontend Handler
# =============================================================================


class FrontendHandler:
    """Handles compilation and serving of the frontend.

    Supports multiple entry points:
    - main.tsx -> main.js/main.css (full app)
    - embed.tsx -> embed.js/embed.css (single-thing embed view)
    - debug-embed.tsx -> debug-embed.js/debug-embed.css (embed debug UI)
    """

    # Entry point configurations: (entry_file, js_output, css_output, html_file)
    ENTRY_POINTS = {
        "main": ("main.tsx", "main.js", "main.css", "index.html"),
        "embed": ("embed.tsx", "embed.js", "embed.css", "embed.html"),
        "debug-embed": ("debug-embed.tsx", "debug-embed.js", "debug-embed.css", "debug-embed.html"),
    }

    # Modules to generate import map shims for (via Node introspection).
    # These must match what main.tsx/embed.tsx populate in window.__THINGWATCHER_INJECTED__.
    # Shims are served at /shims/{module_name}.js and re-export from the global.
    SHIM_MODULES = [
        "react",
        "react/jsx-runtime",
        "react-dom",
        "react-dom/client",
    ]

    # Local modules to generate shims for (via generate-local-shim.mjs).
    # These are our own TypeScript files, not npm packages.
    LOCAL_SHIM_MODULES = [
        ("lib/ThingWatcherBindingsExternal.tsx", "thingwatcher-bindings"),
    ]

    def __init__(self, dev_mode: bool = False):
        self.dev_mode = dev_mode
        self.frontend_dir = pathlib.Path(__file__).parent / "frontend"

        # Compiled content storage: entry_name -> {"js": str, "css": str}
        self.compiled: dict[str, dict[str, str | None]] = {}

        # Generated shim code: module_name -> shim_code
        self.shims: dict[str, str] = {}

        if not dev_mode:
            self._compile_all()

    def _ensure_node_modules(self) -> None:
        """Install node_modules if they don't exist."""
        node_modules = self.frontend_dir / "node_modules"
        if not node_modules.exists():
            logger.info("Installing frontend dependencies...")
            subprocess.run(
                ["pnpm", "install"],
                cwd=self.frontend_dir,
                check=True,
                capture_output=True,
            )

    def _compile_entry(self, entry_name: str) -> None:
        """Compile a single entry point."""
        if entry_name not in self.ENTRY_POINTS:
            raise ValueError(f"Unknown entry point: {entry_name}")

        entry_file, js_out_name, css_out_name, _ = self.ENTRY_POINTS[entry_name]

        if not self.frontend_dir.exists():
            logger.warning(f"Frontend directory not found: {self.frontend_dir}")
            return

        entry_path = self.frontend_dir / entry_file
        if not entry_path.exists():
            logger.warning(f"{entry_file} not found: {entry_path}")
            return

        self._ensure_node_modules()

        # Compile with esbuild
        dist_dir = self.frontend_dir / "dist"
        dist_dir.mkdir(exist_ok=True)
        js_out = dist_dir / js_out_name
        css_out = dist_dir / css_out_name

        logger.info(f"Compiling frontend entry: {entry_file}...")
        result = subprocess.run(
            [
                "pnpm",
                "exec",
                "esbuild",
                entry_file,
                "--bundle",
                "--format=esm",
                f"--outfile={js_out}",
                # Externalize thingwatcher-bindings (resolved via import map for dynamic renderers)
                # Note: react/react-dom are bundled into main app, not externalized
                "--external:thingwatcher-bindings",
            ],
            cwd=self.frontend_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.error(f"esbuild compilation failed for {entry_file}: {result.stderr}")
            raise RuntimeError(f"esbuild compilation failed: {result.stderr}")

        # Read compiled files
        self.compiled[entry_name] = {
            "js": js_out.read_text() if js_out.exists() else None,
            "css": css_out.read_text() if css_out.exists() else None,
        }

        logger.info(f"Frontend entry {entry_file} compiled successfully")

    def _generate_shims(self) -> None:
        """Generate shim modules for import map by introspecting actual packages.

        This runs a Node script that dynamically imports each module and enumerates
        its exports, generating JavaScript code that re-exports from the global
        window.__THINGWATCHER_INJECTED__ object.

        The shims allow esm.sh dependencies (which externalize React) to use the
        same React instance bundled in the main app.
        """
        if not self.frontend_dir.exists():
            logger.warning(f"Frontend directory not found: {self.frontend_dir}")
            return

        script_path = self.frontend_dir / "scripts" / "generate-shim.mjs"
        if not script_path.exists():
            logger.warning(f"Shim generator script not found: {script_path}")
            return

        self._ensure_node_modules()

        logger.info("Generating import map shims...")

        # Generate shims for npm packages
        for module_name in self.SHIM_MODULES:
            try:
                result = subprocess.run(
                    ["node", str(script_path), module_name],
                    cwd=self.frontend_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.shims[module_name] = result.stdout
                logger.debug(f"Generated shim for {module_name}")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to generate shim for {module_name}: {e.stderr}")
                raise RuntimeError(f"Shim generation failed for {module_name}: {e.stderr}")

        # Generate shims for local TypeScript modules
        local_script_path = self.frontend_dir / "scripts" / "generate-local-shim.mjs"
        if local_script_path.exists():
            for file_path, module_name in self.LOCAL_SHIM_MODULES:
                try:
                    result = subprocess.run(
                        ["node", str(local_script_path), file_path, module_name],
                        cwd=self.frontend_dir,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.shims[module_name] = result.stdout
                    logger.debug(f"Generated local shim for {module_name}")
                except subprocess.CalledProcessError as e:
                    logger.error(f"Failed to generate local shim for {module_name}: {e.stderr}")
                    raise RuntimeError(
                        f"Local shim generation failed for {module_name}: {e.stderr}"
                    )

        logger.info(f"Generated {len(self.shims)} import map shims")

    def _compile_all(self) -> None:
        """Compile all entry points and generate shims."""
        for entry_name in self.ENTRY_POINTS:
            try:
                self._compile_entry(entry_name)
            except Exception as e:
                logger.warning(f"Failed to compile {entry_name}: {e}")

        try:
            self._generate_shims()
        except Exception as e:
            logger.warning(f"Failed to generate shims: {e}")

    def _get_compiled(self, entry_name: str, file_type: str) -> str | None:
        """Get compiled content, recompiling if needed in dev mode."""
        if self.dev_mode or entry_name not in self.compiled:
            self._compile_entry(entry_name)
        return self.compiled.get(entry_name, {}).get(file_type)

    async def serve_html(self, _request: Request) -> Response:
        """Serve the main index HTML page."""
        html_file = self.frontend_dir / "index.html"
        if not html_file.exists():
            return Response("Frontend not available", status_code=404, media_type="text/plain")
        return Response(html_file.read_text(), media_type="text/html")

    async def serve_js(self, _request: Request) -> Response:
        """Serve the main compiled JavaScript."""
        try:
            js = self._get_compiled("main", "js")
        except Exception as e:
            return Response(f"Compilation error: {e}", status_code=500, media_type="text/plain")

        if js is None:
            return Response("JavaScript not available", status_code=404, media_type="text/plain")
        return Response(js, media_type="application/javascript")

    async def serve_css(self, _request: Request) -> Response:
        """Serve the main compiled CSS."""
        try:
            css = self._get_compiled("main", "css")
        except Exception:
            css = None
        return Response(css or "/* No CSS */", media_type="text/css")

    def _get_shim(self, module_name: str) -> str | None:
        """Get shim code for a module, regenerating if needed in dev mode."""
        if self.dev_mode or module_name not in self.shims:
            self._generate_shims()
        return self.shims.get(module_name)

    async def serve_shim(self, request: Request) -> Response:
        """Serve a generated import map shim module.

        Routes:
        - /shims/react.js -> module_name = "react"
        - /shims/react/jsx-runtime.js -> module_name = "react/jsx-runtime"
        """
        path = request.path_params["path"]
        module_name = path.removesuffix(".js")

        try:
            shim_code = self._get_shim(module_name)
        except Exception as e:
            return Response(f"Shim generation error: {e}", status_code=500, media_type="text/plain")

        if shim_code is None:
            return Response(
                f"Unknown shim: {module_name}", status_code=404, media_type="text/plain"
            )

        return Response(shim_code, media_type="application/javascript")

    # Embed routes
    async def serve_embed_html(self, _request: Request) -> Response:
        """Serve the embed HTML page."""
        html_file = self.frontend_dir / "embed.html"
        if not html_file.exists():
            return Response("Embed view not available", status_code=404, media_type="text/plain")
        return Response(html_file.read_text(), media_type="text/html")

    async def serve_embed_js(self, _request: Request) -> Response:
        """Serve the embed compiled JavaScript."""
        try:
            js = self._get_compiled("embed", "js")
        except Exception as e:
            return Response(f"Compilation error: {e}", status_code=500, media_type="text/plain")

        if js is None:
            return Response("JavaScript not available", status_code=404, media_type="text/plain")
        return Response(js, media_type="application/javascript")

    async def serve_embed_css(self, _request: Request) -> Response:
        """Serve the embed compiled CSS."""
        try:
            css = self._get_compiled("embed", "css")
        except Exception:
            css = None
        return Response(css or "/* No CSS */", media_type="text/css")

    # Debug embed routes
    async def serve_debug_embed_html(self, _request: Request) -> Response:
        """Serve the debug embed HTML page."""
        html_file = self.frontend_dir / "debug-embed.html"
        if not html_file.exists():
            return Response(
                "Debug embed view not available", status_code=404, media_type="text/plain"
            )
        return Response(html_file.read_text(), media_type="text/html")

    async def serve_debug_embed_js(self, _request: Request) -> Response:
        """Serve the debug embed compiled JavaScript."""
        try:
            js = self._get_compiled("debug-embed", "js")
        except Exception as e:
            return Response(f"Compilation error: {e}", status_code=500, media_type="text/plain")

        if js is None:
            return Response("JavaScript not available", status_code=404, media_type="text/plain")
        return Response(js, media_type="application/javascript")

    async def serve_debug_embed_css(self, _request: Request) -> Response:
        """Serve the debug embed compiled CSS."""
        try:
            css = self._get_compiled("debug-embed", "css")
        except Exception:
            css = None
        return Response(css or "/* No CSS */", media_type="text/css")


# =============================================================================
# App Factory
# =============================================================================


def create_app(
    watcher: "Watcher",
    on_startup: anyio.Event | None = None,
    dev_mode: bool = False,
) -> Starlette:
    """Create the Starlette application with GraphQL endpoint and frontend.

    Args:
        watcher: The Watcher instance to serve.
        on_startup: Optional event that will be set when the server is ready.
        dev_mode: If True, recompile frontend on each request.
    """
    schema_path = pathlib.Path(__file__).parent / "schema.graphql"
    type_defs = load_schema_from_path(str(schema_path))

    schema = make_executable_schema(
        type_defs,
        query,
        mutation,
        subscription,
        tick_type,
        thing_type,
        link_type,
        root_type,
        trigger_record_type,
        change_kind,
        json_scalar,
    )

    graphql_app = GraphQL(
        schema,
        debug=True,
        execute_get_queries=True,
        context_value=lambda request, data: {"watcher": watcher, "request": request},
        websocket_handler=GraphQLTransportWSHandler(),
    )

    # Create frontend handler
    frontend = FrontendHandler(dev_mode=dev_mode)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None, None]:
        """Lifespan handler that signals when server is ready."""
        if on_startup is not None:
            on_startup.set()
        yield

    # Routes - frontend and debug trigger
    routes = [
        # Main app routes
        Route("/", frontend.serve_html, methods=["GET"]),
        Route("/main.js", frontend.serve_js, methods=["GET"]),
        Route("/main.css", frontend.serve_css, methods=["GET"]),
        # Import map shims for React, thingwatcher-bindings, etc.
        Route("/shims/{path:path}", frontend.serve_shim, methods=["GET"]),
        # Embed routes (single-thing view for VS Code webviews)
        Route("/embed", frontend.serve_embed_html, methods=["GET"]),
        Route("/embed.js", frontend.serve_embed_js, methods=["GET"]),
        Route("/embed.css", frontend.serve_embed_css, methods=["GET"]),
        # Debug embed routes (testing UI for embed view)
        Route("/debug-embed", frontend.serve_debug_embed_html, methods=["GET"]),
        Route("/debug-embed.js", frontend.serve_debug_embed_js, methods=["GET"]),
        Route("/debug-embed.css", frontend.serve_debug_embed_css, methods=["GET"]),
        # Debug trigger endpoint
        Route("/debug_trigger/{name}", handle_debug_trigger, methods=["POST"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.watcher = watcher

    # Mount GraphQL app (handles both HTTP POST and WebSocket subscriptions)
    app.mount("/graphql", graphql_app)

    return app
