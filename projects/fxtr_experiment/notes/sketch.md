
## concept:

- global namespace representing configuration + data of an experiment
    - where names map to semantically meaningful parts of an experiment
- entries of the namespace are _grids_ (essentially tables)
    - every grid has a set of coordinates: named columns that index their contents, each with some index set (generally strings or ints)
    - the semantics is that a grid is a _partial function_ from coordinate to value, interpreted as an open map: entries that do not yet have values are simply invalid and cannot be depended on
        - there is a unique way to get a non-open map: the grid with _no_ coordinates contains a scalar and can be "filled" by a single item
        - note that grids do not have to be "complete" and do not even have to have coherent bounds. it is possible to fill grids irregularly, and for some entries to just be "invalid", e.g. if there are 4 samples in category "foo" and 7 in category "bar" we might have a coordinate space (category: str, ix: int) where the only keys that have been populated/requested are just ("foo", 0), ..., ("foo", 3), ("bar", 0), ..., ("bar", 6), and where
    - every grid has a _cell type_:
        - handle: each cell is a service handle, it is a pointer to a running service, parameterized by some per-cell
            - external handles: these are basically all just named slots that will refer to code that has to be bound in at runtime. whatever runtime python manager actually does the task running would be responsible for binding things into this
                - maybe external handles can be parameterized by an entity that they use to configure the external service at connect time?
            - sandbox handles: these point to code that we would instantiate in a sandbox
            - in both cases, the API that the tasks would get would be essentially: when running a task step/activity, the runtime would provide some capability that can be used to connect to the service. this connect call would then proxy to a fxtr2 mounted service that provides the handle, and would return a new fxtr2 serviceref that the task can resolve and call methods on.
                - this would work similarly to the service handle and basic stateless service provider from spikes/fxtr_workflow_2/plans/part1_types_and_basic_provider.md and and spikes/fxtr_workflow_2/fxtr_workflow_2/stateless_service_provider/ , although it would be factored differently than in fxtr_workflow_2 due to it being a cell in a grid rather than a separate service manager object
        - data: each cell is an _entity_ directly saved there. possibly editable
            - for some of these, the value here is just set at the top level
            - data can also be written here by tasks
            - every data item is stored as a FxtrID that then points to a fxtr2 content-addressed entity in an entity store
        - checkpoint: each cell is a checkpoint of the result of executing some task
            - only read or written by a specific task
            - the checkpoint grid is _bound_ to the task grid that owns it
            - generally you only encounter these inside a task grid's subtree
            - checkpoint data can be whatever serializable data a task wants (although we would probably wrap it in an entity for storage in the backing store)
        - subtree: each cell is a "place" where new cells can be stored
            - cell values are arbitrary maps from string names to new grids
            - new grids can only be of either data, task, checkpoint, or subtree type
            - regularity: for now they all have to be regular in shape, meaning that across all coordinates of the subtree, every cell with a given key has the same type, configuration, and column set, so that conceptually we can "prepend" the subtree's own coordinate columns to the subgrid columns to get the actual underlying table view of the data
            - note that subtrees also inherit the partial-function interpretation of grids: you can't list a subtree to see what is there, you can only open grids by name, and if it doesn't match something that is already there you get an error
        - task: each cell is the result of executing a _task_
            - a task grid is _not_ user editable but instead is defined by a _rule_: some DSL expression which maps any given set of coordinates to
                - (a) a specific handle from a handle grid, indicating its _task_ function
                - (b) a set of named slices for a set of parameter grids, which it gets access to (these grids are then considered "input grids")
                    - these slices will often refer to the same variables of the input coordinates, vaguely einsum-like, which then sort of "unify" with the input cell grid. you can also keep an entire axis and slice it in the code, if you prefer
                    - note that dependencies on these slices essentially become some sort of path expression, where we trace subtree key paths + coordinate slices. the thing that it resolves to depends on whatever is currently at the location described by this
            - a task grid implicitly also has its own subtree, where this subtree can only be accessed / written to by its own task
                - tasks can use this to essentially implement durable workflows, where they persist internal information into their internal task or checkpoint grids
            - every task produces a single entity
    - grids can also be associated with _indexers_ or _checkers_
        - the logic for these are themselves represented as handles that are stored in other grid cells
        - each indexer/checker describes a named collection of table column schemas (empty if not needed) where the columns of each table can be either basic types or entities
        - it then maps every grid cell to
            - A status: {OK, SKIP, WARNING(str), ERROR(str)}
            - (if OK or WARNING) A set of row tuples corresponding to this entry, matching the table column schema(s)
        - this then essentially produces an expanded table, whose columns are first the grid coordinates and then the output table columns
        - any cell whose output is ERROR for any indexer is considered to be missing for the purpose of the partial-function reasoning; nobody can observe or depend on these, and downstream analyses are blocked until it is rerun
        - any cell whose output is WARNING is surfaced but not blocked

- general rule for handling unexpected error cases:
    - transient errors that need to be retried due to environment issues should just raise out of the task
    - errors that are actually evidence and should not necessarily be retried (e.g. a model refusal or maybe a parsing issue) should be returned and then caught by a checker as either a validation ERROR (if strict) or a WARNING (if not strict)

