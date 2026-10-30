"""GraphQL server for early prototyping and proof of concept."""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ariadne import (
    ObjectType,
    QueryType,
    ScalarType,
    SubscriptionType,
    UnionType,
    load_schema_from_path,
    make_executable_schema,
)
from ariadne.asgi import GraphQL

# Load the schema SDL
type_defs = load_schema_from_path(pathlib.Path(__file__).parent / "spike_schema.graphql")


# Data classes for the underlying data (no IDs - these are pure data)
@dataclass
class Foo:
    id: str
    data: str


@dataclass
class FooSingle:
    value: Foo


@dataclass
class FooList:
    items: list  # list of FooContainer


@dataclass
class FooDict:
    entries: dict[str, Any]  # str -> FooContainer


@dataclass
class FooRef:
    target_id: str  # RootID of the referenced root


@dataclass
class FooStreamRef:
    target_id: str  # StreamID of the referenced stream


# Type alias for all FooData variants
# Note: FooStreamRef can only appear in stream content, not root content (enforced at data layer)
FooData = FooSingle | FooList | FooDict | FooRef | FooStreamRef


@dataclass
class Root:
    id: str
    content: FooData


@dataclass
class Stream:
    id: str
    key: FooRef | None  # Optional reference to a root that identifies this stream
    items: list[FooData] | None  # None after finalization
    final_result: FooRef | None = None  # Set when finalized

    @property
    def is_finalized(self) -> bool:
        return self.items is None

    @property
    def item_count(self) -> int | None:
        return len(self.items) if self.items is not None else None


# ============================================================================
# Registry event types (for the event log)
# ============================================================================


@dataclass
class RegistryEvent:
    """Base class for registry events."""

    generation: int  # Generation AFTER this event was applied


@dataclass
class RootAddedEvent(RegistryEvent):
    root_id: str


@dataclass
class RootRemovedEvent(RegistryEvent):
    root_id: str


@dataclass
class StreamAddedEvent(RegistryEvent):
    stream_id: str


@dataclass
class StreamRemovedEvent(RegistryEvent):
    stream_id: str


RegistryEventType = RootAddedEvent | RootRemovedEvent | StreamAddedEvent | StreamRemovedEvent


# Wrapper that carries path context - represents FooNode in GraphQL
# Also used for FooData (the projection is identity in Python)
@dataclass
class NodeWithContext:
    """Wraps a node with its path context for ID generation."""

    node: FooData
    container: tuple[str, str]  # ("root", id) or ("stream", id)
    path: list[str | int]

    @property
    def node_id(self) -> str:
        """Compute the JSON-encoded node ID (compact, no spaces).

        Format: [["root"|"stream", id], ...path]
        For roots: [["root","dict"]], [["root","dict"],"beta"]
        For streams: [["stream","mystream"],0], [["stream","mystream"],0,"key"]
        """
        return json.dumps([list(self.container), *self.path], separators=(",", ":"))


# Sample data - a nested structure to explore
ROOTS: dict[str, Root] = {
    "simple": Root(
        id="simple",
        content=FooSingle(value=Foo(id="1", data="A simple foo")),
    ),
    "list": Root(
        id="list",
        content=FooList(
            items=[
                FooSingle(value=Foo(id="2", data="First in list")),
                FooSingle(value=Foo(id="3", data="Second in list")),
                FooList(
                    items=[
                        FooSingle(value=Foo(id="4", data="Nested list item")),
                    ]
                ),
            ]
        ),
    ),
    "dict": Root(
        id="dict",
        content=FooDict(
            entries={
                "alpha": FooSingle(value=Foo(id="5", data="Alpha foo")),
                "beta": FooList(
                    items=[
                        FooSingle(value=Foo(id="6", data="Beta item 1")),
                        FooSingle(value=Foo(id="7", data="Beta item 2")),
                    ]
                ),
                "nested": FooDict(
                    entries={
                        "deep": FooSingle(value=Foo(id="8", data="Deep nested foo")),
                    }
                ),
            }
        ),
    ),
    "with_ref": Root(
        id="with_ref",
        content=FooDict(
            entries={
                "local": FooSingle(value=Foo(id="9", data="Local data in with_ref")),
                "ref_to_simple": FooRef(target_id="simple"),
                "ref_to_list": FooRef(target_id="list"),
            }
        ),
    ),
}

