This package is a place to sketch out an idea for a workflow management system. If it works, this design will be merged more fully with the fxtr system for content-addressed storage and provenance tracking. However, we are starting by prototyping the core ideas in a separate package to avoid having things couple together too closely, so that we can focus on testing the new bits and worry about the integration later. This sketch will make partial use of fxtr:
- The workflow system will itself use fxtr to store its state and compute hash keys (via FxtrID hashing). It will also use fxtr's RPC system to plumb capabilities through the system.
- However, the internal steps will _not_ use fxtr initially.

The central idea is to do something like a durable worflow system, but where the provenance of every result of every step is centrally tracked and managed. Some notes on the design:

- Two types of step function:
    - External step function: Explicitly tracked, provided by the entry point when you launch the system. Required to be stateless but may be nondeterministic. We simply assume the user knows what they are doing with these; the provenance chain points at "the user"
    - Managed step function: Launched and managed by fxtr itself, and the provenance is tracked by fxtr. Three types:
        - Python package, packaged to wasm via Eryx
        - Docker container image, for more flexibility
        - Eventually, Typescript or javascript package, packaged as an esmodule and run via deno
        Managed step functions are fully determined by content-based hash of their configuration, which includes as part of itself all files that are needed to uniquely specify what will run

- Data model:
    - Primitives (string, bool, float, int, None, bytes)
    - Cache slots: a special object with methods on it (described below)
    - Step handles: special objects used to identify a type of step to run
        - Two types, internal and external step handles.
        - External step handles are not "forgeable" and always have to be passed in as an argument at runtime to be used. They are indexed by an opaque fxtr entity that is used as a key, and should describe what the external resource is (e.g. if it is an authenticated model provider, the key should specify which model provider it is).
        - Internal ones can maybe be "compiled in" together so that you can get one as an explicit constant at the program level; TBD exactly how this works. If compiled in, the value of the internal one is part of the content-hash of the outer one, as a link
    - Lists and dicts of these
    - (Eventually we will allow network references to be passed around but not initially)
    - (Eventually we will allow fxtr entities to be constructed and passed around but not initially)