- accessing grids
    - grids can be accessed either at the top level or inside a task
    - reading a handle
        - if you have a handle grid + a specific coordinate, you can connect to it
        - this creates an observation link: consumer depends on the handle value
    - manipulating a data grid
        - if you have a data grid + a specific coordinate, you can do multiple things:
            - you can read from it
                - this gives you the entity value currently stored there
                - if this coordinate is _missing_ that is a _fault_ and the task fails to execute
                - if this coordinate is present this creates an observation link: consumer depends on the coordinate value
            - you can alternatively _write_ to it
                - this writes some entity there
                - if this coordinate is _already populated_ with a _different entity_ this is a _conflict_ and the task fails to execute
                - if the write succeeds this creates a causal link: written value was caused by this task
                - you cannot both read and write from the same grid
        - if you have a data grid slice / a set of coordinates, and the grid is indexed, you can run sql over it
            - if _any_ coordinate in your slice is missing, this is a fault
            - otherwise the sql executes and you get whatever you got
    - manipulating a checkpoint grid
        - inside specifically the task that is bound to it, you can _checkpoint_ to it
        - this is essentially a durable workflows thing
        - if the value at that coordinate is already _bound_ to this executing task, the result is reused / replayed
        - if there is no value at that coordinate, some logic step runs, and the result is both written into the output and _bound_ to this executing task
        - if there is any other value at the coordinate this is an error and should not happen. this is because whenever you remove the bound task execution from its own task grid, you must remove all corresponding entries in a linked checkpoint grid
    - manipulating a task grid
        - basically, what the grid does is fixed at the moment where the grid is created (see grid creation below)
        - this means that really the only thing you need to do with a task grid is _access_ it, which forces execution of it and gives you the result
            - if missing or errored, this is a fault, and the downstream task cannot observe it
        - if you have a task grid slice / a set of coordinates, and the grid is indexed, you can run sql over it
            - if _any_ coordinate in your slice is missing or errored, this is a fault
            - otherwise the sql executes and you get whatever you got
    - manipulating a subtree grid
        - the only thing you can do in a subtree grid is to _open a subgrid_
        - this essentially creates a grid with some desired configuration
        - if an existing grid exists there but does not have this configuration already, that is an error
            - also applies across subtree parents
        - if an existing grid exists there with the same configuration, that is fine, and its contents can be used
        - when creating a task subgrid, you have to specify its rule and link it to whatever input grids it has
        - when creating a checkpoint subgrid, it is immediately linked to the creating task. if there is any grid here already which is not already linked to the creating task, that is an error

