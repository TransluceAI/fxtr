> Questions about the Fxtr2 Data Layer

> Core Concepts

> 1. What's the relationship between "ground" and "data" nodes? The docstring mentions "ground nodes" as opposed to "provenance nodes", but then "d" is called "data" nodes. Are "d" nodes the canonical example of "ground" nodes, with "." and "!" being special ground nodes?

yeah that's basically right. a ground node is a node that doesn't provenance-unwrap to anything / can't be provenance-unwrapped, whereas a provenance node is anything that can. so d, ., and ! are all ground nodes.

> 2. What does "unwrap" mean conceptually? I understand the mechanics (provenance node → underlying data node), but what's the mental model? Is unwrapping like "stripping metadata to get the pure data"?

yes exactly. a provenance node is essentially data with provenance attached, so that you get a different ID even if the data is equal "at the application level", if the nodes came from different places. unwrapping a provenance node strips away the provenance information, and gives you an ID (or the underlying data) that exists at the application level without the provenance attached.

then there's a tricky thing that, in some cases, we will want to refer to provenance information as data at the (meta)application level. in this case, we would have another layer of provenance on top, tracking the source of the original provenance information. unwrapping that gives us the "data at the (meta)application level" which is in this case provenance data. we could if we wanted unwrap it _again_ to see what data it has.

a mental model would be like, suppose we want to trace the execution of a program that produces some data. then the result we would get would probably be data-with-attached-metadata, where the metadata is about the program we executed, and the data is what that program actually produced. then unwrapping throws away the info about tracing the program. but we might have a separate program which lets you explore the history of execution, and that separate program might want to refer to the provenance information directly, so it would not need to unwrap, it would directly use that data. and, if we were tracing _that_ program, we would end up nesting provenance levels. lmk if this makes sense.

> 3. What's the purpose of strong vs weak links (tags 33002 vs 33001)? The code tracks them differently but I don't see documentation on when you'd use strong vs weak. Is "strong" for ownership/containment and "weak" for references?

yes exactly. it is mostly for like frontend purposes, where a transcript might "own" the individual conversation turn data that it contains, but it might only "reference" the model and user that were involved in the transcript. usually a strong link implies a uniqueness relationship: in a given program, each value usually has a single strong link to itself. if there are two strong links to the same object, that is like having two actual copies in memory (like two independently-constructed dataclasses that happen to have the same fields, which you should probably think of as two objects even though they are identical), but you might have arbitrarily many _references_ to that object that aren't considered "where it is stored". that said, for tracking purposes we basically keep all of them to ensure references don't break, but the mental model is that an object with no strong links to it is "orphaned" and we don't necessarily know what it was supposed to be part of.


> Node Types

> 4. "c" (context) nodes - The docs say they're contentless, but they point to a d-node containing a "value" field. What's stored in the context data besides "value"? The ContextEntity class has ctx_key and metadata - what are those for?

generally it will track the execution context in which this entity was first instantiated. usually pulled from some global state maintained by the runner. still somewhat of an open design question, since we haven't implemented the runner context yet.

> 5. "p" (path) nodes - These seem complex. The path is a list of indices into canonical CBOR serialization order. What's the use case for p-nodes? When would you want to "splice a subgraph into a new context"?

good question. the main idea is something like, suppose you want to pass some data into a deterministic function and cache the result. you might not want the provenance of that result to depend on the provenance of the input, because if you ever pass the same input data with a different provenance, you want to be able to use the cache still. and also it's weird to save some provenance that is not used at all by the program if you are just caching. but, you might still want to track provenance internally about the function execution. so it would be nice if you could reassociate a whole data object with a provenance that just says "this is input K to the function Foo, running hermetically". but we don't want that to require deeply traversing the object. but we still want it to roundtrip, so we need some way to produce a canonical way to decode such a provenance node so that we can re-encode it. this is why we enforce index paths, so that we know exactly which part of the data it was relative to the e.g. function argument itself, and we can zip them all back together correctly if we try to save these provenance nodes into data.

