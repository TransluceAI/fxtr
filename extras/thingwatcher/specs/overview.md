# Thingwatcher

A tool for watching things.

Thingwatcher is a viewer for the state of a large number of interacting objects and async tasks. The idea is that you should be able to easily instrument the objects in the code you are already using, and get a rich viewer to help you navigate what is going on.

## Organization
Python package in this directory. Managed by uv. pytest for tests. Frontend code in a separate directory in this directory. Frontend code accessed by python via relative path from the python file path.

## API

### High level
When using thingwatcher, you:
- Track objects (things) via `tw.track(obj)` -> becomes tracked by thingwatcher
- Call `tw.tick(level, desc)` to tell thingwatcher to save the state of all your things
- Call `await tw.pause(...)` to pause the current async task and wait for the thingwatcher UI to allow it to go ahead, allowing you to step through manually.
- Call `await tw.receive(...)` to pause the current async task and wait for input from the thingwatcher UI.
- Call `tw.add_root(name, anchor)` to add a named entry point for exploring the state graph.

When running without thingwatcher configured, `track`, `tick`, and `pause` are all ignored, and `receive`/`add_root`/`remove_root` raise an error.

When running inside a thingwatcher session, these commands instead route to a `Watcher` object, which saves them into a log that can be read out later. The watcher is responsible for taking the objects that you have tracked and serializing them into a form that can be viewed later. You need to declare snapshotters for your tracked object types to use them. However, this is usually done separately from the actual code you are instrumenting, for separation of concerns.

For most tracked object types, snapshotters need to serialize the object into a `ThingState`, determining how it should be written into the log. The watcher will visit every object at every tick, and expects to receive the full set of objects that exist at the given tick. It will then diff this against the values from the last tick in order to save a deduplicated delta into its persistent log.

### Instrumentation

To watch some existing code with thingwatcher, you can use the following APIs:

```py
def track(object: Any, typename: str|None):
    """Tracks an object with thingwatcher.

    No-op unless a thing watcher is active.

    All tracked objects must be weakref-referenceable.

    Args:
        object: The object to track. Will be tracked until the object is finalized or explicitly
            untracked.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.
    """
    ...

def untrack(object: Any):
    """Untrack an object if it is being tracked. (Not usually necessary due to weakref.)"""
    ...

def tick(level: str, desc: str | None):
    """Advance the thingwatcher with a given level and message.

    At each tick, if there is an active watcher, that watcher polls all of the objects to update
    their state and sends it off.

    Args:
        level: The level of the tick, for later filtering.
        desc: Optional description of what happened leading up to this tick.
    """
    ...

async def pause(
    anchor: Any,
    typename: str|None = None,
    tick_after: bool = True,
):
    """Pause execution of the given task until given the go-ahead by thingwatcher.

    No-op if there is no active watcher.

    Args:
        anchor: An object that describes the pause reason. Similar to `track`, but the object will
            only be tracked for the duration of the pause.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.
        tick_after: Whether to trigger a tick immediately after pausing, to show the pause state in the
            UI. If false, will suspend without ticking; in this case the pause will only be visible
            after some other task ticks.
    """
    ...

async def receive(
    anchor: Any,
    typename: str|None = None,
    tick_after: bool = True,
) -> Any:
    """Wait for user input from thingwatcher.

    If there is no watcher, this raises a NoWatcherConnected error.

    Args:
        anchor: An object that describes the pause reason. Similar to `track`, but the object will
            only be tracked for the duration of the pause.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.
        tick_after: Whether to trigger a tick immediately after pausing, to show the pause state in the
            UI. If false, will suspend without ticking; in this case the pause will only be visible
            after some other task ticks.

    Returns:
        The (CBOR-deserialized) payload sent back by thingwatcher.
    """
    ...

def add_root(
    name: str,
    anchor: Any,
    typename: str|None = None,
    replace: bool = False,
):
    """Add a named root entry point.

    Roots are named anchors (like "currentUser", "mainTask") that provide consistent starting
    points for exploring the state graph. They are tracked per-tick with historical queries.

    If there is no watcher, this raises a NoWatcherConnected error.

    Args:
        name: The name for this root (e.g., "currentUser", "mainTask").
        anchor: The anchor object to resolve via AnchorCapturer.
        typename: The name of the type thingwatcher should use to resolve the anchor. Defaults to
            the module.qualname of the anchor's type.
        replace: If True, replace existing root with same name. If False (default),
            raise ValueError if name already exists.
    """
    ...

def remove_root(name: str):
    """Remove a named root.

    The root will be invalidated at the next tick.

    If there is no watcher, this raises a NoWatcherConnected error.

    Args:
        name: The name of the root to remove.
    """
    ...
```