# Sample streams
STREAMS: dict[str, Stream] = {
    "example_stream": Stream(
        id="example_stream",
        key=FooRef(target_id="simple"),  # This stream is about "simple"
        items=[
            FooSingle(value=Foo(id="s1", data="Stream item 1")),
            FooSingle(value=Foo(id="s2", data="Stream item 2")),
            FooDict(
                entries={
                    "nested": FooSingle(value=Foo(id="s3", data="Nested in stream")),
                }
            ),
        ],
    ),
    "stream_with_refs": Stream(
        id="stream_with_refs",
        key=FooRef(target_id="dict"),
        items=[
            FooRef(target_id="simple"),  # Root ref in stream
            FooStreamRef(target_id="example_stream"),  # Stream ref in stream
        ],
    ),
    "finalized_stream": Stream(
        id="finalized_stream",
        key=FooRef(target_id="list"),
        items=None,  # Finalized - items no longer accessible
        final_result=FooRef(target_id="simple"),
    ),
}

# ============================================================================
# Registry state
# ============================================================================

# Current generation number (incremented on every add/remove)
registry_generation: int = 0

# IDs visible in registry (subset of ROOTS/STREAMS keys)
registry_roots: set[str] = set()
registry_streams: set[str] = set()

# Event log - stores all events with their generation number for catch-up
registry_event_log: list[RegistryEventType] = []

# Pub/sub for registry updates (live subscribers)
registry_subscribers: list[asyncio.Queue] = []


def _init_registry() -> None:
    """Initialize registry with existing sample data."""
    # Add all existing roots to registry (no events logged - this is initial state)
    for root_id in ROOTS:
        registry_roots.add(root_id)
    # Add all existing streams to registry
    for stream_id in STREAMS:
        registry_streams.add(stream_id)
    # Generation stays 0 - no events have occurred yet


_init_registry()


# Helper: Parse JSON-encoded node ID into (container, path)
def parse_node_id(node_id: str) -> tuple[tuple[str, str], list[str | int]]:
    """Parse a JSON-encoded node ID.

    Format: [["root"|"stream", id], ...path]
    Returns: ((container_type, container_id), path)
    """
    parts = json.loads(node_id)
    if not parts or not isinstance(parts, list):
        raise ValueError(f"Invalid node ID: {node_id}")
    # First element should be ["root", id] or ["stream", id]
    container_marker = parts[0]
    if not isinstance(container_marker, list) or len(container_marker) != 2:
        raise ValueError(
            f"Invalid node ID format - expected [type, id] as first element: {node_id}"
        )
    container_type, container_id = container_marker
    if container_type not in ("root", "stream"):
        raise ValueError(f"Invalid container type '{container_type}' in node ID: {node_id}")
    path = parts[1:]
    return (container_type, container_id), path


# Helper: Walk a path within a FooData tree
def _walk_path(start: FooData, path: list[str | int]) -> FooData | None:
    """Walk a path within a FooData tree. Returns None if path is invalid."""
    current = start
    for segment in path:
        if isinstance(current, FooList) and isinstance(segment, int):
            if 0 <= segment < len(current.items):
                current = current.items[segment]
            else:
                return None
        elif isinstance(current, FooDict) and isinstance(segment, str):
            if segment in current.entries:
                current = current.entries[segment]
            else:
                return None
        else:
            # FooSingle, FooRef, FooStreamRef are leaf nodes - can't navigate into them
            return None
    return current


# Helper: Get a node within a root
def get_root_node_by_path(root_id: str, path: list[str | int]) -> NodeWithContext | None:
    """Get a node by root ID and path. Path [] means root's content."""
    root = ROOTS.get(root_id)
    if not root:
        return None

    result = _walk_path(root.content, path) if path else root.content
    if result is None:
        return None

    return NodeWithContext(node=result, container=("root", root_id), path=path)