Interfaces:
```python
class StepContext(abc.ABC):
    """Context object. Always available inside a step.

    Used to dispatch to other steps, and to handle workflow state. Every step context implicitly
    has some storage that is used to store its results and cache intermediate outputs. If a step
    is being explicitly run inside a cache slot, that cache slot owns its state. If a step is being
    run _uncached_, its state is stored in some temporary location that is not reused (and is only
    there to make the API work the same way in the uncached setting).
    """

    @abstractmethod
    async def run_uncached(
        self,
        step: StepHandle[P, R],
        *args: P.args,
        **kwargs: P.kwargs
    ) -> Pending[R]:
        """Run a step without any intermediate caching.

        If the containing step is replayed, this call will be run again and may produce different
        results.
        """
        ...

    @abstractmethod
    def cache_slot(self, key: str | int) -> CacheSlot:
        """Construct a cache slot.

        Cache slots are used to make workflow execution durable and to enable reuse of results
        across modified experiments. This gives a reference to a per-context cache slot.
        """
        ...

    @abstractmethod
    def save_checkpoint(self, data: ConcreteTypes):
        """Save some temporary data into this step. Subsequent operations may restore this."""
        ...

    @abstractmethod
    def load_checkpoint(self) -> ConcreteTypes | None:
        """Load the most recent temporary data saved by this step.

        Can be used to restore progress after a crash even if that progress was not already cached
        by using `cache_slot`.
        """
        ...

    @abstractmethod
    def log(self, msg: str):
        """Log a message."""
        ...


class CacheSlot(abc.ABC):
    """Cache slot. Maintains the state of a sub-operation.

    Cache slots always have a primary storage location (used to hold the results of their run) and
    subslot storage locations (which are retrieved by either using `subslot` or using `cache_slot`
    inside the slot's `run`, which do the same thing).

    Internally, each cache slot also maintains a replay safety key that can be used to determine
    whether another invocation of the run is "the same" as a previous one.

    Cache slots can be in one of four states:
    - Empty: Nothing has been done with this slot or its subslot tree.
    - Split: This slot is broken into subslots, rather than containing a single run.
    - Partial run: This slot is being used to run a step, and that step has not yet completed. A
        partial run may contain internal cache slots due to use of step_ctx.cache_slot. It may also
        contain checkpoint data. Note that the internal cache slots are similar to normal subslots
        in terms of how they are stored, but they are _not_ identical; you cannot access these
        slots via `subslot`.
    - Finished run: This slot is being used to run a step, and that step has completed. Finished
        runs always contain an output. A finished run may also contain internal cache slots due to
        use of step_ctx.cache_slot. Finished runs do not contain checkpoint data, and all data
        within them is considered immutable. Finished runs also keep track of every other external
        cache slot that was passed in as an argument, and the finished run state of every run in
        that cache slot that was requested inside this step.

    This means cache slots can also either be used to store a single run, or to have a subtree of
    subslots, but never both. It is an error to call both `run` and `subslot` on the same slot.

    During a workflow execution, cache slots are considered "write once". This means that once
    a slot has been filled with a partial run, the only allowed action is to update the partial
    run state or transition to a finished run. Similarly, a slot being used for one run cannot
    be used for a different run (although if a request is made for the _same_ run it is allowed to
    reuse its state and result).

    After a workflow execution fills out a cache tree, you are still allowed to add _more_ outputs
    into the root cache tree. However, it is not allowed to remove or modify any without clearing
    the entire cache. To enable partial reuse, it is possible for a cache slot to internally track a
    "replay reference slot" which is another cache slot of the same shape whose results we are
    replaying. Replay reference slots can also be in a few modes:
    - Strict replay: It is an error to run any step that does not match the step in the replay
        reference, as if we had already committed to running the same step.
    - Best-effort replay: If we happen to run the same step, we copy the result from the reference.
        If it diverges, we drop the reference slot and save results as new.
    - Migration: Even if we do not run the same step logic, we _still_ copy the result as if the
        result was cached. In this case we modify the provenance record of the result in the cache
        to note that it was migrated.

    Cache slots storing a partial or finished run keep track of a cache key that identifies the
    step being run. This is based on a hash of the args to the step:
        - Primitives are compared by equality
        - Containers are compared by structurally running this comparison across the children
        - Internal step handles are compared by equality
        - External step handles are compared by the name they were registered under (note that
            external step handles also pass around a UUID for non-forgeability purposes but this
            is not used to compare cache staleness)
        - Cache slot arguments are assumed identical for cache key purposes but have additional
            checks for replaying described below.

    If a run of a step is requested in a slot, and the slot already has a _finished_ run for the
    same cache key, we do a cache-replay check to see if we can safely reuse the results. This check
    is an additional check that applies to all cache slots passed into this step as arguments:
    - Look up all recorded cache-slot requests that occured in the finished run we have, and the
        finished run state of each.
    - For each, check if the value at the corresponding _current_ cache slot arg has the exact same
        finished run state.
    If all of these match, the finished run's result can safely be reused. Otherwise, this is
    an error if the finished run came from this cache set itself, or from a replay reference in
    strict mode. In best-effort mode, we treat the existing finished run as if it was a partial run
    with no checkpoint state, and reexecute it.

    If a run of a step is requested in a slot, and the slot has a _partial_ run for the same cache
    key (or a full one that failed cache-replay check in best-effort mode), we execute the step
    again. However, during the execution of that step, we provide the cached results of substeps,
    so that those substeps can be reused. We also provide the most recent checkpoint from the
    previous execution, so that steps that use custom checkpoints can recover partial work.
    """

    @abstractmethod
    async def run(
        self,
        step: StepHandle[P, R],
        *args: P.args,
        **kwargs: P.kwargs
    ) -> Pending[R]:
        """Run a step inside a cache slot.

        This schedules and dispatches the given step to run in this cache slot's storage.

        If two concurrent requests to `run` are made in the same cache slot with the exact same
        cache key, the second request blocks until the first is done, and then does the normal
        cache-replay check, ideally re-using the first one's result.

        If two concurrent requests to `run` are made in the same cache slot with different cache
        keys, the second call is an error.

        If an uncaught exception occurs inside the step being run, this error will not be saved into
        the cache but will instead automatically propagate up through the call stack as a
        BaseException and abort the workflow. The exception raised here should NOT be caught by this
        step; instead, the workflow script should be fixed and retried. Any completed steps or
        saved checkpoitns will be preserved for the next execution. Steps should be written to
        tolerate being retried after an unexpected error. (This behavior is needed so that errors
        do not corrupt the cache and can be retried on next execution even if called inside other
        steps. If you intend to catch errors and handle them without failing the workflow, see
        `run_fallible`.)

        It is allowed to pass cache slots as arguments to the step; these will be shared with the
        remainder of the program and obey the same rules. The main purpose of doing this is to
        encapsulate an existing script into a step without breaking its cache and without preventing
        cache re-use across parts of a step.
        """
        ...


    @abstractmethod
    async def run_fallible(
        self,
        step: StepHandle[P, R],
        *args: P.args,
        **kwargs: P.kwargs
    ) -> Pending[R]:
        """Run a step inside a cache slot, catching (and catching) errors as observable outputs.

        Like `run`, this schedules and dispatches the given step to run in this cache slot's
        storage.

        Unlike `run`, if the inner step encounters an error, that error is _cached_ in the slot
        and treated as a valid outcome of the step. The raised exception can be caught and handled
        by the program as needed (and is _expected_ to be caught, since if the workflow is
        resumed the error will simply be re-raised from the cache). The raised exception will be
        an ordinary Exception, not just a BaseException.

        `run_fallible` should be used when executing code that may fail, and where that failure is
        expected as part of a correct workflow execution. For instance, when executing code written
        by an AI agent running inside a workflow, it would make sense to use `run_fallible` so that
        errors can be observed and fixed by the agent.

        As a safeguard against cache corruption issues, it is not allowed to pass cache slots as
        arguments to the step function in `run_fallible`, so that errors have a consistent
        caching behavior inside and outside this call.

        Note that a workflow abort may still surface as a BaseException and should not be caught
        to avoid deadlocking.
        """
        ...

    @abstractmethod
    def subslot(self, key: str | int) -> CacheSlot:
        """Index a subslot of this slot.

        Accessing a subslot reserves _this_ slot for the enclosing step if not already reserved.
        It is an error to call `subslot` on a slot that was reserved for some other step.
        It is allowed to call `subslot` multiple times with the same key, and this will return the
        same slot object.
        """
        ...

class StepHandle[**P: PendingOrConcreteTypes, R: ConcreteTypes](abc.ABC):
    # conceptually, but bounds on P here aren't actually allowed in python.
    # anyway step handles are not generally constructed by the running step but instead
    # are either synthesized at runtime by the entrypoint program or embedded into the
    # step statically, and each of these will have a more specific input and output type
    # annotation. note that step inputs can _always_ be pending at any level, because
    # pending values are resolved before dispatching the step.
    ...

type ConcreteTypes = (
    int | str | bool | bytes | None
    | CacheSlot
    | StepHandle
    | list[ConcreteTypes]
    | dict[str, ConcreteTypes]
)

class Pending[R: ConcreteTypes](abc.ABC):
    """A pending result of a step.

    Can be awaited to get the value. Can also be directly forwarded to other steps to build up
    a DAG of operations. In some cases we might want to disable awaiting the result but, to start,
    we will always allow these to be awaited inside any step.
    """"
    @abstractmethod
    def __await__(self) -> Generator[Any, Any, R]:
        ...

type PendingOrConcreteTypes = (
    int | str | bool | bytes | None
    | CacheSlot
    | StepHandle
    | list[PendingOrConcreteTypes]
    | dict[str, PendingOrConcreteTypes]
    | Pending[ConcreteTypes]
)

class StepImpl[**P: ConcreteTypes, R: ConcreteTypes](Protocol):
    async def __call__(
        self,
        step_ctx: StepContext,
        *args: P.args,
        **kwargs: P.kwargs
    ) -> R:
        ...

class TopLevelEntrypoint:

    async def run_uncached(
        self,
        step: StepHandle[P, R],
        *args: P.args,
        **kwargs: P.kwargs
    ) -> Pending[R]:
        ...

    async def open_cache_slot(
        self,
        cache_namespace: FxtrNamespace,
        cache_namespace_prefix: str,
        replay_reference: CacheSlot | None = None,
        replay_mode: Literal["strict", "best_effort", "migrate"] = "strict"
    ) -> CacheSlot:
        ...

    async def bind_external_step[**P, R](
        self,
        step_impl: StepImpl[P, R],
    ) -> StepHandle[P, R]:
        ...

    async def build_managed_step[**P, R](
        self,
        ... # todo: what do we need for this?
    ) -> StepHandle[P, R]:
        ...
```

