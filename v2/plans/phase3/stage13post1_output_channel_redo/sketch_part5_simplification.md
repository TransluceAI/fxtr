
From initial implementation of plans/phase3/stage13_output_channels/overview.md: it turns out there are a lot of difficulties in the lifetime mechanics of sending new channels out, regarding where those channels are mounted. Additionally there is just a fairly high complexity surface for output channels.

Proposal: Fully rework the system by enforcing a new structure:
- Enforce a tree relationship on channels: child channels are always associated with their parent channel
    - Can't simply send a new channel down a channel, can only _spawn a child_ channel
- Strictly NO services sent over a channel, not even output channel services
    - Thus, a channel can be simple data-output only
    - To conceptually "send a channel down a channel" you instead send a channel _ID_ down a channel
    - On the reader side, you recognize that ID using app-specific logic and then request a subchannel reader if you are interested in receiving events from the subchannel
        - (and what this means is up to the app. a specific user could choose how they tag channel IDs and what types they assume the results should have, and they could make both sides align on the type annotations. if a specific user wanted to allow non-tree-based links between subchannels they could have all of their subchannels derive from the root channel and just pass the data along in subchannels as needed, with the reader side doing the matching lookups via convention)
- Clearer separation of reader and writer ends of a channel:
    - If you have the writer end of a channel, you can _create_ writer-ends of new subchannels
    - If you have the reader end of a channel, you can _retrieve_ reader-ends of new subchannels
    - When writing to subchannels, you don't yourself need to construct both ends, you just grab from parent
        - This means that parent streams don't keep child streams alive, and child stream lifetime is not owned by the emitter at all. Rather, the child stream is potentially just bookkeeping done by the parent
- Rework the emission / tag / close system and read/write interface (described in more detail later)
    - Channels can have a "status" which can be subscribed to and which is lighter-weight and replace-only
    - The channel owner _is_ a service ref, there isn't a separate emitter
- Also, while here, change any spurious "stream" references to "channel" (I was sloppy initially and so now we use both terms in various places, e.g. filenames and class names)

What this buys us:
- Much simpler lifecycle tracking on both sides. No complex scopes to manage; you can just call methods to follow subchannels on both reader and writer ends.
- Much easier to "route" messages through a bus in the future, since the writer end is fully write-only + data-only and all writes relevant to a channel flow up through a single entrypoint, rather than having state stored inside the job
- Job no longer has to worry about mounting or keeping alive the channel, the channel is just like any other external service

Overview of what this would look like:

```python

_U = service_dialect.OverrideThisTypeBeforeUsing

@service_protocol
class FxtrChannelWriter[T: Entity, S, V, R](Protocol):

    async def start(tag: EntityRef[T], initial_status: S):
        """Starts the channel with a given stream tag and an initial status.

        Can only be called once. It is an error to call this multiple times, or to emit events
            before calling this.

        Args:
            tag: The stream tag.
            initial_status: The initial status of the stream.
        """

    async def emit(*, events: list[V] | None = None, status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT, replace_history: bool = False):
        """Update the stream.

        Args:
            events: New events to add to the stream.
            status: New status to set on the stream. If not provided, don't update status.
            replace_history: If True, delete all events in the history and replace them with the
                new events.
        """

    async def finish(self, final_value: R, final_status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT) -> None:
        """Close the channel with a final value, representing the entire stream.

        Once a stream is finished, no more events can be emitted to it, and all previous messages
        it has sent are assumed to be safe to discard. The final value and status sent by `finish`
        represents the whole stream and is all that is exposed to new subscribers.

        It is an error to call `finish` more than once.

        Args:
            final_value: The value representing the final result of the stream.
            final_status: The final status event for the stream. If not provided, the existing
                status is used.
        """


    async def spawn_subchannel(self, name: str | None = None) -> tuple[str, ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]]:
        """Spawn a subchannel of this channel.

        The subchannel will be valid as long as this (parent) channel is open, and will abort if the
        parent channel finishes without the subchannel finishing.

        Args:
            name: An optional name.

        Returns:
            Tuple (subchannel_key, subchannel) where subchannel_key is a unique key that can be
                used to refer to the subchannel. Generally, subchannel_key should be emitted out
                into the parent channel's own output event stream, so that subscribers can find
                and subscribe to the subchannel.
        """

    async def abort(self, reason: str):
        """Abort this channel due to an unrecoverable error or usage violation.

        The "main" user of a channel is recommended _not_ to use this, and should instead use
        `finish` to set a typed final value and status (e.g. an app-specific error status).

        `abort` can be used to abort a channel in exceptional cases where normal recovery is not
        possible or where the aborter is not the "main" user, e.g. if we are tearing down a channel
        that we did not start, or if an error occurs while trying to emit a finish message itself.
        `abort` is used internally to tear down open child channels when their parent channels
        finish.

        `abort` is idempotent; an abort on an already-finished-or-aborted channel does nothing.

        Args:
            reason: Some best-effort reason for teardown, used for debugging.
        """

class ChannelTag[S, V, R](Entity):
    """Abstract base for channel-tag entity types.

    Subclassing pins the status type S, message type V, and final-value type R for any
    channel started with a tag of this type. This can be used to encode the expected
    types of a channel into the Python type annotations. Note that by convention the
    fxtr _entity type ID_ of the stream tag is assumed to uniquely identify the _wire_
    types of S, V, and R; the Python typechecker then helps validate this at typechecking
    time and the mashumaro converter ensures it works out at runtime.
    """


@service_protocol
class FxtrChannelReader[T: Entity, S, V, R](Protocol):

    async def get_tag(self) -> EntityRef[T]:
        ...

    async def subscribe_state(self, subscriber: ServiceRef[Subscriber[ChannelState[S]]]) -> ServiceRef[UnsubscribeHandle]:
        ...

    async def subscribe_full(self, subscriber: ServiceRef[Subscriber[ChannelUpdate[S, V, R]]]) -> ServiceRef[UnsubscribeHandle]:
        ...

    async def peek_state(self) -> ChannelState[S]:
        ...

    async def peek_full(self) -> ChannelUpdate[S, V, R]:
        ...

    async def open_subchannel_reader(self, key: str) -> ServiceRef[FxtrChannelReader[_U, _U, _U, _U]]:
        """Retrieve a reader for a subchannel by key.

        Args:
            key: The key originally returned by `spawn_subchannel` on the writer end of the channel.
                Must be a subchannel of _this_ channel, not some other channel.

        Returns:
            A reader for the subchannel spawned by `spawn_subchannel`.
        """
        ...



async def start_channel_and_infer_types[S, V, R](
    untyped_channel: ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]],
    tag: EntityRef[ChannelTag[S, V, R]],
    initial_status: S,
    *,
    scope: FxtrScope | None = None,
) -> ServiceRef[FxtrChannelWriter[ChannelTag[S, V, R], S, V, R]]:
    """Start a channel with a given tag entity, and infer its type for typed usage later.

    Args:
        untyped_channel: Ref to the untyped channel (e.g. returned by spawn_subchannel)
        tag: The tag to send. Must be a concrete subclass of `ChannelTag`; used to infer the correct
            type annotations for serialization/deserialization of the S, V, and R types.
        initial_status: Initial status for the channel.
        scope: Scope in which both the service and its tag can be resolved. Defaults to the active
            scope.

    Returns:
        A copy of untyped_channel with its target type replaced with the types inferred from `tag`.
    """
    ...


def infer_channel_reader_type(
    reader: ServiceRef[FxtrChannelReader[_U, _U, _U, _U]],
    as_type: EntityTypeAnnotation[ChannelTag[S, V, R]]
) -> ServiceRef[FxtrChannelReader[ChannelTag[S, V, R], S, V, R]]:
    """Infer a channel reader's full type from the type of its tag."""



@dataclasses.dataclass(frozen=True)
class ChannelState[S]:
    status: S
    stage: Literal["open", "complete", "abort"]

type ChannelUpdate[S, V, R] = (
    OpenChannelSnapshot[S, V]
    | CompleteChannelSnapshot[S, R]
    | AbortedChannelSnapshot[S]
    | ChannelUpdateEvent[S, V]
    | ChannelCompleteEvent[S, R]
    | ChannelAbortedEvent
)


# TODO: override here where if tag_type is not passed, use _U and require replacement for both
async def create_root_channel[S, V, R](
    *,
    tag_type: EntityTypeAnnotation[ChannelTag[S, V, R]] | None = None,
    scope: FxtrScope | None = None,
) -> tuple[
    ServiceRef[FxtrChannelWriter[ChannelTag[S, V, R], S, V, R]],
    ServiceRef[FxtrChannelReader[ChannelTag[S, V, R], S, V, R]],
]:
    """Create and mount both a reader and a writer end of a channel.

    Args:
        tag_type: Type for the tag of the streams. Used to infer serializers/deserializers for the
            S/V/R messages. If None, the writer and reader must have their types explicitly
            configured later.
        scope: The scope to mount the channel implementations on. Defaults to the active scope.

    Returns:
        A pair of root writer and reader channel services. Best practice: use the writer inside a
            try block and call `abort` on exceptional exit, so that the writer aborts _before_ the
            reader itself tears down.
    """

```

At the end of stage13post1 we should replace _all_ users of the old channel system with this new one and make sure all tests are green.
