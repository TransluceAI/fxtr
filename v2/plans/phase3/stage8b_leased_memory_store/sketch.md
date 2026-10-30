Goal: Redesign the memory store, memory arena, and scope system so that it interoperates with the new lease system in @fxtr2/leases/LOCAL_CODE_OVERVIEW.md

What this means:
- Right now:
    - Memory store owns a bunch of data and tracks lifetimes using its own concept of tokens. Tokens have a DAG relationship and can be invalidated.
    - Memory arenas operate in terms of these tokens. Each arena has a token and uses it as the backing source for its data
    - Scopes create new tokens. Scopes also manage their own lifetimes, and ensure that they stay open as long as their data is needed; tokens themselves don't do this.
    - Mounting of external arenas into scopes is unsafe; the scope creator must guarantee the data outlives the scope.
    - Arenas have no notion of lifetime, in general; its assumed that the caller knows what they are doing.
    - There was an attempt at plumbing arenas through RPC calls but it ran into fundamental lifetime issues due to the above.

- Separately:
    - New lease system exists
    - Leases give a general mechanism (and recommended pattern) for dealing with resource ownership and keeping resources alive: resources are supposed to stay open until either all leases are discarded or until the "grant" is revoked, at which point all leases break and callers immediately can tell
    - Leases will also be used to manage lifetimes of resources sent over RPC in the near future.

- Desired end state:
    - Roughly, redesign how the memory store, scope, and arena system works so that scopes have the same user-facing API, but internally all lifetime management is managed via tokens and leases
    - Specific notes:
        - Memory store should allocate unconditional lifetime tokens, hold onto their lifetime owners, and use those instead of its own existing tokens. Memory store can still manage the DAG relationship itself. Memory store also is responsible for actually managing all the resources that back the tokens.
        - Instead of memory store having scopes call a method to tell it to tear down a token, memory store instead registers this as a cleanup on the new lifetime token; it will then tear down as soon as there are no leases to it.
        - Memory store modifies things so that, alongside the DAG relationship, it stores actual backing leases. so, as long as token A is alive, if token A depends on token B as a parent, the memory store holds onto a lease for token B keyed by token A. This ensures resources clean up in the right order, and token A keeps token B alive automatically
        - _Every_ arena now comes with a token that gates its lifetime, accessible via `arena.token`. All methods on the arena require passing a lease to the token.
        - Memory arenas forward their lease to the memory store, which uses the lease's token to identify where to load/store from. Memory store methods (e.g. bare_store) all change to take in a lease, rather than a (old-style) token.
        - Scopes now request a new token from the store and then hold onto _both_ an arena _and_ a lease for it, which keeps the resources alive. This also means that scopes do _not_ need to manage their own lifetimes and do not need separate keepalive behavior. Ending a scope simply closes the arena and then discards the lease (allowing the data to be reclaimed if this is the last reason for holding it open). Unwinding of scopes becomes free. This _is_ a behavior change but it only means scopes can exit preemptively. Every scope now has a sort of hybrid between being a normal scope and a weak scope: they don't auto-close when parent closes, but they also don't block exit. Scoping of the user code is simply decoupled from the lifetime of the backing resources in the memory store. `new_weak_child_scope` goes away. (Scopes continue to have parents, which first go to the memory store for figuring out tokens, but then subsequently are just for cache resolution.)
        - The `unsafe_lazily_attach_from_arena` API becomes a safe `lazily_attach_from_arena` API. External resources, such as FxtrNamespace subclasses and the HTTP read-only namespace, and really anybody who unsafely mounts their own arena into a memory store's arena (with token `X`), needs to use the new API.
            - Using it requires you to provide all of
                - The entity ID to add
                - An arena object with possibly a _different_ token `Y` than the memory arena they are attaching to
                - A lease for a token `Z`.
                - A "lifetime converter": a function which takes (and consumes) two leases, a conversion lease to token `Z` and a lease to the memory arena's token `X`, and returns a lease for `Y` that can be used to access this arena. This signature is annotated as a generic Protocol with `__call__`.
            - As a common special case, it is allowed for `Y == Z`; the default value of the API uses a lifetime converter that discards the `X` lease and returns the `Z` lease as `Y`.
            - The memory store stores this as the parent info, and uses the converter to lift its own internal lease to a lease for the foreign arena so that it can load it
            - Most (all?) existing external storage systems will use an UnconditionalLocalLifetime via `__asynccontextmanager__` to create their token and root lease; this then ensures that the resource stays open until it is no longer needed
            - It is allowed to use a AttachedLocalLifetime in which case `exchange_forward` would be the lifetime converter fn and `RB_Key` would be Z; this is intended for use for bidirectional settings where we lazily send info in both directions. We should add at least one test for this
        - For now broken lease errors can just surface wherever they happen to surface; we don't preemptively subscribe to break notifications