Things to still figure out:
- How cache keys and cache data will actually be stored
- How managed step functions will work, concretely
- How managed step functions will be provided with "compiled-in" references to other managed step functions (thinking basically, they will be provided at runtime but need to be requested using a special API, not using the step fn args)
- How network references will work
- How migration and provenance tracking will work

Possible order of implementation:
0. Basic package setup
1. Core workflow system:
    - Core types
    - Cache slot without replay_reference support, just writing and resuming
    - Step context and top level entrypoint with external steps only
    Should be possible to do a very simple workflow on top of the system.
2. Extend core workflow system to support `run_fallible`, `save_checkpoint`, and `load_checkpoint`
3. Extend core workflow system to support strict and best-effort replay mode
4. Add network service handle system: some opaque type that can be unwrapped to get a `anyio.abc.ByteStreamConnectable` that can then be wrapped into a higher-level transport like a `httpx.AsyncBaseTransport`
    - Can construct this at the top level entrypoint by providing a "TransportRoute"-like object which determines how to route the connection, following pattern in extras/isolation_spike/isolation_spike/proxy.py (we can copy the relevant parts of this over), or by providing a bare anyio.abc.ByteStreamConnectable that the entrypoint should use.
    - Passed around as an opaque value with a FxtrID key for caching, similar to StepHandle
    - We can also provide a helper that turns the `anyio.abc.ByteStreamConnectable` into a `httpx.AsyncBaseTransport`, via
        - custom httpcore.AsyncNetworkStream class
        - wrapped in a custom httpcore.AsyncNetworkBackend
        - wrapped into a subclass of httpx.AsyncHTTPTransport which swaps in the network backend
    - This can be somewhat similar to extras/isolation_spike/fxtr_task_runner/fxtr_task_runner/transport.py or extras/isolation_spike/isolation_spike/_runner.py but designed to be robust / not reach into unsupported internals like _pool