# Helper: Get a node within a stream
def get_stream_node_by_path(stream_id: str, path: list[str | int]) -> NodeWithContext | None:
    """Get a node by stream ID and path.

    For streams, path must start with an item index.
    Path [0] means first item, [0, "key"] means key within first item.
    """
    stream = STREAMS.get(stream_id)
    if not stream or stream.items is None:
        return None

    if not path:
        # Empty path not valid for streams - need at least item index
        return None

    item_index = path[0]
    if not isinstance(item_index, int):
        return None
    if item_index < 0 or item_index >= len(stream.items):
        return None

    item = stream.items[item_index]
    remaining_path = path[1:]

    if remaining_path:
        result = _walk_path(item, remaining_path)
        if result is None:
            return None
    else:
        result = item

    return NodeWithContext(node=result, container=("stream", stream_id), path=path)


# Helper: Get a node by container and path (unified)
def get_node(container: tuple[str, str], path: list[str | int]) -> NodeWithContext | None:
    """Get a node by container (root or stream) and path."""
    container_type, container_id = container
    if container_type == "root":
        return get_root_node_by_path(container_id, path)
    elif container_type == "stream":
        return get_stream_node_by_path(container_id, path)
    else:
        return None


# RootID scalar - simple string identifier (pass-through)
root_id_scalar = ScalarType("RootID")

# StreamID scalar - simple string identifier (pass-through)
stream_id_scalar = ScalarType("StreamID")

# NodeID scalar - JSON-encoded [[type, id], ...path] (pass-through, just a string)
node_id_scalar = ScalarType("NodeID")

# JSONPath scalar - serializes/deserializes JSON arrays
json_path_scalar = ScalarType("JSONPath")


@json_path_scalar.serializer
def serialize_json_path(value: list) -> str:
    return json.dumps(value)


@json_path_scalar.value_parser
def parse_json_path_value(value: str) -> list:
    return json.loads(value)


@json_path_scalar.literal_parser
def parse_json_path_literal(ast, _variables=None):
    from graphql import StringValueNode

    if isinstance(ast, StringValueNode):
        return json.loads(ast.value)
    return None


# Query resolvers
query = QueryType()


@query.field("registry")
def resolve_registry(*_):
    """Return the registry with current generation and visible roots/streams."""
    return {
        "generation": registry_generation,
        "roots": [ROOTS[rid] for rid in registry_roots if rid in ROOTS],
        "streams": [STREAMS[sid] for sid in registry_streams if sid in STREAMS],
    }


@query.field("root")
def resolve_root(*_, id: str):
    return ROOTS.get(id)


@query.field("stream")
def resolve_stream(*_, id: str):
    return STREAMS.get(id)


@query.field("node")
def resolve_node(*_, id: str):
    """Resolve a node by its JSON-encoded ID (works for both root and stream nodes)."""
    try:
        container, path = parse_node_id(id)
    except (json.JSONDecodeError, ValueError):
        return None
    return get_node(container, path)


@query.field("nodeByPath")
def resolve_node_by_path(*_, rootId: str, path: list):
    """Resolve a node by explicit root ID and path."""
    return get_root_node_by_path(rootId, path)


# Root type resolvers
root_type = ObjectType("Root")


@root_type.field("id")
def resolve_root_id(root: Root, *_):
    return root.id


@root_type.field("content")
def resolve_root_content(root: Root, *_):
    return NodeWithContext(
        node=root.content,
        container=("root", root.id),
        path=[],
    )


# Registry type resolvers
registry_type = ObjectType("Registry")


@registry_type.field("generation")
def resolve_registry_generation(registry: dict, *_):
    return registry["generation"]


@registry_type.field("roots")
def resolve_registry_roots(registry: dict, *_):
    return registry["roots"]


@registry_type.field("streams")
def resolve_registry_streams(registry: dict, *_):
    return registry["streams"]


# FooNode type resolvers
foo_node_type = ObjectType("FooNode")


@foo_node_type.field("id")
def resolve_foo_node_id(ctx: NodeWithContext, *_):
    return ctx.node_id


@foo_node_type.field("data")
def resolve_foo_node_data(ctx: NodeWithContext, *_):
    # Identity projection - the ctx itself represents the data too
    return ctx


# FooData union type resolver
foo_data_union = UnionType("FooData")


