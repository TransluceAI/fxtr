# RPC improvements and event streaming over RPC

Overall goal: Expand the ability of the RPC system in fxtr/impls/rpc.py to support streaming of events in order to monitor progress.

## Initial state
An RPC system exists, and allows FxtrProtocolImpl objects to communicate, including by passing (handles to) implementation objects to and from other objects over RPC.

A few well-known entities can be advertised by each side, and these will not be sent. However, there is no way to adaptively save entities that need to be referred to multiple times.

Each time an RPC object is sent over the boundary, it is wrapped in a new type on the other side. This means that if you send the same implementation back and forth over RPC, then methods on those objects must unwind back-and-forth over the connection; e.g. if A sends Foo to B, and B sends that Foo back to A, and then A calls foo.bar(), this data is serialized and sent from A to B and back to A, where A handles it, then sends the result back to B which replies back to A.

## Phase 1: Loopback fix

Make it possible to send references to a counterparty's own handles.

- In addition to FxtrRPCHandle, there should be a FxtrRPCLoopbackHandle(handle_id: int) type, which indicates a handle that is currently exposed by the _other_ side of the connection.
- FxtrRPCConnection should hold onto a remote-exports dict mapping id(RemoteSubtargetImpl) -> handle id
    - It should add an entry when creating a RemoteSubtargetImpl
    - It should remove the entry in the finalizer for the RemoteSubtargetImpl
- FxtrRPCConnection's `register` should be rewritten to return the FxtrRPCHandle. Also, it should first look up whether the impl is in its remote-exports dict, and if so, instead should return the FxtrRPCLoopbackHandle. in this case no incref has to happen.
- in _sendable_value_to_cbor:
    - if trying to serialize FxtrRPCLoopbackHandle error
    - if serializing FxtrProtocolImpl, return the handle from `register` directly
- in _cbor_to_sendable_value:
    - if it is a FxtrRPCHandle do what it currently does
    - if it is a FxtrRPCLoopbackHandle, look it up in our own `connection._targets` mapping and return that

Then add a test to make sure loopback works (preserves object ID across ping-pong) to tests/test_rpc.py

## Phase 2: Cross-message entity caching.

Goal is to add more messages to make it possible to explicitly and safely track entity presence across the connection without re-sending data.

New model of how entity storage works:
- Each side of the connection maintains a set of cached entities in a CID->Entity map. Initially these are the advertised entities of the CachingEntityLocator, but the connection object also holds onto its own temporary cache.
- Each side of the connection maintains "remote leases" of what the other side has. This must always be a _subset_ of what the other side _will_ have at the moment that the counterparty receives all pending outgoing messages from the sender.
    - This essentially means that the only way to _add a remote lease_ that the other side has something is either (a) to send that entity, (b) to receive a note that the other side has the entity.
    - Conversely, the only way to validly _remove an entry that this side has_ is to wait to receive a note from the remote that it no longer expects it.
- Counterparties exchange messages to update this state:
    - "has": Sent by a party to inform that they have some entities and promise to retain them, so that the counterparty does not have to send them. This is sent at the beginning of the connection, splitting the "info" message into two parts: first a version exchange, then a "has" message.
    - "send": Sent by a party to provide some entities. The receiver is obligated to retain them, as they may be referenced in the future, and from the sender's perspective the receiver already has them from this point onward, since messages are received in order. There is a flag which, if set, also marks those objects as being retained by the sender, similar to a "has" message.
    - "withdraw": Sent by a party to inform that they want to release some objects that they are retaining, and so the other side should remove them from their own remote guarantee set. References either some explicit list of CIDs, or the set of CIDs in a previous "has" or "send" message by message index (generalized from call counter, with send tagged by who sent them since the calls are distinct).
    - "release": Sent by a party to inform that they do NOT need the other party to hold some entities, and that they will not assume the other party has them anymore. References either some explicit list of CIDs, or the set of CIDs in a previous "has" or "send" message by message index (generalized from call counter, with send tagged by who sent them since the calls are distinct).
- Use of "call" and "response" no longer passes any entities; instead the entities are assumed to have already been available due to a "has" or "send" message previously in the conversation.
    - (Handles are managed the same way as before and not affected by this; they can be sent just like any other entity, and withdrawn/released in the same way, as just data, and this is tracked separately from the weakref of whether the subtarget at that handle is still alive)
    - "event_stream" response messages (early replies to "call" messages) should behave similarly
- "info" is split into two messages: a "handshake" message that establishes the versions supported, and a "declare_root" that identifies a FxtrRPCHandle for the root object. "declare_root" can assume the FxtrRPCHandle was already sent in a "send" message.
    - Connection initiation should then send "handshake", followed by "send" and "declare_root" if the connection wants to serve a root impl
- There is an async private contextmanager function which can be used to "reserve" any CID that the counterparty has. It should increment a value in an internal `lease_reservation_count` dict while held.
- Initial withdraw-release schedule:
    - When sending "call", we now send "send" with the retain flag, followed by "call", followed by "withdraw" and "release" on that send.
    - When sending "respond", we send "send" without the retain flag, followed by "respond", followed by "release".
    - When receiving "withdraw", we mark the targets as withdrawn. As soon as their `lease_reservation_count` drops to zero, or immediately if already zero, we remove it from our remote leases dict and send a "release" method.
    - When receiving a "release", if these are entities in our own cache, we drop them from the cache. (If they were in the original loader cache, we keep them.)
    - While processing a "call", we reserve the entities in the call until it finishes, then allow them to be released.

At this stage, things should mostly work as before. However, this now gives the option of implementing alternative clients that hold onto some entities for longer than would otherwise be required.

TODO: before starting this part, figure out how it interacts with entity streaming. we will want it to be straightforward to enable caching for entity streaming. current thought is that RemoteSubtargetImpl can have a special tag that makes it cache its state