> 6. "l" (lift) nodes - I see this mentioned in entity_core.py with "NOT IMPLEMENTED YET". What was the intended purpose? Should I document it as "planned but not implemented"?

lift nodes were an idea for simplifying p nodes.

> 7. "b" nodes - I see case "b": in the ID string parsing but it's not documented in the node type table in entity_core.py. What is a "b" node?

oh hm. I think a b node was similar to a "u" node but slightly different for some reason, but I don't actually remember why. lets actually remove this. I don't think any of the other code uses it.


> Arenas and Storage

> 8. What's the difference between a Store and an Arena? MemoryStore seems to be the backing storage while MemoryArena is a view with a tracked liveset. Is this like a database vs a transaction/connection?

good question. that's a fairly good analogy. basically an "Arena" is the main concept that gets passed around and has to conform to a common interface, and it governs the type of operations that you are allowed to do. if you have an Arena, that essentially means that you have a _lease_ on the set of nodes in the arena; somebody has promised that if you try to retrieve values that are in that arena, they will be able to give you the data, and also they will be able to give you a new lease on the new data that it points to. but the MemoryStore itself is the actual thing responsible for fulfilling those promises, since it is the thing that actually holds the data. and it can efficiently give multiple leases out to the same in-memory data, and all of those different processes don't really have to care whether it came from the same source or not.

for a while arena and store were the same concept but I realized that you often want a lot of arenas that can very cheaply be copied and forked, while using a single manager object that actually handles the low-level data ops. the sqlite backend and sqlite arenas are another example of this pattern, although there the lease they give is somewhat weaker

> 9. Why does store() need to be synchronous but load() can be async? The docstring mentions this explicitly.

this is an api design thing.

load _must_ be async, because it is possible that you have been given an ID to some very large object that isn't yet in memory. so if we don't have it, we have to pause to get it, and this might be expensive, and we don't want to block the thread.

store _could_ be async, but we can always choose to delay the write, and that doesn't matter for correctness. so it is more convenient for the user if delaying the write is the default behavior, and so store is _sync_. that way the user can always store big objects without worrying about waiting for them to commit, and just trust that by the end all the data that they
saved will be wherever they want it to be.

in fact for memory stores, we immediately "store" it in our in-memory cache, which is sync because it doesn't do any async comms. so there isn't even any writeback delay in this case. but if we "load", we might have to go fetch from an attached external arena, so we can't implement "load" as sync.

actually the sqlite store implements the restricted CopyTargetArena interface. this interface does not have a sync `store` method at all, it just has an async `copy_from_arena`. this has to be async because it has to async load from the source arena. but, we can fold the writeback into this async method for "free" from the usability perspective, because we've already entered an async context.

so in practice when a user interacts with the system they might
- create a sqlite backend that attaches to some persisted data
- attach it into a memory arena that they will use for caching
- load and store to that memory arena, with all stores being fast serialize-and-cache operations on the memory arena
- finally, do a slow async writeback of the root object they built, once, via `copy_from_arena` out of the memory arena, which will commit all writes back to sqlite

> 10. What are "reservations" in the SQLite schema? I see entity_reservations_table - is this for concurrent access protection?

its actually for concurrent deletion prevention. concurrent access is fine because entities are immutable. but, we want to maintain an invariant that, if any entity data has no references to it, then it can be deleted with no consequence.

usually, entity data is pointed to by the namespace pins or by an arena, which keeps the data live. but, while we are copying data into an arena, there is a brief period where we have copied the data into the database but we do not yet have it in an arena. reservations make sure we still have some reason to keep it around in this period.

it's possibly unnecessarily conservative currently because the period of being reserved is very brief, and we might be able to reorganize the code to do it in one transaction instead. but it is a useful concept I think, and it might allow more complex operations in the future, so I lean toward keeping it.

> Serialization

> 11. What is mashumaro? The entity_serdes.py uses it heavily. Should I document it as "a serialization library" or is there something specific about why it was chosen?

