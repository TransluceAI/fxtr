# Simplified capability-passing system over websocket

Overall goal: Expand the ability of the RPC system in lib/fxtr/fxtr/impls/rpc.py to support a _simplified_ version of capability passing in the style of Cap'n Proto. In particular, it should be possible to have bi-directional communication proxied across Python objects.

# Current state

There are multiple types of class involved with an Impl, which were designed around enabling impls to expose HTTP routes, which are actually no longer used at all by the system.

RPC system supports independent call/response message passing, currently implemented over Starlette as a HTTP api.
- lib/fxtr/fxtr/impls/rpc.py contains the RPC protocol, which sends entities as needed over CBOR.
- lib/fxtr/fxtr/impls/impl_base.py describes what an implementation has to do.
- other files in lib/fxtr/fxtr/impls implement the client and server for the RPC connection.

Right now, the only type of objects that can be sent over RPC are JSON-serializable objects and entities, as determined by the AnyFrozenIPLDable type. These must be frozen into tuples and frozendicts, and they are tightly coupled to a content-based serialization format that we are using for other purposes, despite not really needing to be.

# [DONE] Phase 1 desired state

Simplify "impl" indirection and remove duplication.

- Modify impl base class in lib/fxtr/fxtr/impls/impl_base.py and lib/fxtr/fxtr/impls/impl_py_local.py to unify ProtocolImpl, PythonImplProxy, and PyImplTarget
    - There should just be, in lib/fxtr/fxtr/impls/impl_base.py:
        - an ABC type FxtrProtocolImpl with a method `protocol_spec` getting the protocol spec
        - a concrete mixin FxtrImplClass (subclass of FxtrProtocolImpl) which, in __init_subclass__, looks at base classes and expects to see exactly one vase class that has a core.protocol_spec-registered base class, and stores it as a classmethod `_fxtr_protocol_spec`, then returns it via `protocol_spec`.
- The current logic in LocalImpl can be moved to implserver and used to serve every impl instead of having this logic be owned by the impl. The server should check the protocol_spec of the impl and only allow requests to the methods that are annotated on the ProtocolSpec object, as well as to `protocol_spec` itself.
- LocalImpl and ServableProtocolImpl can then be removed
- RemoteImpl and RemoteImplPythonWrapper in lib/fxtr/fxtr/impls/impl_remote.py can be combined
- lib/fxtr/fxtr/impls/impl_py_local.py can be removed (subsumed by base and server)
- All old implementations of PyImplTarget should instead implement FxtrImplClass
- All old callers of as_python_impl should just use the FxtrProtocolImpl directly and call methods on it
- The entry points for impls should have FxtrProtocolImpl as the annotation for their deps

# Phase 2 desired state

Expand type system to support passing impls around and have input/output types be a bit more flexible.

- There should be a Python type alias `SendableValue` in core, beneath where `ProtocolSpec` is currently defined, that accepts any CBOR-serializable primitive, CBOR-serializable collections, entities, and FxtrProtocolImpl objects. It should be like a mutable version of `core.AnyFrozenIPLDable`, and it should NOT accept tuples or frozendicts.
- The `core.define_protocol` helper should inspect the type annotation and check whether it is a valid subset of `SendableValue` using a new function, instead of using the current AnyFrozenIPLDable check. The new function should be called `is_sendable_value` and the way it should work is:
    - It should check if it is a dict[str, ...] or list[...], and if so recurse on that.
    - If not, it should check if it is exactly a CBOR-serializable primitive type (the same primitives in AnyFrozenIPLDable); if so that's OK.
    - If not, it should check if it is a subclass of FxtrProtocolImpl; if so that's OK.
    - Otherwise it should try to call `core.python_type_to_hub_type_spec`. If that succeeds, check if `TypeSpec_AnyEntity` is a supertype of the result. If so that's OK
    - Otherwise it's an error.