@foo_data_union.type_resolver
def resolve_foo_data_type(ctx: NodeWithContext, *_):
    node = ctx.node
    if isinstance(node, FooSingle):
        return "FooSingle"
    if isinstance(node, FooList):
        return "FooList"
    if isinstance(node, FooDict):
        return "FooDict"
    if isinstance(node, FooRef):
        return "FooRef"
    if isinstance(node, FooStreamRef):
        return "FooStreamRef"
    return None


# FooSingle type resolvers
foo_single_type = ObjectType("FooSingle")


@foo_single_type.field("value")
def resolve_foo_single_value(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooSingle)
    return node.value


# FooList type resolvers
foo_list_type = ObjectType("FooList")


@foo_list_type.field("items")
def resolve_foo_list_items(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooList)
    return [
        NodeWithContext(
            node=item,
            container=ctx.container,
            path=[*ctx.path, i],
        )
        for i, item in enumerate(node.items)
    ]


# FooDict type resolvers
foo_dict_type = ObjectType("FooDict")


@foo_dict_type.field("entries")
def resolve_foo_dict_entries(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooDict)
    return [
        {
            "key": key,
            "value": NodeWithContext(
                node=value,
                container=ctx.container,
                path=[*ctx.path, key],
            ),
        }
        for key, value in node.entries.items()
    ]


# FooRef type resolvers
foo_ref_type = ObjectType("FooRef")


@foo_ref_type.field("targetId")
def resolve_foo_ref_target_id(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooRef)
    return node.target_id


@foo_ref_type.field("load")
def resolve_foo_ref_load(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooRef)
    return ROOTS.get(node.target_id)


# FooStreamRef type resolvers
foo_stream_ref_type = ObjectType("FooStreamRef")


@foo_stream_ref_type.field("targetId")
def resolve_foo_stream_ref_target_id(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooStreamRef)
    return node.target_id


@foo_stream_ref_type.field("load")
def resolve_foo_stream_ref_load(ctx: NodeWithContext, *_):
    node = ctx.node
    assert isinstance(node, FooStreamRef)
    return STREAMS.get(node.target_id)


# Stream type resolvers
stream_type = ObjectType("Stream")


@stream_type.field("id")
def resolve_stream_id(stream: Stream, *_):
    return stream.id


@stream_type.field("key")
def resolve_stream_key(stream: Stream, *_):
    # key is a FooRef, but we need to wrap it to make load() work
    # We create a "virtual" NodeWithContext for the FooRef
    if stream.key is None:
        return None
    # Return the FooRef directly - the FooRef resolvers will handle it
    # But FooRef resolvers expect NodeWithContext, so we wrap it
    return NodeWithContext(
        node=stream.key,
        container=("stream", stream.id),
        path=["__key__"],  # Virtual path for the key ref
    )


@stream_type.field("items")
def resolve_stream_items(stream: Stream, *_):
    if stream.items is None:
        return None
    return [
        NodeWithContext(
            node=item,
            container=("stream", stream.id),
            path=[i],
        )
        for i, item in enumerate(stream.items)
    ]


@stream_type.field("itemCount")
def resolve_stream_item_count(stream: Stream, *_):
    return stream.item_count


@stream_type.field("finalResult")
def resolve_stream_final_result(stream: Stream, *_):
    if stream.final_result is None:
        return None
    # Same pattern as key - wrap in NodeWithContext for FooRef resolvers
    return NodeWithContext(
        node=stream.final_result,
        container=("stream", stream.id),
        path=["__finalResult__"],  # Virtual path for the final result ref
    )


@stream_type.field("isFinalized")
def resolve_stream_is_finalized(stream: Stream, *_):
    return stream.is_finalized


# ============================================================================
# Registry operations
# ============================================================================


async def _log_and_notify_registry(event: RegistryEventType) -> None:
    """Log an event and notify all live subscribers."""
    registry_event_log.append(event)
    for queue in registry_subscribers:
        await queue.put(event)


async def add_root(root_id: str, content: FooData) -> Root:
    """Create a root and add it to the registry."""
    global registry_generation
    root = Root(id=root_id, content=content)
    ROOTS[root_id] = root
    registry_roots.add(root_id)
    registry_generation += 1
    await _log_and_notify_registry(RootAddedEvent(generation=registry_generation, root_id=root_id))
    return root