---
5a. Add first-class fxtr support inside step fns
    - External step functions run in a FxtrScope whose parent is whatever was the active scope at the level of the top-level entrypoint.
    - Step functions can receive FxtrIDs in their args (which are added to the scope before the fn runs) and can return FxtrIDs in their result (which must be loadable in the scope at return time)
    - For scope isolation purposes, every step fn is invoked with a similar type of dispatch indirection as in the Fxtr RPC bridge system:
        - We create a new FxtrScope containing the call, with all of the args and things that we need lifted into it
        - The FxtrIDs needed by the step fn are then snapshotted into a separate arena, which is then mounted into the _inner_ temporary scope, so the step fn can retrieve those entities but isn't tracked as a normal child of the outer scope
        - On return, the entities are all saved into the namespace from this temporary scope, and it is then thrown away
        - This all happens inside the lifetime of the outer FxtrScope so this is safe; the indirection just to prevent the step fn from lifting other stuff it is not supposed to have access to
    - In `bind_external_step`, callers can attach a config entity to the step handle. This is not part of the identity of the step for caching, but the step fn can access this config entity via a method `get_step_config_id() -> FxtrID` on the step context and then load it
5b. Adapt checkpointing system to use first-class fxtr support
    - Checkpointing now becomes a pin/unpin/retrieve style API which routes to the checkpoint prefix, rather than a monolithic checkpoint blob. Additionally all of those pins that remain when the job finishes get folded into the immutable StepFinished result just like substeps, so that they can be referred to again later
    - By default pins from a previous run do _not_ show up when you re-execute in either same-slot or best-effort mode (in contrast to the cache slots which get walked automatically). However, there is a separate method `try_retrieve_from_previous_execution` which can be used to load values from previous attempts; if used, this also copies the relevant pin into the new run's namespace. One could then implement the old checkpoint API based on this by writing to some known "checkpoint" pin to save, and using `try_retrieve_from_previous_execution` on that to load.
    - (note that this might mean we need to have separate prefixes for each execution attempt when replaying a partial run, so that we don't step on it. or maybe we have exactly two: one for all previous runs, flattened together, and one for the new run. or, maybe simpler: maybe it is all in one namespace, but when the task pins something, we wrap it in an wrapper entity that assigns it an execution generation; then when you load, if its a previous generation, it is treated as not current; if you use try_retrieve_from_previous_execution it upgrades the generation)
6. Add fxtr RPC service handle system: some opaque type that can be passed to the step to get a FxtrRPCHandle
    - Constructed at the top level by binding a FxtrRPCHandle similar to how you bind an external step fn
    - Passed around as an opaque value with a FxtrID key for caching, similar to StepHandle
    - Can be unwrapped to get (a fresh clone of) the FxtrRPCHandle inside the step fn
7. Eryx-over-fxtr integration:
    - Define a type for eryx workflow steps
    - Set up the invocation logic for this to connect to eryx over fxtr RPC over the main sandbox callback channel; on the loader side we wrap and bind the fn as a fxtr RPC target.
    - All operations on the step context and on cache slots get turned into fxtr RPC calls that call back to capabilities originally provided to the sandbox through the channel; they then execute on the host in the same way that they would have executed if called by an external step fn already running on the host (e.g. they go through the full dispatch flow on the unwrapped objects)
    - Network service handles also proxy themselves as capabilities over the fxtr RPC channel, rather than having multiple callbacks (change from isolation spike)
    - Set it up so that the eryx config entity type can be directly used as a StepHandle and passed to `run` etc; when we dispatch to this it requires us to actually _spin up a sandbox_ via a sandbox factory. Can set up the sandbox factory so that it preloads the imports and gets cached in memory by the sandbox bundle's FxtrID
        - the raw sandbox config can be parameterized by an arbitrary fxtr config entity, which can then be loaded from the step context via the same method as the external steps have; this gets threaded into each execution as a cap (so we can re-use the same sandbox even for different fxtr config entities)
    - This should make it possible to build graphs where an Eryx task gets access to another Eryx task (with different deps) via its config object, and invokes it as a subtask in its own slot
8. Similar system for docker, but using a mounted Unix domain socket as the network communication mechanism (based on the spike, but with a single root domain socket that dynamically brokers the connection to the per-transport socket)
9. Basic debug web UI (derived from "streams spike" from way back)
10. Figure out provenance
11. Add migration mode
12. Fxtr ease of use features: type-driven entityref conversion on both sides of a step fn
13. maybe consider run_uncached (not that important to the rest of the system)

---

notes on cache slot replay check:

essentially what we want to ensure is that all of the values in the cache slot args have the same values as they did when we observed them during the previous execution, so that we can safely reuse the cache and be sure that the result would _not_ have been observably different. if re-executing the step would have seen a _different_ cached result internally, that means that the output of the step might have also been different, and the side effects might have been different. we need to avoid this!

to do this, when a step is executing, we need to track the set of all runs that were emitted inside that step and that either (a) were dispatched _to_ a cache slot that was derived from a cache slot argument to this step, or (b) that received as an _argument_ a cache slot that was derived from a cache slot argument to this step. in both cases, we need to track the _path_ by which all of the foreign cache slots it received were derived from this one's args + where they went, and we also need to link to the finished run result of that run (its `SlotFinished` entry). this tracing will probably require us to wrap/trace the cache slots that get fed in as args so that we can record operations that use them. all of this info needs to get stored into the SlotFinished.

then, to do the replay check, we need to essentially answer the question: if we assumed the step itself was deterministic, and assumed it had the _same_ sequence of runs that it emitted, would all of those runs see or use the same cached data? this means, for each such run, is the version of the run that is currently in the cache slot the same as the version we recorded in our `SlotFinished` entry, and furthermore, does that run also pass the cache-slot-arg replay check for itself, if we passed it the args that it would have received if we replayed this sequence of runs ourselves?

if we verify that this holds for _every_ run that was in any way derived from a cache-slot arg, that will recursively verify everything, and ensure that the run would be safe to replay. (but we don't have to verify runs that just used subslots of this cache slot, and didn't depend on our cache-slot args, because if they were purely derived from our own active cache slot then we know they are still safe because our own cache slot owns their state.)

we'll want various tests to test all the edge cases here, where passing in cache slots that are identical to the previous ones should hit the cache, but passing in cache slots that have different state should error out, because passing in a cache slot that has contents non-identical to what was observed in the previous execution is essentially a "cache-breaking" change and is considered to require a fresh execution that is consistent with those new cache slots.