- how tasks work
    - each task is a fxtr2 service that runs against a task capability
    - essentially two levels
        - outer (deterministic/idempotent) level:
            - can perform general operations on grids
            - can checkpoint operations into checkpoint grids
                - this would look like how a durable workflow works, essentially, where you'd grab a checkpoint grid cell and pass the step logic you want to run (as a serviceref) + its args, and then that would be an async call that would run the logic if not computed, or give you the cached result if it is
            - basically, can do anything that is idempotent, deterministic, and trackable by the system
            - as a safety check (in strict mode) the outer level always runs a second time and ensures that every operation that the outer level tries to perform is already done / cached
        - inner (step) level:
            - always runs into a checkpoint grid, so it will reload from checkpoint on a re-run
            - _cannot_ schedule steps or tasks; it is essentially cut off from all outer-level operations (todo: how? maybe the simplest way is to not prevent them directly in particular but then detect them on the second-time safety rerun and raise an informative error there. but we'd have to track this across all reruns, even across regular crashes, where the final safety-check run of a task must replay _every_ observed action that has been observed on _any_ previous run of that task)
            - _can_ connect to service handles obtained from handle grids and use them (likely because the step fn would receive a resolver capability object that it can use to instantiate the service handle, which would then only live as long as the step fn)

- Linkage relationships:
    - Observation relationships: if task grid cell A reads another grid slice B (directly or via indexer) and receives a value from it, OR writes to or creates another subgrid or grid slice B, then task cell A observed slice B
        - This means that task A becomes stale if B changes; A observes B, B causally precedes A
    - Responsibility relationships: if task grid cell A writes a grid slice B into a data grid, or creates a grid B in a subtree grid, then task A is responsible for B
        - This means that that task B is part of the logical closure of A, and we can trace back from B to A to figure out why it is here
    - Binding relationships: checkpoint grids are bound to the task that owns them, because only that task can ensure that writes to them are idempotent
    - Note: all relationships are tracked by name / subtree path / cell coordinates, not by any other sort of internal ID

- Task states
    - Staleness: Whether the current result of a given task grid cell task should not be viewed as a sample from the distribution it is "for"
        - A task is _directly stale_ if any of the slices of input grids it observed has a different value now than it did when originally observed
            - This includes any direct-input _or_ subtree grids that it accessed and observed while running
            - Note that if A writes to B but then B is removed, A becomes stale (it asserted the presence of B which counts as observation)
        - A task is _stale_ if either
            - it is directly stale
            - it observed some other task (from a task grid) and that task is stale
        - A task is _up to date_ if it is not stale
    - Sourcedness and requestedness
        - Sourcedness: Why is this task grid here? Requestedness: Who depends on which slices of this task grid?
        - Task grid slices can be _user-requested_ if they are marked as such. Tasks added by the user generally start this way. Any grid with a user-requested slice becomes _user-sourced_.
        - A task grid slice is then:
            - _task-requested_ if some other task cell uses their outputs
            - _active_ if there is a chain of task-requestedness up to a user-requested slice
            - _unused_ if not task-requested or user-requested
            - note: unused cells can be pruned without affecting any user-requested result
        - Task grids are then
            - _task-sourced_ if some other task created them and wrote them into a subtree
            - _rooted_ if there is a chain of task-sourcedness that ends at a user-sourced grid
            - _orphaned_ if not task-sourced or user-sourced
            - (orphaned grids indicate places where we are not tracking how this computation came to be. if an orphaned grid is also fully unused, it can be pruned entirely)
    - These relationships are tracked in a database and updated when things change

- Handle migrations
    - When you change the identity of a service handle, this would by default mark stale any task that used that handle
    - However, in some cases you might want to upgrade code and promise that the new version does the same thing as the old
    - In this case you could swap out the handle and assert to the system that you have not changed the behavior of any successful task completion
        - (so, expanding the set of inputs is allowed, and turning errors into fallbacks is allowed, but changing the interpretation of existing arguments in a way that would have affected any results of any existing tasks is "against the rules" and can corrupt the cache; this is the user's responsibility)
    - This would go to all of the cells that refer to the old handle identity, and instead _evolve_ them so that they now report themselves as up to date against the new handle identity
    - However, these cells then get marked as such, or at least there is the right kind of storage so that at a given cell we track all of:
        - the current fingerprint identity of the task cell, indicating what it currently tracks as being "current"
        - the actual record of when this task (or checkpoint) cell was executed, and what version of the code was running at the time
    - This extra metadata lets us debug issues if we run into them

- Version log
    - At any point where no tasks are running, you can create a "commit" that saves the current state
        - Everything mutable and not deterministic-to-rederive-automatically gets checkpointed into this commit
            - so, content + requestedness go here, but staleness / activeness are derived
        - Commits are vaguely git-like and include an entity that describes current state + a pointer to previous commit
        - You could also extract the state hash and refer to that entity as a stable pointer to the state of your experiments
    - Initially this just allows you to rewind and/or fork. Eventually we could support e.g. merging

- Event log
    - Some sort of log of events that happen
    - in v0 probably should not be load bearing
    - eventually we could consider event-sourcing the whole thing and making the version log a derived view of the event/operation log

- Durability
    - Essentially treat this as a durable workflow system
    - Each task cell updates as essentially a state machine as it runs, from pending (empty) -> running -> complete/errored
    - Re-running after an error preserves task subtree state and checkpoints (replay)
    - In v0, probably just a single runner process at a time; if we see a "running" task on startup, it must have crashed and we treat it as such

- Storage
    - in v0, probably we combine two things:
        - a sqlite database tracking the workflow state and holding entity IDs
        - then separately, a repo store object that maps entity IDs to their data
            - this one would just use fxtr's existing entity storage system

- Visualization
    - maybe every grid can have an optional "tag" entity, which records what the grid "means". this would be a place where you could write documentation for it, and could also configure renderer options for it. this would be especially useful for data grids used as input, where the tag entity could be used to browse and maybe even edit the input entities that then flow downward through the rest of the graph

## things to flesh out

the specific areas I want to explore, roughly in the order I can imagine implementing them

- the basic interfaces of the system
    - how do we represent the overall data model and API in a fxtr2-compatible way?
    - specifically, task functions need to be fxtr services, and their steps probably should also be fxtr services
    - conversely, there needs to be some fxtr service representation of any capabilities we give the tasks
    - and then probably it's useful to have a top-level python API one would use to access the system, which maybe would also use or involve fxtr services

- how we might actually implement the backend
    - lets assume that we have a python process running everything, using anyio for concurrency, and managing fxtr state. how would it actually persist the results and query them?
    - currently thinking we'd use a sqlite database to store all the state

- getting to a basic prototype, with some super basic state visualizations powered by the fxtr workstation system.
    - probably should include at least data, tasks, checkpoints, handles
    - could probably skip indexers for now
    - could probably skip any form of sandboxing and just assume we bridge in some services

- how indexing and checkers actually fit in
    - API side:
        - how do you query them? presumably you get a handle you can run sql against. but it would have to be restricted in visibility so that you can only see valid slices, and could reject trying to query
        - how do you attach them? presumably this is part of the subtree API so that tasks can also attach them for querying
    - implementation side:
        - how do these indexer tables get saved? do each of them map to its own table?
        - how do we actually execute a query against it in a safe way?

- eventually
    - plumbing in sandboxed code
    - configuring everything so that
    - mutating the store by like requesting/unrequesting things, clearing state at cells/slices, deleting cells, etc
    - provenance tracking
    - migrations