Usually, for thingwatcher-aware objects, you can call `track` at the end of `__init__`.

The `typename` of the various objects is used to determine how thingwatcher should interpret and save the object into the log. This is kept separate from the objects themselves so that integrating thingwatcher requires minimal changes. (Note that `receive` assumes thingwatcher is connected and thus )

### Configuration
You initialize a watcher globally, and run it using either:
- `async with watcher.watching_and_serving():` - an async context manager that keeps the watcher active until the context exits
- `await watcher.watch_and_serve_until(done)` - a convenience method that waits for a coroutine inside `watching_and_serving`

Watcher constructor takes params to configure its path, port, etc.

The watcher requires you to configure how different types of thing should be saved into the store. This can be done either on a specific `watcher` or globally on `tw`.

#### Configuring

```py
@dataclass
class ThingLink:
    link_type: str
    link_key: CborableTypes
    to_thing_key: CborableTypes

@dataclass
class ThingState:
    key: CborableTypes
    thing_type: str
    thing_data: CborableTypes
    links: list[ThingLink]

@dataclass
class ThingDelta:
    key: CborableTypes
    data_diff_kind: Literal["replace", "append", "no_diff"]
    new_data: CborableTypes  # must be None if data_diff_kind is "no_diff"
    added_links: list[ThingLink]
    removed_link_keys: list[CborableTypes]

@dataclass
class ThingRef:
    key: CborableTypes

class StateSnapshotter(Protocol):
    """Snapshotter that takes an object and captures an immutable snapshot of its state.

    Usually, snapshotters should return the full state of the object. If an object appears in one
    tick and is not present in the next tick, it will be treated as if it had been deleted.

    As an optimization, snapshotters are allowed to emit a ThingDelta instead of a ThingState. This
    is equivalent to emitting the same ThingState as was emitted in the last tick, but is more
    efficient.
    """
    def snapshot(self, object: Any) -> ThingState | ThingDelta | list[ThingState | ThingDelta]:
        """Snapshot a possibly-mutable object at a given point in time.

        Args:
            object: An object we are tracking.

        Returns:
            Either the current state of the object, or a change in the object since the last
            snapshot. Can also output a _list_ of things if this Python object maps to multiple
            logical things to track with thingwatcher.
        """
        ...

def register_snapshotter(
    tracked_type=str | type[Any],  # a type obj gets mapped to module.qualname for convenience
    state_snapshotter: StateSnapshotter
):
    """Registers a snapshotter for a particular type, used for `track`.

    Args:
        tracked_type: The typename of an object passed to `track`.
        state_snapshotter: Snapshotter to use.
    """
    ...

class AnchorCapturer(Protocol):
    """Handler that determines how to serialize an anchor object.

    An anchor is a Python object that maps to a single (primary) thing, giving it a stable
    identity. Anchors are used for:
    - `pause()` and `receive()`: The anchor identifies what is being paused/waited on
    - `add_root()`: The anchor identifies what the named root points to

    Unlike tracked objects (which use weakrefs), anchor objects for roots are held with
    strong references until explicitly removed.
    """
    def handle(self, anchor: Any) -> ThingState | ThingRef:
        """Snapshot an anchor object.

        Args:
            anchor: An anchor object passed to `pause`, `receive`, or `add_root`.

        Returns:
            Either the current state of the object, or a reference to an already-snapshotted thing.
            `ThingRef` can be used if `anchor` is also a tracked object, and we expect its state
            to be already captured by a `StateSnapshotter`.
        """
        ...

def register_anchor_capturer(
    anchor_type=str | type[Any],  # a type obj gets mapped to module.qualname for convenience
    anchor_capturer: AnchorCapturer
):
    """Registers an anchor capturer for a particular type.

    Used for `pause`, `receive`, and `add_root`.

    Args:
        anchor_type: The typename of an anchor object.
        anchor_capturer: Capturer to use.
    """
    ...


def configure_rendering(
    thing_type:str,
    color: str,
    link_colors: Mapping[str, str] | None=None,
):
    """Configure how a given thing should be rendered.

    Args:
        thing_type: The type of the thing to configure. Should match the thing_type in ThingState.
        color: The color to show the thing.
        link_colors: Optional colors for links by link type, for links from this thing.
    """
    ...
```

