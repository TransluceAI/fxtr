Background: we just finished a large refactor of the fxtr2 codebase which adds:
- Major new primitives: a "lease", which gives you the right to access some data or some live object, and LeasedCapIR, which defines IRHandle/IRFuture classes that can be accessed via leases
- Significant refactor of internals so that all arenas are associated with leases and use them to manage their lifetimes
- An RPC system that allows IRHandle/IRFuture to be bridged across a transport, with leases managing lifetimes and ensuring they drain cleanly

There is also an existing system that bridges the low-level "arena" storage model with a user-facing FxtrScope / EntityRef system that will manage lifetimes for the user and help with type conversion. The FxtrScope owns the lease for its storage and manages it on behalf of the users while open.

Next step: Figure out how to do the equivalent "user-facing" version of IRHandle.

High level ideas about how this might work:

- Some analogous concept to EntityRef[T] that works for live capability handles. Maybe ServiceRef[T]?
    - Like EntityRef[T], these would be accessed through a scope, and their type variable would give type-safe access to the thing inside, but in a way where you sort of attach a type converter. They also transparently work through scopes, where child scopes get to look at parents.
    - Unlike EntityRef[T], ServiceRef[T] is not a reference to data stored in the scope, but instead a reference to an IRHandle that can be used to do something.
    - `await ServiceRef[T].resolve()` gives you a T which you can dispatch methods to, somewhat analogous to `EntityRef[T].load()`
    - you probably don't have as much flexibility with a ServiceRef[T] as you do with an EntityRef[T].
        - in particular you probably can't just store the T back in and get back an equal ServiceRef[T]?
        - and there's no such thing as "copying" a service from one scope to another, because they aren't data?
            - but maybe there's a way to _bridge_ an existing service ref, where a bridged ref keeps alive all the resources from wherever it came from? the semantics are different than a copy though because it actually keeps the source alive
    - however, there is still _some_ way to mount a service into a scope. I'm imagining something like `scope.mount_service(impl, as_type=T) -> ServiceRef[T]`.
    - so basically the way you thread a service through is something like
        - you instantiate an implementation of a service
        - you mount it to some scope, and define what interface it is supposed to support.
            - internally, this runs some conversion logic based on runtime type-based dispatch on `T`, builds an IRHandle that has a lifetime token that is attached to the scope's lifetime, and stores it + a lease for it on the scope
                - Probably a token owned by the scope itself; see later discussion
            - the returned `svc: ServiceRef` is then a lightweight object that wraps this IRHandle + the target type `T`
        - you then send that `ServiceRef` through whatever logic you have, possibly even over RPC (more details discussed later)
        - finally you end up in a scope where you want to _use_ the service. at this point you do `await svc.resolve()` to get a `T`
            - internally, this runs some sort of _reverse_ conversion logic, which gives you something that implements `T` by calling into IRHandle. the set of `T` that are actually allowed to be created here would depend on what you register as the "deserializer" for T
                - maybe there's a "default implementation" where T is a protocol with some decorator applied to it, and we return a dynamic implementation of that protocol by like wrapping all the protocol methods as IRHandle dispatches.
                - the conversion logic is _allowed_ to be async because it can do some RPC calls to set itself up. but for simple cases it might just be a simple cast that doesn't actually run any logic
        - the `T` that you get implicitly is bound to the specific scope that you used to resolve to it. methods on it all clone the necessary lease from the scope. once the scope goes down, the object cannot be used anymore. the service ref _may_ still be valid if it was mounted into some parent scope.

