
Desiderata:
- Mostly declarative: the semantic meaning of the workflow should be stored as data and version controlled, rather than constructed imperatively
- Trace-based, no upfront declaration of a graph separate from its results
- Hierarchical + referentially transparent: possible to define tasks that run other tasks
- Caching as a first class citizen: you shouldn't have to reason implicitly about cache contents, different samples shouldn't silently duplicate themselves unless you specifically request this

Components of the design:
- A _task_ or _step_ is a possibly-nondeterministic, but otherwise observably _pure_, computation, bound to some arguments
- A _procedure_ or _task function_ is the logic that defines how to execute a task
- _Task storage_ is "the place that stores and/or holds fixed the result of a task"
    - One perspective: this is the location where a task's progress and final result is stored, enabling durable re-execution
    - Another perspective: this is a just-in-time resolved "seed" that lets us treat the result as deterministic
    - Another perspective: this is like an address in a probabilistic programming language, it records and allows replay of nondeterministic effects
- A _realization map_ is an append-only map from task slot to task result.
    - When executing a task, previously-executed (sub)tasks are looked up from this, and new results are written into it.
    - Failed executions are temporarily persisted into realization maps but are _not_ considered realizations. No successful realization / task execution can depend on or observe a failed task execution.
- A _service handle_ is some entity data object referring to a external service that can be statelessly reinitialized and used to perform operations.
    - These can either be _built_ to be stateless, or can be _promised_ to be stateless
    - Each has an _identity_ which determines how it is cached, and some mechanism for the system to spin it up when needed

**Note** Went back and forth on naming, sometimes referred to equivalent things as task/step/workflow. Currently calling them "tasks". Possible conceptual breakdown: a "procedure" is a self-contained stateless operation you can run with different arguments. A "task" is like a thunk, it binds together a procedure with its arguments, and can be cached into a cache slot. An "outcome"/"result" is what you get by executing a task. "Realization" is the act of getting an outcome from a task, by executing procedures and/or retrieving (sub)results from cache.

Sketch of how the design will work:
- step functions may take as input:
    - Fxtr entities
    - Regular data
    - Write-only task slots (so, they can run tasks into them but not read directly from them)
    - Stateless services
- Every task execution combines a step function, a task slot, and args
    - This then produces a cache key: formed from a hash of the entities, data, cache slot paths relative to the realization map, and service identities
    - On a mismatch: rejected
        - But, some ability to best-effort replay from one realization map into another
    - If empty: the new task executes and its data is saved into this slot
        - Running the task may load or save additional data to additional task slots
        - Any such writes observed during a task are collected for coherence checking
        - Final saved result combines (a) the result of the step, (b) any side-effect task slot writes that occured (or were observed from cache) during this slot's execution
- Realization maps have an implicit coherence invariant: if a run R into task slot A observed a cache write/retrieval of run R' to task slot B, then run R' must be in task slot B for as long as R is in task slot A
    - This makes sure that both slots see a consistent state of the world, in particular so that we are never in a state of the world where task slot B contains result X but task slot A is only valid under the assumption that task slot B contains Y and they are consequently in an invalid combination of states