## Architecture

### Backing store
The backing store can be sqlite. It should have a few main tables:

Ticks:
- tick: unique int
- timestamp
- level: str  # like a log level
- type: str
- data: bytes

Thing states:
- thingstate_id
- valid_from: int
- valid_until: int | null  # if null it is still valid
- thing_key bytes
- type str
- data bytes
- data_extends: thingstate_id nullable  # this means that the data for this state should be appended to the data from previous states

Note: `triggerable` is computed dynamically from watcher state (which anchors are currently paused), not stored in the database. Only the current tick can have triggerable things.

Links:
- thing_key bytes
- valid_from: int
- valid_until: int | null  # if null it is still valid
- type str
- link_key bytes
- target_thing_key bytes

Trigger record:
- thingstate_id
- tick int
- trigger_payload bytes

Roots:
- root_name: str
- thing_key: bytes
- valid_from: int
- valid_until: int | null  # if null it is still valid

All keys and data are represented as CBOR. Keys are CBOR serialized with canonical=True in Python's cbor2.

### Watcher object + GQL server
When `watch_and_serve_until` is called, watcher object starts a starlette server with a graphql route via ariadne. It also serves html and tsx pages which are compiled dynamically via esbuild. Based on the approach in `v2/fxtr2/gql_spike`.

The graphql server is how the frontend communicates with the backend. It has ops for
- retrieving all ticks
- for a given tick, retrieving all thing metadata (not including encoding or data) and also all links
- for a given tick, finding things that were removed at this tick (valid until this tick)
- subscribing to new ticks
- retrieving deltas of what things changed between one tick and another tick
- retrieving the data + encoding for a single thing at a given time
- get some metadata about the config and the graph (e.g. color mappings)
- (etc)

There are also GraphQL mutations for triggering actions:
- `triggerPause(key, payload)`: Trigger a paused thing by its thing_key (base64-encoded). The payload is passed to the waiting `receive()` call. Returns a `TriggerResult` with success/error info.
- `triggerTick(level, desc)`: Trigger a tick manually.

Additionally, there's a REST endpoint for debug/test synchronization:
- `POST /debug_trigger/{name}`: Fire a named debug trigger (for test sequencing, not production pause/receive).

### MCP server
Minimal MCP server to allow AI agents to talk to the graphql server directly, retrieve data back, and trigger updates.

### Frontend
The frontend should be written in React tsx.

The frontend should use `urql` to visualize the data. It should use base-ui for UI components like tooltips, pickers, etc.

On the bottom of the screen should be a timeline. It should show all the ticks, and have a scrubber you can use to select ticks, or step back and forth. This should show the tick message and level. There should be a selector that selects which levels of tick to show in the scrubber (based on the levels that actually appear; can be arbitrary strings). There's also a position at the end called "live" which waits for new ticks and immediately activates them.

The main body of the screen is a dockview panel system. There is one main tab which cannot be closed, which is a 2d canvas with things shown in it via cytoscape.js. Things point to other things based on the links. The things should be laid out with fcose. All active things are shown, as well as things that were deleted in the current step. Nodes are formatted based on state:
- idle nodes are normal
- new nodes have a green border
- changed nodes have a yellow border
- removed nodes are transparent and have a red dashed border

When the set of active nodes changes due to slider or updates, we re-run layout.

Each thing is shown as a colored circle. On hover it instantly shows a tooltip with the type and key of the thing.

There are two side panels open by default, one above the other, on the right of screen. The top one is a details-view panel. The clicked node has its data shown in this view.

The bottom one is a things view. It shows the list of things at this step by key. You can select the things here also. There is a checkbox to show only changed things, and to sort either by name or by last-updated.