async def remove_root(root_id: str) -> bool:
    """Remove a root from the registry (data remains for existing refs)."""
    global registry_generation
    if root_id not in registry_roots:
        return False
    registry_roots.discard(root_id)
    registry_generation += 1
    await _log_and_notify_registry(
        RootRemovedEvent(generation=registry_generation, root_id=root_id)
    )
    return True


async def add_stream(stream_id: str, key_root_id: str | None = None) -> Stream:
    """Create a stream and add it to the registry."""
    global registry_generation
    stream = Stream(
        id=stream_id,
        key=FooRef(target_id=key_root_id) if key_root_id else None,
        items=[],
    )
    STREAMS[stream_id] = stream
    registry_streams.add(stream_id)
    registry_generation += 1
    await _log_and_notify_registry(
        StreamAddedEvent(generation=registry_generation, stream_id=stream_id)
    )
    return stream


async def remove_stream(stream_id: str) -> bool:
    """Remove a stream from the registry (data remains, existing subscriptions continue)."""
    global registry_generation
    if stream_id not in registry_streams:
        return False
    registry_streams.discard(stream_id)
    registry_generation += 1
    await _log_and_notify_registry(
        StreamRemovedEvent(generation=registry_generation, stream_id=stream_id)
    )
    return True


# ============================================================================
# Stream subscription infrastructure
# ============================================================================

# Pub/sub for stream updates: stream_id -> list of asyncio.Queue
stream_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


async def emit_stream_item(stream_id: str, item: FooData) -> int:
    """Emit a new item to a stream and notify subscribers.

    Returns the index of the new item.
    """
    stream = STREAMS.get(stream_id)
    if stream is None:
        raise ValueError(f"Stream not found: {stream_id}")
    if stream.items is None:
        raise ValueError(f"Stream is finalized: {stream_id}")

    stream.items.append(item)
    index = len(stream.items) - 1

    # Notify all subscribers
    event = {"type": "item", "stream_id": stream_id, "index": index, "item": item}
    for queue in stream_subscribers[stream_id]:
        await queue.put(event)

    return index


async def finalize_stream(stream_id: str, final_result_root_id: str) -> None:
    """Finalize a stream and notify subscribers.

    Args:
        stream_id: The stream to finalize.
        final_result_root_id: Required - the root ID that represents the final result.
    """
    stream = STREAMS.get(stream_id)
    if stream is None:
        raise ValueError(f"Stream not found: {stream_id}")
    if stream.items is None:
        raise ValueError(f"Stream is already finalized: {stream_id}")

    # Set final result (required)
    stream.final_result = FooRef(target_id=final_result_root_id)

    # Clear items (marks as finalized)
    stream.items = None

    # Notify all subscribers
    event = {
        "type": "finalized",
        "stream_id": stream_id,
        "final_result_root_id": final_result_root_id,
    }
    for queue in stream_subscribers[stream_id]:
        await queue.put(event)


# Subscription resolvers
subscription = SubscriptionType()


@subscription.source("streamUpdates")
async def stream_updates_source(_, info, id: str, afterIndex: int):
    """Generator that yields stream update events."""
    queue: asyncio.Queue = asyncio.Queue()
    stream_subscribers[id].append(queue)

    try:
        stream = STREAMS.get(id)
        if stream is None:
            return  # Stream not found, end subscription

        # Send existing items after afterIndex (catch-up)
        if stream.items is not None:
            for i in range(afterIndex + 1, len(stream.items)):
                item = stream.items[i]
                yield {
                    "type": "item",
                    "stream_id": id,
                    "index": i,
                    "item": item,
                }

        # If already finalized, send that event and return
        if stream.is_finalized:
            final_root_id = stream.final_result.target_id if stream.final_result else None
            yield {
                "type": "finalized",
                "stream_id": id,
                "final_result_root_id": final_root_id,
            }
            return

        # Stream live events
        while True:
            event = await queue.get()
            yield event
            if event["type"] == "finalized":
                return  # End subscription after finalization

    finally:
        stream_subscribers[id].remove(queue)


