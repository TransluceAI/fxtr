
Sketch of json transport:
- basically the public API of the json transport module is that it accepts an anyio.abc.ObjectStream[JSONTypes] and produces an anyio.abc.ObjectStream[IRValue[JSONLeaf]]
    - so, it assumes you already have a mechanism for bidirectionally asynchronously sending any JSON-serializable value across a channel
    - and then it multiplexes capabilities over this channel so that you can send IRValues over it, and whatever object you get on the other side behaves identically to the value on the local side: local handles become remote handles, local RPCPending become remote RPCPending, etc, and all events just automatically happen
    - (ObjectStream is an ABC; we will want to construct concrete instances of it)
- it implements this using a variant of the Cap'n'Web protocol https://github.com/cloudflare/capnweb/blob/main/protocol.md
    - mostly we match the same behavior as Cap'n'Web but with a few tweaks
    - changes to instructions:
        - drop instructions from cap'n'web that are specific to data types that are not in our IR (e.g. "pipe")
        - the cap'n'web "stream" instruction becomes "submit"; it has similar semantics: like "stream", there is no ability to promise pipeline off of it, it is considered pulled immediately and then the remote side immediately considers it discarded as soon as it sends the resolve/reject. but is generally used to implement dispatch_and_wait rather than being specific to streams.
        - we need to add two new instructions ["teardown", id, value] and ["broken", id, exception] for teardown
            - these get sent if a live (non-disposed) handle or promise tears down either normally or exceptionally
        - also, there is no "main" interface; instead there is an instruction ["send", expr] which sends a value to the top-level ObjectStream. so you can establish a sort of advanced bidirectional connection and then send sequences of arbitrary objects back and forth in either direction. each such expr could include exports. things can be pushed along the connection normally without affecting the ObjectStream; the "send" instruction is just for the top level stream. you can mirror normal Cap'n'Web by doing something like `["send", ["export", 0]]`
    - changes to expressions
        - drop expressions for types we don't support
        - add expressions for tag values
        - somewhat reinterpret semantics for "promises" to match our RPCPending system; RPCPending values are sent as promises and promises become RPCPending, whereas handles go through the non-promise path. there are some details here to work out I think, in order to make everything connect up properly
- tests can integrate with the existing transport tests although might want to rework it slightly
    - basically for tests of the JSON transport it should create an implementation of anyio.abc.ObjectStream[str], then lift it to an anyio.abc.ObjectStream[JSONTypes] by doing json serialization/deserialization, then lift _that_ to anyio.abc.ObjectStream[IRValue[JSONLeaf]] using the json transport module, then hold the stream open in the context and pass the user's value through the stream