it is a serialization library that is very flexible with serialization. something I wrote about it a while back: "seems to combine a number of cool features that I like about regular dataclasses (simple python data model), msgspec (no validation except at serialization boundaries, lightweight, supports tagged unions without manually specifying Discriminator at the use sites, can generate json schema), and pydantic (customizable serialization hooks, classes know how to serialize themselves, can also generate json schema). it also is very extensible and powerful. planning to use this as the batteries-included user level representation of entities in refactored fxtr, but then allow you to hook the serialization process (via mashumaro) to do mostly whatever you want"

the system I have in entity_serdes would be hard to implement in a different serialization library I think, because it requires discriminator-based decoding + custom type decoding based on type annotations + discriminator is a dynamic object.

> 12. The _fxtr_cached_ref_weakref pattern - DataclassEntity stores a weak reference to its EntityRef. I see the comments but want to confirm: this is purely an optimization to avoid re-serializing identical immutable entities?

yes exactly. removing it should produce the same observable behavior except that we'd do a bunch of unnecessary work.

> Provenance Tracking

> 13. What's a practical example of provenance tracking? The docstrings are very theoretical. Can you give me a concrete example like "if you load dataset X and transform it with function F, the result has provenance that links back to X and F"?

imagining something like this:
- at the top level you create an entity describing an input argument for a job. this doesn't have any provenance.
- at the top level you also create an entity declaratively specifying the environment where that job should run, what python environment it should have, and what the python entry point. this also has no provenance
- you ask a (yet to be implemented) task runner to run the task. the task runner starts up the environment.
    - it then captures a lockfile based on the environment and saves it as a context entity. then it attaches it into a "c" ID, and creates a "." ID whose _provenance_parent is this "c" ID.
    - it also creates a "p" node that wraps the input argument entity, and attaches metadata saying "this was the input argument to the job with this environment", linking to the environment node also. this becomes the _provenance_parent of the input argument entity
- the runner runs the task. internally, the application logic does stuff with the input argument, and constructs some new entities.
    - these new entities inherit the ACTIVE_NODE_CONTEXT as their context node. this will be the "." ID whose _provenance_parent is the active "c" id. this means that every entity we create has a _provenance_parent identifying that it was created in this environment.
- the task periodically saves things to the active arena. in provenance tracking mode, the active arena would be some sort of ProvenanceArena, which would inspect the _provenance_parent of all the IDs being saved into it, and then actually serialize the provenance information, returning a new ID that attaches the right provenance to the result. this part isn't implemented yet, I think.
- finally the task completes. the runner, which was responsible for setting up the environment, now asks this provenance arena to give it the provenance node representing the provenance of the final result of the computation. it then saves _this_ as the actual result of the job
- the top-level code can either unwrap this to load the result of the job, or use some provenance API to look up provenance info.

> 14. The ACTIVE_NODE_CONTEXT context var - When would this be something other than the "." ground node?

- it would always compare equal to the "." node. however, in certain execution contexts it would have a _provenance_parent that is a "c" node. in this case, we would be able to track the provenance of any entities that embedded it as their "$fxtr.ctx".

> Other

> 15. What's the status of streams (StreamSink, OutputStream)? Are these actively used, or experimental?

these are experimental. however some concept of a stream with an API like this will exist. tbh the whole library is fairly experimental at this point.

> 16. The system_entities.py file - It has relative imports (from entity_core import ...) instead of from fxtr2.data.entity_core import .... Is this intentional or a bug? It would fail if imported as part of the package.

ah, things have moved around, it might be buggy. just fixed it.

> 17. The scratch notebooks - Are these meant to be kept as examples/tests, or are they just development artifacts that can be ignored?

these are sort of half examples, half tests, but they are out of date probably. you should look at them as inspiration for what to write tests about, but after we make real tests you should ignore them.

> 18. Garbage collection - The memory arena has elaborate dead node tracking and cleanup. Is this reference counting? Is there a specific GC strategy I should document?

yes it's basically reference counting. we don't need fancy GC because cycles are impossible to construct normally when using content-addressed immutable data. however, the exact reference set depends on node type and we have a few other invariants to maintain I think
