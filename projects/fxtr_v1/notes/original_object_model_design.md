# Fxtr: A configuration and orchestration system for modular, reproducible experiments


## NOTES

### Object model

* An "entity" is some immutable, fixed-schema, content-addressed data on the Hub. Every entity can be converted to DAG-CBOR and then hashed, producing a CID (content ID).
* A "value" is some immutable data on the Hub. Values usually live inside entities. Integers are values. Lists of values are values. Entities are values. Etc.
* A "typespec" describes a particular set of values, and defines how that object should be converted to/from DAG-CBOR. Entities always contain their own typespec, which describes the values held by those entities. Typespecs are themselves values, and some entities (like schema entities) contain typespecs.
* A "schema" is a type of entity, which describes a set of field names and their typespecs. Schemas can be used to construct "data entities", which actually have values that must match those typespecs.
* A "protocol spec" is a type of entity, which represents an interface+spec that a type of object could implement. The protocol spec itself, as an entity, is versioned and immutable.
* An "implementation plan" is a type of entity, which describes how a specific implementation should be provided. It is essentially the configuration needed to specify "what kind of implementation" a given experiment or component needs (e.g. a plan might be "use the official OpenAI API" or "give me the result of a run of this specific task"). It may not specify enough information to *fully* resolve the implementation (e.g. it doesn’t say what your API key should be, or what run of the task to use) but it should provide enough information such that *any* such implementation is "a valid run of the task" or "a valid instantiation of the desired component"
* An "implementation" is a running piece of software that communicates with an async API, either locally or over RPC, with the ability to serve whatever it is doing over a HTTP REST API. Implementations usually (always?) implement specific protocols (although I suppose it might be possible for one implementation to implement multiple protocols). Implementations themselves don’t exactly live on the hub, although they are referred to by the hub, and they are built by tooling that is associated with the hub.
* A "provider" is some code that constructs a (running) implementation from an (immutable) implementation plan. Providers might do stuff like "check whether a package is installed" or "spin up a docker container" or "provide an API key".
* A "provenance record" is some information about what a given provider had to infer/resolve to construct the implementation. For instance, specific pinned package versions. This information should be sufficient to re-instantiate "the same" (morally) object which should behave equivalently, or to fail if no such object can be instantiated in the current system.
  * Stuff like "dependency versions" or "specific runs used for previous tasks" should definitely be in provenance.
  * Stuff like "API keys" probably should NOT be in provenance (because it both doesn’t really matter for correctness, and would not be ok to publish)
* A "task" is an entity that describes a step of an experiment that can be run. Tasks can depend on implementation plans and previous tasks (although technically, depending on a previous task is equivalent to depending on a plan for how to retrieve that task's result)
* A "run" is an entity which declares that a particular task produced a particular piece of data, and lists the provenance of any implementation used when running that task. In particular, each run knows about any previous runs whose data it used.
* A "runset" is a set of runs, such that there is at most one run of any task in the runset, and if run R is in the runset and depends on previous run R', then run R' is also in the runset.

#### Namespace IDs
We want to be able to implicitly namespace blueprint modules so that code written by different
people does not conflict, and that even identical code could in principle have different access
rules. However, we want multiple "versions" of the same code to be possible to preserve the same
ID, e.g. if you add new experiments to a file it should not invalidate your old ones. We also want
to be able to transfer ownership or move it around in the hub.

To handle this, all IDs in the hub are associated with a "blueprint namespace", and the namespace
controls who owns and has modification rights to the objects defined in the namespace. When you push
new objects to the hub, you do so relative to this namespace.

Within a blueprint namespace, objects are equivalent by value. For instance, any entity with the
same data that lives in the same namespace is the same entity.

### Blueprints/Collections, Tasks, and Runs

Eventually, we expect objects to be defined in one of two places:
- A "collection" or "blueprint", which defines entities in python, which are cheap to reconstruct
- A run database, which stores outputs of expensive runs, each of which is linked to a task in a blueprint.

Many operations cannot be done at blueprint definition time and are instead represented as tasks.
A task is code that has to run with access to certain resources, and produces an entity.

When working with the behaviors hub, we assume that
- every run of a task is identically distributed and fungible, and can be replaced with any other,
- we only ever need to look at _one_ run of each task (where multiple versions of a task can be explicitly created by giving each a different tag),

This property means that we can compute a final artifact given any set of existing artifacts, by
walking the DAG of task dependencies similar to a build system.

In many cases, it is useful to aggregate over many samples of a stochastic procedure, such as
aggregating over synthetic scenarios or over samples given a fixed prompt. This can be done by splitting the task into multiple tasks which each have a different tag.