- External services are bridged in and assigned an identity. These are implicitly trusted. Intent is to keep the number of these small
- Internal code logic is _built_ and _referenced_ via nix, which controls the runner.
    - rough sketch:
        - fully-external services have to be passed in externally to the top-level task fn
        - external step functions, if they get access to the trusted job manager / task slot manager, could spawn or get access to other external services or step functions
        - nix-defined _sandboxed_ step functions can be built with a set of named pointers to other sandboxed services or step functions. then when they launch they can request those task handles from the runtime.
    - some more details
        - when a service is configured in nix
            - defined as an attrset with
                - a type tag identifying it
                - a derivation pointing to a nix2container image manifest (readable via nix2container util but we don't trust the util here)
                - a config entity
                - an attrset of attached bindings, which are each attrsets of this shape
            - when launched via nix inside a sandbox
                - roughly: its connect method always gets passed (a) a config entity, (b) a map from binding name to service handle for the attached binding, (c) the spawner service itself that accepts a service handle and produces a running service
                - it then returns the service, configuring it however needed
                - built on top of existing fxtr_sandboxing
                - the binding logic essentially means that every stateless service attrset gives exactly enough information to instantiate a fresh impl of the service, and then the second-arg service it gets exposes the ability for it to request its own parents
        - bound in externally
            - for this, it can just be the stateless service itself
- a step function is just a type of stateless service, where the service exposes `run` with a context arg
```python
class TaskFn[**P, R](Protocol):
    async def run(self, step_ctx: TaskStepContext, *args: P.args, **kwargs: P.kwargs) -> R:
        ...

class TaskStorage(Protocol):
    def run(
        self,
        step_fn: ServiceHandle[TaskFn[P, R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        ...

    def subslot(self, key: str) -> TaskStorage:
        """Access a subslot of this slot."""
        ...

class TaskStepContext(Protocol):
    def subslot(self, key: str) -> TaskStorage:
        """Access a subslot of this step's own cache slot.

        Can be used if this step wants to launch additional work, but did not receive cache slots
        as arguments.
        """
        ...

    # eventually, maybe marked with a comment but not actually added in V1:
    # async def checkpoint_store(self, key: str, value: FxtrID | EntityRef[Any]): ...
    # async def checkpoint_retrieve(self, key: str, type: EntityTypeAnnotation) -> EntityRef[T]:

    # eventually, some mechanism for logging output, not needed in v1
```

How this will all wire up in v0:
- Conceptually we split into a "stateless service spawner" and a "workflow runner", each implemented in its own submodule of `fxtr_workflow`
- **Stateless service manager**:
    - Python object, manages async via anyio (but mostly doesn't own async lifespans itself and rather depends on workflow runner for this).
        - Maintains a map from service key entities to running services, and also holds additional state that allows it to manage the lifecycle of reproducibly-executed hermetic services.
        - Ideally implemented as actually "just" a fxtr2 service, so that it's possible to thread these through a workstation or similar. They can also test out the (new) impl mount lifespan system, so their own internal scope management and state logic can live inside their `_fxtr_mount_lifespan` handler (automatically used when they get mounted into a scope) rather than living in an external contextmanager that their caller has to handle.
    - External services: Holds these as ServiceRefs to a service that has a single method `connect()` (no args) which returns the actual service that the task should use. Can be attached to a running workflow manager and bound to a key (producing a ExternalServiceHandle[T] from a ServiceRef[T], where ExternalServiceHandle is an entity that wraps another arbitrary key entity)
        - Mechanism for this: similar to how the fxtr workstation backend bridges in services
        - If you want to provide a singleton service that is known stateless, you can wrap it in a connector that just returns that singleton instance when `connect` is called (and there can be some helper that does this)
    - Sandboxed services: Maintains a map from DRV hash to the git commit, flake attr, and flake path, as needed by the sandboxing spawner. Also has a cache of running sandboxed services that it has already used. DRV hash gets wrapped in a NixBuiltServiceHandle[T] which is what tasks use to interface with it
        - It creates its own sandboxing env at runtime
        - When a task needs to connect to a sandboxed service, it walks nix attrs itself to figure out what to pass to that sandbox service's entrypoint
            - and also at this point discovers that service's own bound sub-services, puts them into its own big cache by DRV hash, and passes in the appropriate NixBuiltServiceHandles into the entrypoint
        - To actually resolve this it would need to hold a nix backend and repo root similarly to how the sandboxing env does.
        - Note though that it should be possible to create a stateless service manager without any nix backend (in which case only external services are allowed)
    - Owns and spawns in services when requested:
        - For external services: we invoke the external service provider to get a concrete service
        - For sandboxed services: could be customized in principle, but for now, if we already have a spawned service we call its `connect` with the saved args for it to get a new service; if we don't have a spawned service sandbox we first go spawn it, do the walk of its sub-services to bind them in and prepare the args to its internal entrypoint, and cache them for later use
    - Has a method that can be used to release all of the cached sandboxed connect services that it has spun up, so that new requests actually spin up a new sandbox guest
    - Also has a configurable max_guests_per_sandbox param, and after it has connected that many guests it starts a new sandbox for the next guest
        - future deferred refinement (for now track with a TODO): track how many guests per sandbox are currently active (by watching the tokens of each `connect` result and waiting for them to expire) and have this be max_concurrent_guests_per_sandbox, so we can re-use a sandbox unless it is getting heavy concurrent load; we could also potentially hold alive the old sandbox connecter services in a pool so that we can dynamically fill them back up / spin them down. we can use the simpler thing for v0
    - TODO: we will want to change the provenance system
- **Workflow runner (slot manager)**:
    - Main job of a workflow manager is to manage a durable workflow across a number of named cache slots
    - Maintains an anyio taskgroup of jobs, where each job is bound to a cache slot, and is responsible for claiming the cache slot, writing its progress into it, and writing the final result before exiting
    - Ideally also implemented as actually "just" a fxtr2 service.
        - The general API contract for how people interact with it is given by the `TaskStorage` / `TaskStepContext` / `TaskFn` protocols
    - Each cache slot corresponds to / is delegated to a single async task
        - This task owns the slot. When this task exits for any exceptional reason that corresponds to a failure of the execution, and anybody waiting on the slot receives it
        - Generally task fns that invoke other things into cache slots end up sending and awaiting a request to the workflow runner, which then deduplicates by cache key and makes sure there is only one job running in a given slot
            - Jobs that cache-hit can share each other's result and all get notified
            - Jobs that cache-miss error out immediately
    - Slot shapes
        - A task storage object refers to a particular pin inside a fxtr2 storage namespace
            - Creating a subslot is equivalent to appending "/{subslot}" to the current storage key (this is documented as the contract for `subslot` but TaskStorage API does not let you walk _up_ the tree)
            - Usually, the storage namespace will be a fxtr2 "git store", which syncs its contents into tsv files in a git repo. This then acts as a sort of "lockfile"
                - Not intended to modify this manually, since a lot of the content pins other content, but you _are_ expected to version control it like you would a lockfile
                - (sidenote, not important for this change: eventually BH would detect both remote entities declared in nix flakes and the tsv-pinned entities here and ensure they travel alongside the repo)
        - This pin gets updated to refer to the current state of the task. There are a few typed cases:
            - (empty: implicit if there is just no pin)
            - `WorkflowTaskClaimed(cache_key=..., owner:str=(some string describing this runner), timestamp=...)`: Some task is actively running into this slot.
            - `WorkflowTaskComplete(cache_key=..., result: Entity, timestamp=..., depends_on: dict[str, EntityRef[WorkflowTaskComplete]]: ...)`: This task completed in this slot with a result. `depends_on` is a map of _other pin names / slot storage names_ whose results this task observed / wrote into, pinning the specific observed result.
                - KEY INVARIANT: as long as this entry is in the task storage, those _other_ entries must also be in task storage and must match at their given keys. (We don't check this on normal operation but we do use this to prevent discarding of old results, and can optionally do a validity check on request)
            - `WorkflowTaskIncomplete(cache_key=..., kind:str=... reason: str=..., timestamp=..., depends_on: tuple[str, ...]: ...)`: This task ran into an issue while executing. depends_on is informational: a list of other task keys that this task requested.
                - kind can range from "failed", "child_incomplete", "child_conflict", "interrupted"
            - _EDIT:_ Possibly we should not save claimed/incomplete into the persistent storage at all. They could instead be saved in memory / persisted in an event log.
    - Failure semantics
        - Failures that bubble out of a _step function_ get special handling, due to the interaction with the cache/realization map.
            - A task function that throws an error gets marked as "failed" in the realization map. Similarly if we try to run an incompatible task into a realization map cache slot, this is marked as a "conflict" (treated like a failure)
            - Any task function that was waiting on the result of this task function becomes "poisoned": regardless of whether it succeeds or fails, the result will be marked as "incomplete" (also treated like a failure). This is to ensure we don't cache failures that aren't coherent as task results; a finished cache slot cannot point to failed/partial cache slots
            - Related to this ^: every task function that is invoked inside another task fn must complete before that task fn can exit.
                - Even if one child task fails, the manager keeps alive all of its child tasks that were already launched, and waits for them so it can save their results coherently into the store
        - (note: in earlier stages of implementation we could start by not allowing cache slots as task args at all, which then avoids the need for poisoning)
- organization:
    - fxtr2.workflows: contains
        - a module with an empty abc.ABC for a service handle (a subtype of Entity) and a typed service protocol for stateless service managers, focusing on the ability to spawn a service keyed by a key. service protocol API intentionally does not expose/require/pin down the contract of how to register external services or hermetic nix ones; it is purely expressed in terms of the API surface of some object that can be _used_ to spawn new tasks
            - (where the point of the abc.ABC is basically to give you a way to declare "I am a type of service handle for T" for a concrete subclass, and hvae type inference route around it)
        - a module with the typed service protocols for TaskStorage, TaskStepContext, and TaskFn
        - a module with the implementation of a workflow runner
            - I think actually the shape of this can be that the workflow runner is a python object that exposes ~all of its functionality through a root `TaskStorage`
            - to construct it you would enter some `WorkflowRunner` object as a context manager, and pass it (a) a stateless service spawner, (b) a index-supporting fxtr namespace that it can own
            - it would then give you a root `TaskStorage` where:
                - all attempts to run something through this workflow slot resolve their TaskFn through the stateless service spawner, and save their results into the namespace
            - as an extension, the workflow runner would also let you clear cache slots by name or glob pattern. it would maintain an index over its cache slots so it can query for relationships between them, and use this to ensure that clearing a cache slot requires also clearing any cache slots that depend on / used their results. (for v0 it can just error if it finds any and suggest cleaning those up first).
        - a basic implementation of the stateless service manager that _only_ supports external services with no caching or special logic, basically the minimum thing to both (a) implement the service protocol needed by the workflow system and (b) attach external services via external service handles
            - this should actually be structured such that the stateless service spawner protocol (which connects to handles) is a separate sub-service produced by the root object, for capability-passing reasons: if you own the whole service manager and are registering services, you can derive a capability to retrieve and spawn those services, but if you just have the spawner capability you can't upgrade it and register your own stuff there.
    - the existing `fxtr_sandboxing` package sandbox env gains a stateless service manager impl, which is like the basic implementation in fxtr2.workflows but _also_ adds the ability to transform a nix git flake reference (to a whole derivation-plus-config-plus-bindings object) into a step handle entity that this service manager can recognize and instantiate
        - Specifically, we convert the attrset to a typed fxtr entity NixStatelessServiceHandle where:
            - the derivation is represented as its _derivation hash_
            - the config entity is just inserted as an EntityRef[UnknownEntity] / just a FxtrID (both equivalent)
            - the bindings become a dict[str, EntityRef[NixStatelessServiceHandle]]
        - We then bind a provenance entity to each of these which identifies its git hash + flake path + flake attribute (with attribute recursing into the binding path as needed)
        - For cache-key purposes, this provenance data is not consulted; it is only there to help debugging and tracking state while executing.
            - Possible extension: possibly the stateless service manager could use this provenance as a _check_ that the binding was actually authorized

(note: there was an older project also called "fxtr_workflow" which had some of these ideas but not others. I think it is cleaner to start fresh with a new design and not consult this older project at all, so don't worry about older references to this.)

possible rough implementation order:
- all of the type-only modules in fxtr2.workflows
- basic stateless service manager in fxtr2.workflows with some basic tests for it
- workflow manager implementation, with a full implementation of workflow slots and step contexts, but _without_ the ability to pass `TaskStorage` as an arg (args must be regular serializable fxtr data; task storage detected => raise NotImplementedError) or receive a subslot from the step context (which should exist so the API is stable but just raise NotImplementedError) and also no way to clear task storage. tested against basic service manager
- add ability to pass a workflow slot into a task fn as an arg, which requires handling extra things:
    - tasks waiting on each other
    - parent tasks recording all of the child tasks that executed
    - handling cache keying in a sensible way
- add ability for task storage to be deleted. this would involve also checking and maintaining the invariants that are set up during task writes about recording dependencies
- build out the _sandboxed_ / hermetic implementation of the stateless service manager
    - should basically work the same way as far as the workflow system is concerned, but now it would be possible to just spawn a bunch of tasks out from a git-pinned entrypoint instead of creating and bridging in extra stuff.
    - this piece might end up being somewhat complex on its own
- [should be usable at this point]
- follow up / extension 1: streaming output as part of task context, surfaced by root workflow manager (depends on separate output channel system)
- follow up / extension 2: add ability for checkpointing partial progress of tasks

desired layout:
- overview.md - overview of the change and of the files here / their states
- design/stateless_service_manager.md - more complete design doc for stateless service manager
- design/workflow_runner.md - more complete design doc for workflow manager
- part1_types_and_basic_manager.md - plan for impl these
- part2_workflow_manager_core.md
- part3_storage_args.md
- part4_storage_deletion.md
- part5_sandboxed_service_manager.md
- followups.md - tracked extensions + other things to follow up on