- The send/receive RPC logic in lib/fxtr/fxtr/impls/rpc.py should convert these to/from CBOR directly without using TypeSpec_AnyFrozenIPLDable; instead it should recursively walk the structure, and only transform CIDs to/from entities (using TypeSpec_AnyEntity's ipld conversion logic). If it encounters a `FxtrProtocolImpl` it should raise a temporary NotImplementedError; this will be added in phase 3.
- All existing users of @core.define_protocol and FxtrProtocolImpl inside `lib/fxtr/core` and `lib/fxtr/tests` should be modified to use dicts and lists and avoid freezing them.
- All existing users of @core.define_protocol and FxtrProtocolImpl inside `lib/fxtr/examples` should be modified to use dicts and lists and avoid freezing them. You can look specifically for protocols that use `frozendict[` and `tuple[` as type annotations, and then look specifically for subclasses of those protocols, since I think these are the only things that should need to change; other implementations and protocols should be fine as-is.

# Phase 3 desired state

Refactor RPC system to have the same user-level behavior, but use websockets instead of HTTP.
- Client-server connection should be a websocket connection over `/.well_known/org.transluce.fxtr.rpc/ws`
- All websocket messages should be CBOR
- There should be a client object FxtrRPCConnection (defined in lib/fxtr/fxtr/impls/rpc.py) that maintains a persistent websocket connection when built and can act either as the server side or client side. It should have necessary handlers to handle messages.
    - It should have an `await call(target_handle, method, *args, **kwargs)` method. Calling this should send a CBOR message with "call" as the method and passing the target handle and a fresh call ID. It should then add an anyio event to a handler list and wait. When the websocket responds with a "response" message with the same call ID, it should retrieve the result and return it, also clearing the handler state. If disconnected, all pending calls should raise RemoteRPCError. If the server responds with an error response, the `call` call should receive this and raise RemoteRPCError.
    - It should store a set of "targets" in a dict from handle ID number to target. There should also be a mapping from target python ID to its handle. Its `__init__` should take an optional `root_target: FxtrProtocolImpl` which is registered as id 0.
    - The connection should own a CachingEntityLoader at init. It should immediately send an info message when constructed, based on the CachingEntityLoader. It should also watch for its counterparty's info message and save the known CIDs on the other side.
    - The `format_call_request` and `format_call_response` methods should be folded into private helper functions on FxtrRPCConnection, and it should use these private functions to build the messages it sends on either side of `call`.
- The current client object (RemoteImplPythonWrapper) should make a websocket connection and bind it to a FxtrRPCConnection on first call like it does in the old version with _fetch_server_info. It should then route method calls to the `call` on the RPC connection with target handle always 0.
- The server should serve a websocket handler over the above URL. On connect, it should create its own FxtrRPCConnection object around the connection and pass its root object, and then send the info message.
- Client and server impls should be wired up to use this new websocket system.

Note that at this stage, the only target handle id is 0. This machinery is to set up for the next phase where we will have more targets than this.

Nevertheless, at this stage all existing users of clients and servers should be unchanged and continue to work without changes.

You can test using pytest in `lib/fxtr/tests`.

## Phase 3 extra refactor
During phase 3 implementation, discovered a need to refactor how implementations are scoped. New rule: the provider is always responsible for constructing remote impls for the return values, not the dependencies.

To implement this:
- In impl_remote:
    - Make RemoteImpl take a task group as __init__ arg
    - Add a class method that is an async contextmanager `RemoteImpl.connect(...)` that takes all the args of __init__ except the task group, and creates the task group to pass in
    - create a helper function that takes an anyio send stream, does the aconnect_ws, builds the connection, and runs the receive loop
    - have _ensure_connection start that task and retrieve the connection from the receive stream (you can see the new implementation of lib/fxtr/fxtr/providers/uv_py_provider_entrypoint.py for an example of the pattern)
- In lib/fxtr/fxtr/providers/provider_base.py:
    - Remove RemoteImplRef and ImplRoute
    - `provide` should both take and return FxtrProtocolImpl only
    - route_as_impl can be removed
- For all subclasses of ImplProvider:
    - Make sure they return a FxtrProtocolImpl
    - Remove any logic for building RemoteImpl from RemoteImplRef
    - In uv_py_provider in particular, when returning a RemoteImpl from `provide`, attach it to the provider's own task group
- For all other users of RemoteImpl (or RemoteImplRef):
    - Make sure that `RemoteImpl.connect` is always used in a context manager only for the scope where it is needed


# Phase 4 desired state

Expand RPC system to support passing sub-object capabilities.

- Connections
    - Connections should have a connection refcount dict that tracks a reference count for each handle (initially incremented when registered). When refcount drops to zero we remove the target from the dict (but never re-use the handle id).
    - Connections should have a `register(target: FxtrProtocolImpl) -> int` method, which registers a handler object and gets back a local handle. When this RPC connection _receives_ call messages for this handle, it should redirect them to the target.
        - objects we've seen before get the same handle ID
        - whenever we register, we increment a reference count on the object.
    - The connection should have a `_decref_mine(my_handles: dict[int, int])` method, where keys are handle ids, and values are number of times to decref them.
        - When a handle count reaches zero, we remove it from the handle dict and clean up its state.
- Connections should have a `_decref_theirs(their_handles: dict[int, int])` method. This should send a message to the other side with type "decref". The receiving connection should call `_decref_mine` on it.
- Connections should store a weak-valued map from their_handle to a FxtrProtocolImpl object
- Call requests and responses should allow passing this new alias type (as handled between the implserver and impl_remote client)
    - When sending messages, whenever we see a FxtrProtocolImpl, we
        - register it with the connection to get a handle (thus incrementing its refcount)
        - replace that FxtrProtocolImpl with a DataEntityBase subclass called "FxtrRPCHandle(handle_id: int)" (namespace "!builtin.rpc")
        - send the message over the websocket
    - When receiving messages, we maintain a decref counter. Then
        - whenever we see a FxtrRPCHandle, we do the following
            - if this id is already in the weak-valued their-handle map, retrieve the impl proxy and add 1 to the temporary decref counter for this handle
            - if this ID is NOT in the weak-valued their-handle map, create a new FxtrProtocolImpl object of a NEW class `RemoteSubtargetPythonWrapper` which is like `RemoteImplPythonWrapper`, passing it the handle id. Put it into the weak-valued map. Add a weakref.finalize on this wrapper so that when it dies, we send a `_decref_theirs` method with this handle ID and count 1
        - finally at the end if anything is in our counter, send our temporary counters to `_decref_theirs`

The idea here is that every time the exporter refers to one of its own handles, it promises to keep it around until the receiver is done, and the receiver will decrement its reference count only when it is either sure it has an existing reference or sure that nobody else needs the reference.

Note: if we send the same object multiple times, we will call `register` multiple times, so increment its refcount multiple times. Then on the receiving side we will _deserialize_ it multiple times, which will translate to a matching number of decrefs (either now or queued).

# Phase 5 desired state

Better entity spawning.

Currently the lib/fxtr/fxtr/providers/uv_py_provider.py and lib/fxtr/fxtr/providers/uv_py_provider_entrypoint.py talk to each other by having the entrypoint set up a server that the provider connects to, and then the spawner serves a bunch of impls on a bunch of different routes, which are never removed.

We could simplify this by instead having the spawner _directly return_ the spawned implementation rather than serving it on a route on the other server, piggy-backing on the existing connection. This would give us automatic garbage collection when we no longer need the implementation on the client.

In more detail:
- DependencyPlanUrl should become DependencyPlanItem and have a plan and also a FxtrProtocolImpl
- _UV_SPAWNER_PROTOCOL should accept DependencyPlanItems and return a FxtrProtocolImpl
- `UVManagedPyImplProvider` should store and track a spawner for each _PlanGroup instead of creating it in `provide`
- `UVManagedPyImplProvider.provide` does not need to do _serve_dep_task or attach a RemoteImpl to the spawn result, it should just return the spawned impl

## Note on protocol_spec handling

To avoid async protocol_spec() calls, protocol specs are now sent eagerly:
- The root impl's protocol_spec is sent in the info message and validated by RemoteImpl
- For handles, the protocol_spec is included in the FxtrRPCHandle entity and stored in RemoteSubtargetImpl at creation time
- This allows protocol_spec() to remain synchronous on all impl types
