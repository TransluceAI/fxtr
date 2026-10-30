
current state:
- at a low level, resources are managed by tokens
    - having a lease on a token gives you access to the resource
    - once all leases on a token expire, the token closes
    - the owner of the resource can use the token to manage graceful resource cleanup in the style of structured concurrency:
        - ordinarily the resource owner waits for the token to close, and then cleans up resources that are gated by the token
        - if the resource becomes unavailable unexpectedly, the owner can (and should) revoke the token to inform consumers it is no longer usable
- some tokens are atomic, and represent an independent lifetime. other tokens are _derived_ and represent the intersection of multiple other lifetimes. this enables common useful patterns:
    - if resource A is only valid while B is valid, we can construct a A-specific token K, then make a derived A=K+B token, and use that to gate access to the resource; K represents a claim on the ability to convert B into A. once A closes, A-specific resources can be discarded, which will happen when _either_ B or K becomes inaccessible.
    - if resource A is given B later and is supposed to keep it alive, A's resource owner can just hold onto a lease for B and clean it up after A's token closes
- scopes then add additional features on top of this system and are designed to be easier to use for end users and hide the details of tokens.
    - each scope manages two things:
        - data (where the token represents the ability to access this specific data + all data referenced by it, which might be owned by other tokens)
        - services, which are each running for the lifetime of some token
    - in the common case, a scope grabs and holds leases on some parent scopes, provides some resources, and then _drains_ when executed, blocking until there are no more consumers of the scope's token before cleaning up gracefully
    - however, the semantics here are complex and there are many entry points. there is an internal helper to make a derived scope that does not capture parents. scopes can be mountable which gives them a task group but this is optional. once a scope drains you can still access it manually via low level lease-driven methods but these are not exposed to the user. scope opening semantics are somewhat confusing
    - also mechanisms setting provenance when a scope opens, but this then sometimes overridden separately
    - difficulty of the current setup: if you mount a service into a scope and then the scope drains (common in RPC flows), the service impl has no way of using its parent scope itself anymore, or of managing its own lifecycle relative to its parent scope. the mechanism by which an RPC call creates a child scope for its dispatch is complex and requires reasoning about system internals, and it is not possible to replicate similar functionality externally.

goal: clean up the lifetime semantics for scopes and make it a well-designed and "public" API surface, in such a way that services that need to manage long-living resources can use their parent scopes to do so.

new clearer conceptual model of scope lifetimes
- a given scope can be in a number of lifespan states:
    - open: the scope's resources are alive and can be used by anyone who has the lease. this is a scope-level property. scopes start out open and then close once and never reopen.
    - authorized: the current async task / context has a lease to access the resources in this scope. this is a _per-scope-contextvar_-level property. a scope can be authorized in some async tasks and unauthorized in others. an unauthorized scope can become authorized if you have its lease and provide it in a contextmanager.
        - authorization (with a live lease) is what is needed to do most scope operations
    - within-lifespan: the scope's lifetime is coupled to a context, which can enter _new_ lifetime context managers on behalf of this scope, and ensures that either (a) they tear down _after_ this token ends, or (b) they revoke the token if an exception causes an early teardown.
    - active: this scope is the "current" scope in the task. this is a _global-contextvar_-level property. at most one scope is active at a given time. the active scope is (almost) always authorized
- normal scope ctxmgr registers as within-lifespan + open + authorized, then at ctxmgr close loses authorized + drains until closed + + notifies all child lifespan taskss to close + finishes task group.
- to simplify things, every scope entered as a context manager has a lifetime and is thus mountable (so, no separate `mountable` arg)
    - scope builds an anyio task group on entry, and holds a "closing" event
    - public async method to attach a provided lifespan context manager to an authorized scope: what this does is start an async task in the task group which enters the context manager then signals readiness and awaits the close event; `await tg.start(...)` then ensures the context is entered before the attach fn yields
- ideal behavior, replacing current open/close mechanics:
    - a scope starts out not-yet-open, not authorized, not active, not within lifespan. it also doesn't even take a lease on its parents or create its token, it just sits there. if it gets GCed nothing happens, nobody is waiting for it. no scope methods work at this point
    - normal route: enter the scope as an async context manager
        - this starts a task group, actually creates the token (as derived from parents + a key, for uniformity we keep this structure even if there are no parents in which case it is derived just from a key) and enters its lifetime owner context, binds lease to the scope authorization contextvar (=> making it authorized), maybe marks active (if not isolated), yields.
        - on control flow exit (either path): discard the authorization lease, wait for lifetime token to drain, _then_ set the lifespan event (triggering all lifetime contexts to close), then wait for task group to finish
    - non-mountable route: (rarer, but used for e.g. root scope)
        - method `lowlevel_start_lifespanless()`: create the token, bind lease to scope authorization contextvar, yield. _no_ task group here.
        - method `lowlevel_force_close()`: force the token to end (which also breaks any open lease including authorization one)
- all child scopes become derived scopes; no more need for a derived constructor. scopes always allow rebuilding their lease from parents while open and not drained.
- RPC dispatch uses this mechanism to reactivate the mount scope during fn calls, it is legitimately authorized while those calls run.
- service impl helpers let you define a `_fxtr_mount_lifespan(self, scope)` handler and it runs in the lifespan TG and tears down _after_ token closes. long-running tasks can store stuff in their mount scope and use `_fxtr_mount_lifespan` to manage it. scope is authorized before `_fxtr_mount_lifespan` yields but becomes unauthorized afterward
- bridging a service from a scope now requires an explicit `keepalive_via`:
    - "independent_lease": neither scope descends from the other, and the source scope's lifetime is NOT structurally inside the dest scope's lifetime (in the sense that async execution should allow the source to finish draining and close _after_ the dest closes), and the dest will keep the source alive until after the dest closes
        - (or we could have separately a "concurrent_independent" and "nested_independent" with different rules for each)
    - "concurrent_descendant_bridge": the source is a descendent of the parent, running concurrently (neither scope's lifetime dominates the other, so that the source can safely finish draining and close _simultaneously with_ the dest), and the parent will arrange for the two scopes to close simultaneously (keeping both scopes alive until both drain)
    - we check the descendant property ourselves. the structurally-inside property is difficult to check naively so we can skip this but we could add a TODO to consider a stricter mode
