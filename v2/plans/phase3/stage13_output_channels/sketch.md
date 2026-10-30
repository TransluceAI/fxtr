## What it is
New type system in fxtr2 + the demo workstation for output streams, built on top of service refs.

Goal is to provide a standard best-practice interface for plumbing state out of a computation across RPC, with the ability for subscribers to subscribe to current state and then have that state get visualized on the workstation frontend UI.

## Where it goes

- Core types and implementation can go in fxtr2.common.streams
- Workstation-compatible frontend renderer in projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md
- Demo example in examples/fxtr_workstation_demo modified to actually use the new stream system


## User-facing interface

```python

type FxtrChannelData = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | FxtrID
    | EntityRef[Any]
    | ServiceRef[FxtrChannelStorage]
    | Mapping[str, "FxtrChannelData"]
    | Sequence["FxtrChannelData"]
)
"""Supported types that can be sent as channel messages.

Not directly used by the system because the system uses mashumaro to convert value types from a
caller-provided type annotation, but conceptually the type is only _valid_ if it can be serialized
into this form.
"""


class FxtrOutputChannel[T: Entity, V, R]:
    """An output channel running in a scope.

    Conceptually, an output channel consists of:
    - An initial "tag", which identifies the stream, and whose type identifies the stream's type.
        (This is type parameter T.)
    - A sequence of messages, which are sent iteratively while the channel is open. The
        tag + messages together represent the current state of the channel, and it is possible to
        send _additional_ channels as parts of sent messages, e.g. to represent sub-computations.
        (The type of each message is type parameter V.)
    - A final message, emitted when the the channel closes. This message represents the final
        state of the stream, _replacing_ all messages sent. (This is type parameter R.)

    While a channel is open / before a channel is complete, new subscribers receive the entire
    log of all messages sent along the channel. When the channel _closes_, subscribers are expected
    to drop live references to entities or services in these messages and to the channel object, and
    instead copy out the _final_ message and use that. New subscribers after the channel closes
    do not get any of the sequence messages and can only receive the final message.

    This abstraction is designed to support streaming output from nested computations, while also
    allowing consumers to avoid pulling data they do not need, or pinning resources longer than
    necessary. For example:
    - To represent a token stream from a language model sampling call:
        - Each response would be a channel, whose tag identifies the model and timestamp, and
            whose tag _type_ declares that this is a stream of partial model outputs that should
            be parsed in a particular way.
        - Each partial output streamed back by the sampler would be a message.
        - When the response completes, the final message would be the concatenation of all partial
            messages.
    - To represent a sequence of parallel jobs:
        - The batch job itself would be a channel, identifying itself as a batch job.
        - Each individual job would _itself_ also be a channel. These would then get emitted by the
            batch job channel as _messages_. Clients could choose to subscribe to individual jobs
            as they appear.
        - When each individual job completes, its own channel would finish. When the batch job
            completes, the batch channel would finish.

    Suggested pattern when working with streams across resources over RPC:
    - the caller passes in a temporary FxtrChannelSink that the callee can use.
    - if the callee wants to send individual messages of a type known to the caller, they can emit
        them directly.
    - but if the callee wants to send progress updates of their own internals, they can emit their
        _own_ channel into the parent channel, attaching their own tag.
    - at the end of the call, the caller closes the temporary FxtrOutputChannel, enabling any
        retained resources of the callee to be freed.
    """

    def __init__(self, channel_tag: EntityRef[T], scope: FxtrScope):
        """Open a new channel with the given tag, and mount it in the given scope.

        The channel will only be open as long as the given scope. If the scope ends without the
        channel finishing, the channel will abort. (Note that it is recommended to catch exceptions
        and send a custom finish message instead of simply letting the channel abort, because a
        fallback abort message discards all message content.)
        """
        # on init, creates and mounts both the sink end and the storage end
        ...


    @property
    def sink(self) -> ServiceRef[FxtrChannelSink]:
        """This channel's sink. Can be used to write to the channel. Mounted on the scope."""
        ...

    @property
    def storage(self) -> ServiceRef[FxtrChannelStorage]:
        """This channel's storage. Can be used to read from channel. Mounted on the scope."""
        ...

    async def finish(self, value) -> None:
        """Close the channel with a final value, representing the entire stream.

        Once a stream is finished, no more events can be emitted to it, and all previous messages
        it has sent are assumed to be safe to discard. The final value sent by `finish` represents
        the whole stream and is all that is exposed to new subscribers.
        """

    async def emit(self, item: V, from_scope: FxtrScope | None) -> None:
        """Emit a value over this channel.

        Equivalent to calling `emit` on the sink, but exposed directly for convenience.

        Args:
            item: The item to emit.
            from_scope: The scope to copy or bridge the item from. Defaults to the channel's scope.
        """
        ...

    async def replace_snapshot(self, items: list[V], from_scope: FxtrScope | None) -> None:
        """Replace the new-subscriber snapshot with a new (usually smaller) sequence prefix.

        Old subscribers do not receive any updates from replace_seed. However, new subscribers after
        replace_seed see `items` as the history of events _instead_ of the previous set of events
        (followed by all subsequent events published via `emit` as normal).

        This can be optionally used to condense a long message history into a smaller snapshot for
        new subscribers.
        """
        ...

@service_protocol
class FxtrChannelSink[V](Protocol):
    """Writer side of a channel. Allows values to be emitted into it."""

    @service_method
    async def emit(self, item: V) -> None:
        """Emit a value over this channel.

        Args:
            item: The item to emit. Any FxtrChannelStorage services in the result will be kept alive
                and possibly subscribed to until it closes.
        """
        ...

@service_protocol
class FxtrChannelStorage[T: Entity, V, R](EventSource[V], Protocol):  # conceptually, needs to be faked currently
    """Reader side of a channel."""

    # NOTE: every FxtrChannelStorage should have a single fixed type ID that identifies itself
    # as a FxtrChannelStorage, defined in a fixed namespace in fxtr2

    @service_method
    async def status(self) -> ChannelStatus[R]:
        """Check the status of this channel without fetching data."""
        ...

    @service_method
    async def subscribe(self, sub: ServiceRef[Subscriber[ChannelEvent[T, V, R]]]):
        """Subscribe to this channel.

        Subscribers immediately receive every message ever sent along a given channel, then continue
        receiving new events until they unsubscribe or the stream finishes.

        If the channel has already completed by the time a subscriber subscribes, the only message
        will just be the CompleteChannelSnapshot[R] | ChannelAborted.

        Note: Somewhat similar to listeners in the workstation system.
        """
        ...


type ChannelEvent[T, V, R] = OpenChannelSnapshot[T, V] | CompleteChannelSnapshot[T, V] | AbortedChannelSnapshot[T] | ChannelEmitEvent[V] | ChannelCompleteEvent[R] | ChannelAbortedEvent


@dataclass
class OpenChannelSnapshot[V]:
    """First message delivered to a new subscriber for an open channel."""
    tag: EntityRef[T]
    values: tuple[V, ...]

    kind: Literal["snapshot_open"] = "snapshot_open"

@dataclass
class CompleteChannelSnapshot[V]:
    """First and only message delivered to a new subscriber for a complete channel."""
    tag: EntityRef[T]
    result: R

    kind: Literal["snapshot_complete"] = "snapshot_complete"

@dataclass
class AbortedChannelSnapshot[V]:
    """First and only message delivered to a new subscriber for an aborted channel."""
    tag: EntityRef[T]
    reason: str

    kind: Literal["snapshot_abort"] = "snapshot_abort"

@dataclass
class ChannelEmitEvent[V]:
    """Event emitted to subscribers when the sink receives a new message."""
    value: V

    kind: Literal["emit"] = "emit"

@dataclass
class ChannelCompleteEvent[R]:
    result: R

    kind: Literal["complete"] = "complete"


@dataclass
class ChannelAbortedEvent:
    reason: str

    kind: Literal["abort"] = "abort"


@dataclass
class ChannelStatus[T]:
    """Status for an open channel.

    Can be used by consumers who do not want to fetch the full data of the channel but want to
    peek at its status and tag.
    """
    tag: EntityRef[T]
    status: Literal["open", "complete", "abort"]

```


## Renderer system

- New dispatch key for a stream content card
    - This dispatches based on the _tag_ of the stream, and gives it the FxtrChannelStorage that it can subscribe to
    - No-match renderer just has a card with a header that has a block reflink to the tag, then a streaming log of messages, and when it completes it drops all messages and replaces them with a block reflink to the result
    - But you can override this to show your stream content in some other way (e.g. string appending for a chat message)
- New dispatch key for a stream status preview
    - Shows a ChannelStatus as a block element
- FxtrOutputChannel implements its own regular workstation renderer dispatch keys
    - Preview-style keys: just show the status preview
    - Panel viewer: dispatch to the stream content card renderer