- Dep conversion: just as EntityRef converts to/from FxtrCbor, ServiceRef converts to/from LeasedCapIR's IRValue
    - This also applies "recursively"
        - In the case of EntityRef, the fields get converted using mashumaro's type-driven conversion, with generics propagated, when you call `load()`, and any EntityRef in there get their types bound by the original load, so that they recursively can be loaded.
        - In the case of ServiceRef, this happens _at method-call time_:
            - when you call a method on the result of `ServiceRef.resolve()`, your arguments get _dumped_ into IRValue, discarding the structure they have, but maybe still type-validating them (so that you can't just throw arbitrary data and have it dumped in some naive way)
            - when you receive the result back as an IRValue, it gets auto-converted back for you into the type that `T` was annotated as returning for that method
    - Roughly, FxtrID gets represented as tagged bytes, and ServiceRefs themselves get represented as tagged pairs of IRHandle and a Lease to use it
    - This conversion could probably be handled by mashumaro, possibly with a somewhat different set of default serialization strategies? or a different dialect? we will need to scope out exactly what the behavior here should be.

- RPC mechanics:
    - Once we are down to IRValue level, it should basically flow directly through with IRValue semantics
    - One thing to be careful about is leases, but this should hopefully work "normally"
        - If you pass one service ref into another, that becomes passing a lease as a call arg, which is allowed
        - The lease is taken by the other side and kept alive as long as it needs it; usually it will be discarded by the end of the call, but there is flexibility to do something more sophisticated

- A scope that has things mounted to it will need to stay alive until all of the leases to its mounted entities are no longer used
    - It would discard its own copy when its scope exits
    - It would then have to wait for all other copies to finish before it drains, following normal lease semantics
    - This probably motivates having a _separate_ inner token for the mounted RPC values, so that the scope can watch for it to close and only finish the scope when that happens
    - One design that might be worth doing to enable this: replace the current keepalive system with a lease-based system for scopes themselves, separate from the lease system for the memory store. then this lease would represent the lifetime of the scope's own resources, and would be where we would attach the mounted services; scopes would then keep each other alive because they would wait for their token to drain before they shut down, and they would hold ordinary leases on their parent scopes rather than a separate KeepAlive structure. Their own context manager would just wrap the normal unconditional lifetime async context manager.

- How to return a ServiceRef from a ServiceRef and wire it up properly?
    - (in particular, suppose you have a spawner service that spawns other services)
    - I think basically this would be handled by the "when you receive the result back as an IRValue, it gets auto-converted back" component from above
        - when we receive a tagged (handle, lease) pair from a call, we attach that to our own scope
            - (implies that we do need the ability to bridge somebody else's lease into our scope? the scope would adopt this lease, it possibly could just be held independently of our main token and just closed afterward. however, worth thinking through this in the context of attached tokens, discussed next)
        - then we wrap that in a ServiceRef with the type it is supposed to have
    - Desired behavior: when you run a service in a scope and it returns a new service, that new service is now attached to the same lifetime of the scope you ran it in / bound it to.
        - This mirrors loads: when you load an entity, you get tracked access to all the subentities in the entity
        - General rule: if you have a capability that imitates a fxtr ref (it is a cap that, when you call a method, loads the data and also returns new refs), then it should behave in the scope the same way fxtr data behaves

- Scope and tracked data interactions when mounting as ServiceRef
    - When you mount an impl into a scope, that scope would become the parent scope for all method calls
        - Each method call would run in a new active scope that is the child of the mount-scope
        - New data that is provided in arguments, as well as new services passed in as arguments, would automatically bridge into the scope
            - Some bridge capability which you can use to build an areana
            - Lease on the bridge capability
            - Leases on all of the other args, converted in via the symmetric arg-convert logic (this time using the type annotations of the impl?)
        - On return:
            - Data that was loadable in the scope and returned by it would get bridged back to the caller
            - Similarly, service refs that were visible in the scope would get bridged to the caller
            - This might or might not directly keep the scope itself alive? it would require keeping alive the scope's own token and anything it can reach
                - Scope mutation APIs would close (maybe this happens immediately when you "try" to exit a scope)
                - Resources would continue until cancelled
            - Maybe this scope is just a magic scope that doesn't use the context manager; we just abandon its lease ourselves and send the last clone of it back over the RPC channel, and then let it clean itself up via the callback (the lower level API)
                - Matches other lifetime resources where the contextmanager is just for convenience and doesn't have to block the program

- Thinking through in detail how ownership actually works with lease, and how servicerefs and entityrefs get represented at a low level and get fed through correctly with correct ownership:
    - Each scope has a token representing its lifetime. The token stays open throughout the scope, the scope closes the root lease and then drains.
    - This token is either identical to or derived from the memory store token
    - Existing mechanics for data:
        - You store new data using `scope.store` or similar. This gets bound to your scope's token in particular
        - You can also bridge foreign data in by providing an arena, a key lease, and an optional converter.
    - Analogous mechanics for services:
        - You bind new services using `scope.mount` or similar. This gets bound to your scope's token in particular
        - You can also bridge foreign services by providing a handle, a key lease, and an optional converter.
    - Implication:
        - Scopes would store the set of services that are considered tracked in the scope
        - Scopes (not ServiceRef) would own the leases for them
        - Probably a separate set of owned handles (which use the scope's own lease and don't need to store it separately) and inherited handles (which have a key lease and a converter from the scope's own lease)
    - How this would bridge through IRHandle calls:
        - Roughly we need a new calling convention for IRHandle calls that are wrapped in ServiceRef
        - Some tag for the top level call and result to make this safe
        - Calling convention is something like:
            - Caller provides:
                - The user's provided argument, dumped to IRValues, with servicerefs dumped to IRHandle and FxtrID dumped to tagged bytes
                - if there are any FxtrIDs in the argument, a fresh arena-as-IRHandle containing all of them
                - A set of leases for all tokens needed by the other args (called the "input token set")
            - Callee returns:
                - The function's result, dumped to IRValues, with servicerefs dumped to IRHandle and FxtrID dumped to tagged bytes
                - if there are any FxtrIDs in the result, a fresh arena-as-IRHandle containing all of them
                - Leases for all tokens used by something in the result:
                    - For tokens that predate the call scope: A lease for those tokens
                    - For tokens that do not predate the call scope (e.g. the call scope itself):
                        - a "key" lease that can be used to run the converter
                        - a converter IRHandle which takes a new set of for the "input token set"
                    - For simplicity, represented as a key-lease per token + a converter which may be None

Things we have to do:

1. Memory store token refactor:
    - Make memory store no longer responsible for owning its lifetime token. Instead, it _receives_ the token, and leases for the parent tokens, and just subscribes to it
    - Essentially the memory store just promises to keep alive the resources associated with the token until the token lifetime closes

2. Attached => derived lifetime refactor:
    - Generalize "attached lifetime" from one attached token to a set of tokens, and rename it "Derived": to do exchange forward, you pass in a _map_ from req token to req lease, and you get one copy of the attached token; then in reverse you can take the attached token and break it back out into a map of its consituent tokens
    - No more need for a dedicated key token; a key token is just an arbitrary token in the map that is designated as such, and is only used to manage this output. (Reverse mapping would then re-instantiate the key token in this case)
    - Output token ends as soon as _any_ constituent token ends
    - Change call sites for it so that they use the new signature

3. Scope token refactor:
    - Remove keepalive system and replace it with owning a fresh token and using the token drain logic
        - Each scope owns its token lifetime and gives out clones of the root lease for its token
            - By default scopes use an unconditional lifetime token and store its root lease
            - However, there is an alternative low-level constructor that configures it to make a derived lifetime token for some set of derived leases, with an additional unconditional lifetime token serving as its key
                - In this case, while open, the scope holds both its root key lease and an output lease (by forwarding all of the derived leases through)
        - Scopes can be used in a non-contextmanager way:
            - Scopes are immediately active even before being started as a context manager
            - Scopes have an `await aclose()` method which closes and drains them: specifically, `aclose` drops the main lease, waits for token to end, then returns, OR, if interrupted or cancelled, revokes token immmediately.
                - For unconditional: drop the root lease, then wait for root token to end.
                - For derived: drop _both_ the root key lease _and_ the saved output lease, then wait _only_ for the output token to end. Do not wait for the key token to end, it is free to dangle.
                - In both cases: revoke lifetime if cancelled / errors out.
            - Then the async context manager flow is exactly the same as calling `aclose` in a finally block
            - Methods directly on the scope (e.g. storing, loading) don't work as soon as the scope starts to close, because conceptually the scope enters a "drain" phase as soon as it starts to close. However, child scopes can still call into their parent scopes by providing their own copy of the lease (maybe they will need to call a private scope-internal method that accepts an explicit lease for these cases)
            - For consistency, we can also configure this pattern for some of the other objects that have an `__asynccontextmanager__` that drains them (e.g. the local lease lifetimes themselves)
        - When used as an async contextmanager, scopes just `aclose` themselves in a finally
    - Test for the derived case:
        - In task 1: create a normal scope, store some data, extract an arena with the token associated with the scope. Send it to task 2 along with a lease. Abandon the stored data.
        - In task 2: create a derived scope, derivative of the first scope's lifetime. Then bridge in data from task 1 via the reverse conversion (binding it to the memory store with STATIC_LIFETIME for the key lifetime and the reverse conversion logic to unwrap). Then store three new entities that all depend on the data from task 1. Extract a new arena containing the new data. Send (key lease, converter, new arena) to both task 1 and 3. Drain the scope
        - Back in task 1:
            - Take all three outputs, and bind them into task 1's original scope, using the converter.
            - Load one of them, and load through it to re-load the entity that was originally stored in task 1
            - Send a lease for task 1'as scope to task 3. Then drain the scope.
        - In task 3:
            - Combine task 1's lease with task 2's key and converter to get a new lease for task 2's scope. Use it to mount the data from task 2 into a new temporary scope. Then load the second entity through it, which should bridge through to task 1's data automatically. Then exit the temporary scope. Nothing should close.
            - Drop the output lease for task 2. Nothing should close.
            - Then, drop the main lease for task 1. _Both_ task 1 and task 2's output tokens should close, and the tasks should exit drain and exit the scope context manager. Task 2's scope key lifetime should still be alive (but useless now, because it just gates a now-useless converter) but the task itself is done.
            - Then, drop the key lease for task 2. Task 2's key lifetime should close.

4. Arena-over-CapIR implementation
    - Arena-as-CapIR
        - Create an IRHandle implementation that wraps an arena and implements an API like the HTTP bridge (with precomps etc) or the backend side of a SQLite arena,  but returns the values as IRValue expressions, and also _includes_ the ability to abandon nodes (with the abandon signal actually dropping them on the "server" side). The token for the IRHandle should be the same as the token of the wrapped arena, for simplicity.
        - Create a symmetric Arena implementation that wraps this IRHandle to implement its own API, essentially structured like the client arena for HTTP or SQLite, receiving and integrating the precomp sets.
            - Instances of this Arena hold:
                - their own token `Z`
                - an input token `X`
                - an IRHandle with token `Y` ("remote token") representing the remote arena,
                - an IRHandle with token `K` which is a _callable_ that takes some input lease for `X` and produces a lease for `Y`. optional, if excluded then we use `X` as `Y`
            - The arena key `Z` must be either:
                - a derived token that is derived from `X` (and also `K` if applicable).
                - `X` itself (only allowed if there is no conversion callable)
            - Methods on the arena use dispatch pipelining: they dispatch on the conversion handle, then immediately pipeline this result as the lease for the IRHandle representing the remote arena (this should hopefully work, we may have to adjust IRHandle dispatch semantics slightly)
            - (one question: should the converter and no-converter versions be the same class or different?)
    - Test for this, derived from the above but with the restriction that all communication is through IRValues
        - In task 1: create a normal scope with token `A`, store some data, extract an arena with the token associated with the scope. This time, turn it into an IRHandle, and send that IRHandle along with a lease. Abandon the stored data.
        - In task 2: create an outer key token `B`. Create a derived scope owning a new token `C`, derivative of task 1's token `A` as well as of `B`. Create an arena wrapper with the derived token, using the IRHandle (with A as X == Y == Z) and mount it into task 2's scope. Then store three new entities that all depend on the data from task 1. Extract a new arena containing the new data, and convert it into an IRHandle with token C. Also create a converter with token B, which wraps an A->C conversion. Send (B lease, converter handle, new arena handle) to both task 1 and 3. Drain the scope.
        - Back in task 1:
            - Take all three outputs, and bind them into task 1's original scope, by:
                - Creating a new derived token `D` derived from `A` and `B`
                - Building the client arena with `D` as its own token, `A` as the input token, `C` as the equivalent of Y, `B` as the equivalent of `K`, and the remote converter
                - Binding that into task 1's scope via a lease for `B` + the forward conversion fn to combine it with `A` to get `D`
            - Load one of them, and load through it to re-load the entity that was originally stored in task 1
            - Send a lease for task 1's scope to task 3. Then drain the scope.
        - In task 3:
            - Combine task 1's lease with task 2's key and converter to get a new lease for `C`, and wrap it into an arena (with `C` as X == Y == Z)
            - Use it to mount the data from task 2 into a new temporary scope. Then load the second entity through it, which should bridge through to task 1's data automatically. Then exit the temporary scope. Nothing should close.
            - Drop the output lease for task 2. Nothing should close.
            - Then, drop the main lease for task 1. _Both_ task 1 and task 2's output tokens should close, and the tasks should exit drain and exit the scope context manager. Task 2's scope key lifetime should still be alive (but useless now, because it just gates a now-useless converter) but the task itself is done.
            - Then, drop the key lease for task 2. Task 2's key lifetime should close.

5. ServiceRef fundamentals, with UnknownService
    - Goal here: implement the basics of how servicerefs will work without any of the fancy type conversion, but with the basic mechanics in place
    - Pieces:
        - A scope can either be "mountable" or not, configured with a constructor param. The main difference is that a "mountable" scope:
            - Is not considered active until `__asynccontextmanager__` enters (in particular, `mount` does not work)
            - Creates an anyio TaskGroup and holds onto it through the duration of the contextmanager
            - Tears down the task group _after_ the token ends or is revoked (so that anything using the token can freely use the task group)
        - Scopes get a method `def mount_lowlevel_cap_as_service(handle: IRHandle[B], service_type: type[T]) -> ServiceRef[T]` which takes a handle whose token is the scope's own token, and mounts it as a FxtrService. (Error for non-mountable scopes.)
        - Scopes get another method `def attach_leased_lowlevel_cap[B2, K](handle: IRHandle[B2], service_type: type[T], lease: Lease[K], converter: LifetimeConverter[B, B2, K] | None) -> ServiceRef[T]` which mounts an existing IRHandle as a service. The key lease is held until (after) the scope closes. (Error for non-mountable scopes.)
        - Define a base class `FxtrServiceImpl[T]` which is kind of like the Entity base class, where the requirement is it has a method `def _mount_as_fxtr_service(self, scope) -> ServiceRef[T]`
        - Scopes get a method `def mount(service_impl: FxtrServiceImpl[T]) -> ServiceRef[T]`
            - Just calls `_mount_as_fxtr_service` with itself as the scope
            - (Error for non-mountable scopes.)
        - Type registry (similar to how the entityref registry works for deconversion) that lets you register async converters which take the scope, an `IRHandle[B]`, a callable `Callable[None, Lease[B]]`, and the target type `T` and produce a value of type `T`
            - By convention, the callable should not hold a persistent copy of the lease at rest, but re-fetch it when needed
        - `ServiceRef[T]` has a method `async def resolve(scope: FxtrScope | None) -> T`
            - This then calls into `scope.resolve_service(svc: ServiceRef[T]) -> T` which runs the async converter on the internal `IRHandle[B]`
            - The lease callable we provide to the converter is derived from the scope, and works until the scope starts to drain and then is no longer usable at that point
        - A specific type annotation `type UntypedService = Any` with a registration
            - The converter ends up mapping to a UntypedServiceProxy class
            - It accepts arbitrary method names, and arbitrary combinations of FxtrCBOR primitives, EntityRefs, as well as ServiceRefs as arguments, and simply throws away the EntityRef and ServiceRef type info, representing them as IDs
            - It internally handles all the scope-bridging logic described above (in particular, forking out a new arena from its source arena, turning it into a handle, grabbing leases for all the args, packaging it up, sending it, receiving the result, parsing out the components, and mounting any returned IDs back into its bound scope)
                - It always uses its own "home scope" (the scope where `resolve` was called) for method calls and requires all args to be tracked there
            - It returns FxtrIDs and ServiceRef[UntypedService] only
        - A specific base class you can use to create a simple service
            - Called `UntypedServiceImpl(FxtrServiceImpl[UntypedService])`
            - Method `def _service_exposed_methods(self) -> Collection[str]` listing the methods that are intended to be remote-callable; inferred to be "every method that doesn't start with an underscore" if not provided
            - Implements `_mount_as_fxtr_service` by
                - creating an IRHandle impl which wraps itself, and attaches scope semantics for each dispatch:
                    - Start and wait for a new async task in the task group (waiting for it to signal readiness)
                    - In the new async task:
                        - Start new mountable scope with a token derived from all tokens in the input + with the mount-scope as the parent scope
                        - Set that scope as active
                        - Mount in the IDs in the args via the provided arena handle, wrapping it like the "task 2" case in the above tests but using the full calling convention
                        - Mount in all the other services as well
                        - Convert args up to FxtrIDs and ServiceRef[UntypedService]
                        - Call the method (in a try/finally)
                        - If it returns successfully, package the result, create a new arena with the results, extract new mounted ServiceRef in the scope, and do the other basic things like how results are returned out of "task 2" but using the full calling convention. Then send that back to the original call
                        - Then drain the scope normally
                    - Back in the original dispatch impl, wait and receive the final result from the task, then submit it as the completion of the task. (Any leases in this result will keep alive the async background task, so they can be used by the caller.)
                - then mounts that handle to `mount_lowlevel_cap_as_service`, with `service_type=UntypedService`
    - At this point we should have a full implementation of the mechanics of cross-scope RPC, and we can write tests that involve a number of scenarios:
        - passing fxtr data in and out of mounted services
        - passing services as arguments into services and using them there
        - mounting and returning new services inside a service impl

6. FxtrScope RPC API
    - In this step we can take the service system and bridge it with the RPC system
    - How: A context manager which takes one ServiceRef (for our main interface) + an ObjectStream, and creates a new scope where there is another attached ServiceRef representing the remote main interface
        - Essentially just wrapping the existing RPC connection logic with the new ServiceRef layer
        - RPC context always creates a new scope which is nested inside the RPC block; the new scope drains before the connection goes down (which happens before our main interface tears down from its parent scope)
    - Test to make sure this works:
        - Two sides both set up a connection over a JSON-marshalled object stream, with some top-level main service of both
        - In task 1's scope, mount a new service, then send the new service to some method of the task 2's main interface
        - In task 2's call, mount another new service that wraps the new service it got as an argument, and return it
        - Back in task 1's scope, retrieve the returned service and invoke it (which should call back and invoke task 1's mounted service)
        - Let all the scopes drain; this should work

7. Serviceref conversion via mashumaro
    - Builds on the fundamentals from above
    - However, extends it with mashumaro-driven per-method type conversion based on the type annotations of the methods, with the restriction that method-level type params are not allowed (but class-level type params are OK)
    - Idea is that you decorate a Protocol class with a conversion decorator which then registers a converter for that protocol as the target type, which is implemented by a `UncheckedTypedServiceProxy`, which acts like a `UntypedServiceProxy` except that it de-converts the args first, and converts the return value
        - (maybe we refactor to share the internals)
    - Then conversely there is also a `UncheckedTypedServiceImpl` which you can subclass, which does the same thing but on the other side (as the call target)
    - Together this should make it possible to have a typed API on both sides of the RPC boundary
    - It is "unchecked" because neither side checks that the service types match, they simply do conversion at runtime