@subscription.field("streamUpdates")
def stream_updates_resolver(event, info, id: str, afterIndex: int):
    """Transform internal event to GraphQL type."""
    if event["type"] == "item":
        # Wrap the item in NodeWithContext for proper resolution
        ctx = NodeWithContext(
            node=event["item"],
            container=("stream", event["stream_id"]),
            path=[event["index"]],
        )
        return {
            "__typename": "StreamItemEmitted",
            "streamId": event["stream_id"],
            "index": event["index"],
            "node": ctx,
        }
    elif event["type"] == "finalized":
        return {
            "__typename": "StreamFinalized",
            "streamId": event["stream_id"],
            "finalResultTargetId": event["final_result_root_id"],
        }
    return None


# Union type resolver for StreamUpdateEvent
stream_update_event_union = UnionType("StreamUpdateEvent")


@stream_update_event_union.type_resolver
def resolve_stream_update_event_type(obj, *_):
    return obj.get("__typename")


# Object types for subscription events
stream_item_emitted_type = ObjectType("StreamItemEmitted")
stream_finalized_type = ObjectType("StreamFinalized")


@stream_item_emitted_type.field("node")
def resolve_stream_item_emitted_node(obj, *_):
    return obj["node"]


# ============================================================================
# Registry subscription
# ============================================================================


@subscription.source("registryUpdates")
async def registry_updates_source(_, info, afterGeneration: int):
    """Generator that yields registry update events."""
    queue: asyncio.Queue = asyncio.Queue()
    registry_subscribers.append(queue)

    try:
        # Catch-up: replay events from the log where generation > afterGeneration
        for event in registry_event_log:
            if event.generation > afterGeneration:
                yield event

        # Stream live events
        while True:
            event = await queue.get()
            yield event

    finally:
        registry_subscribers.remove(queue)


@subscription.field("registryUpdates")
def registry_updates_resolver(event: RegistryEventType, info, afterGeneration: int):
    """Transform internal event to GraphQL type."""
    if isinstance(event, RootAddedEvent):
        root = ROOTS.get(event.root_id)
        return {
            "__typename": "RootAdded",
            "generation": event.generation,
            "root": root,
        }
    elif isinstance(event, RootRemovedEvent):
        return {
            "__typename": "RootRemoved",
            "generation": event.generation,
            "rootId": event.root_id,
        }
    elif isinstance(event, StreamAddedEvent):
        stream = STREAMS.get(event.stream_id)
        return {
            "__typename": "StreamAdded",
            "generation": event.generation,
            "stream": stream,
        }
    elif isinstance(event, StreamRemovedEvent):
        return {
            "__typename": "StreamRemoved",
            "generation": event.generation,
            "streamId": event.stream_id,
        }
    return None


# Union type resolver for RegistryUpdateEvent
registry_update_event_union = UnionType("RegistryUpdateEvent")


@registry_update_event_union.type_resolver
def resolve_registry_update_event_type(obj, *_):
    return obj.get("__typename")


# Object types for registry subscription events
root_added_type = ObjectType("RootAdded")
root_removed_type = ObjectType("RootRemoved")
stream_added_type = ObjectType("StreamAdded")
stream_removed_type = ObjectType("StreamRemoved")


@root_added_type.field("root")
def resolve_root_added_root(obj, *_):
    return obj["root"]


@stream_added_type.field("stream")
def resolve_stream_added_stream(obj, *_):
    return obj["stream"]


# Create executable schema
schema = make_executable_schema(
    type_defs,
    query,
    subscription,
    root_id_scalar,
    stream_id_scalar,
    node_id_scalar,
    json_path_scalar,
    root_type,
    registry_type,
    stream_type,
    foo_node_type,
    foo_data_union,
    foo_single_type,
    foo_list_type,
    foo_dict_type,
    foo_ref_type,
    foo_stream_ref_type,
    stream_update_event_union,
    stream_item_emitted_type,
    stream_finalized_type,
    registry_update_event_union,
    root_added_type,
    root_removed_type,
    stream_added_type,
    stream_removed_type,
)

# ASGI app for uvicorn
app = GraphQL(schema, debug=True)
