Some of these proposed changes are already incorporated into "Daniel note" markers, some are new.

Changes to details of the existing design:
- No new wrappers for task storage and task slots. Instead these should just be normal ServiceRefs. Async penalty for subslots is OK for now.
- Don't persist incomplete or claimed markers into the storage.
- Change of plans about storage type: we will generally store into a normal sqlite namespace rather than a TSV namespace in v0. TSVs can eventually be used as an export format.

Larger changes to the whole structure:
- Lets maintain two storage namespaces:
    - The realization map only stores successful task results, and is conceptually mutable, and can be written and updated pointwise.
    - The op log tracks operations on the realization map, and is conceptually an append-only log, which stores a full history of all operations performed on the realization map
        - Launching jobs
        - Recording results (success OR failure)
        - (eventually) Deleting results
    - Op log is for now just an event log. Eventually we could consider an event-sourcing redesign where the realization map is derived from the op log.
    - For now op log entries can just be pinned by timestamp keys
- The top level workflow runner should not directly expose a task storage key. Instead it should expose a protocol that lets you start a "run group" by passing a name and description. The run group would be a service protocol that then has a `get_storage` method that returns the TaskStorage. When passing storage as an arg into a run at the top level, all of them must internally have a matching run group. The run group gets a unique entity identifying it, formed by its name, desc, and timestamp of creation. This is then saved into the op log alongside each of the events for jobs launched into that run group. The purpose of the run group is to group sets of runs that were all launched together when viewing the op log. The run group does not affect the realization map in any way.

