# Perf Benchmark — Scratch Notes

## Design decisions

### Why not pytest-benchmark?

- These are macro-benchmarks (0.1-2s per run), not micro-benchmarks. Statistical rigor from hundreds of iterations isn't needed.
- Async is a first-class concern; pytest-benchmark has no native async support.
- The setup model we want (shared source, per-iteration target rebuild) is awkward with `pedantic`.
- viztracer integration is much simpler with a custom runner (just run one iteration under tracing) vs fighting pytest-benchmark's repeated-run model.
- pytest-benchmark's main value-add (cross-run comparison via saved JSON) is easy to replicate.

### Key insight: separate source setup from target setup

The user wants:
- Source arena populated once (expensive entity creation)
- Target namespace/scope rebuilt per iteration (what we're measuring)

This means the source scope must outlive the benchmark loop. The target is created and destroyed within each iteration.

### Transfer logic

The core benchmark does one of two things depending on the target type:
- **Pin into namespace**: for each entity ID, call `await namespace.pin(key, entity_id, scope=source_scope)`
- **Copy into scope**: for each entity ID, call `await target_scope.copy_from_scope(entity_id, source_scope)`

But actually, thinking about this more carefully:

For case (a) memory→sqlite: we have entities in a memory scope, we open a SQLiteFxtrNamespace and pin each ID. This means source_scope must be "included" or the namespace's scope must see the source. Actually, `pin` takes an explicit `scope=` parameter, so we pass the source scope directly.

For case (b) sqlite→memory: we have entities in a sqlite namespace. We need to retrieve them and copy them into an isolated memory scope. The retrieve API with `copy_into_scope=True` and `into_scope=target_scope` handles this. But we need all the keys, so we'd iterate over the pinned keys and retrieve each.

Actually wait — for case (b), the "source" is a sqlite namespace that was pre-populated during setup. Then the benchmark timing covers: open an isolated scope, retrieve all entities into it with copy_into_scope=True. But do we re-open the namespace each iteration? We probably should keep the namespace open and just rebuild the target scope each time.

Hmm, but the user said "benchmark cases should NOT share state". Let me re-read... they said benchmark cases shouldn't share state, but within a single case, sharing the source across iterations is fine (that's what we discussed).

### Entity ID tracking

When we create entities in the source, we need to know what IDs to transfer. The entity generator should return a list of (key, entity_id) pairs, since pinning requires string keys.

### For case (b) — sqlite to memory

Setup phase:
1. Create entities in memory scope
2. Open SQLiteFxtrNamespace, pin all entities into it
3. Keep the namespace open as the "source"

Benchmark iteration:
1. Create isolated FxtrScope as target
2. For each key: `await namespace.retrieve(key, into_scope=target_scope, copy_into_scope=True)`
3. Exit target scope

### For case (d) — 20 indexers

Setup phase:
1. Open SQLiteFxtrNamespace
2. Register 20 TypedPathValueIndexer instances (each indexing a different path)
3. Create entities in memory scope

Benchmark iteration:
1. Pin entities into the namespace (indexers fire during pin)
2. Destroy and recreate namespace? Or just unpin?

Wait — if we're measuring the cost of pinning WITH indexers, we need the namespace to have indexers but no entities. So:
- Setup: create namespace, register 20 indexers, create source entities in memory
- Per iteration: create a FRESH namespace with the same 20 indexers, pin entities

Actually, creating a fresh namespace per iteration means re-registering 20 indexers each time. That's expensive setup that isn't what we're measuring. Better to:
- Setup: create namespace, register 20 indexers (empty). Create source entities.
- Per iteration: pin all entities, then unpin them all afterward (cleanup within iteration)

Or: use a fresh db path per iteration (tmp subdirectory). Create namespace + register indexers as part of per-iteration setup (not timed), then time the pin.

I think the cleanest approach is: the target factory creates a fresh tmp dir + namespace + registers indexers each iteration. The factory is split into "setup" (not timed) and the namespace is yielded for timing. So the factory is an async context manager where everything before `yield` is setup and everything after is teardown.

### viztracer integration

Two modes:
1. `--trace CASE_NAME`: run a single case once under viztracer, save to `{case_name}.json`
2. Normal mode: run all matching cases N times, report stats

For viztracer, we'd do:
```python
from viztracer import VizTracer
tracer = VizTracer(...)
tracer.start()
# run one iteration
tracer.stop()
tracer.save("output.json")
```

### CLI interface

```
uv run python examples/benchmarks/store_benchmarks/run.py [OPTIONS]

Options:
  -k PATTERN       Only run cases whose name contains PATTERN
  -n NUM           Number of iterations (default: 5)
  --trace NAME     Run one case under viztracer and save trace
  --json FILE      Save results to JSON file
  --entity-count N Number of entities to generate (default: 1023)
```

## Redesign: Entity scenario × Transfer scenario matrix

### Problem with current design

Each benchmark case hardcodes both the entity generation AND the transfer direction into a single
class. This makes it hard to reason about what's being measured. The `--entity-depth` and
`--payload-size` CLI flags parameterize entity generation, but they don't let you choose between
"one big recursive tree" vs "many independent small objects" — those are fundamentally different
workload shapes.

Also, the current `entity_count` reporting is misleading: for the tree case, we pin one root key
but transfer ~2047 entities recursively. For a "many small" case, we'd pin 500 keys each with 1
entity. These are very different performance characteristics that should be separate scenarios.

### Two orthogonal axes

**Entity scenarios** define WHAT we transfer:
- Shape of the entity graph (tree vs flat vs chain)
- Number of pin keys vs number of total entities
- Payload size

**Transfer scenarios** define WHERE we transfer:
- Source type (memory scope vs sqlite namespace)
- Destination type (sqlite namespace vs isolated memory scope)
- Destination configuration (plain vs with indexers)

A benchmark case = entity scenario × transfer scenario.

### Key design question: how do entity scenarios interact with transfer scenarios?

Transfer scenarios need to know:
1. A list of `(key, entity_id)` pairs to pin/retrieve
2. The source scope (for pin operations)
3. The total entity count (for reporting)

Entity scenarios provide all of this. Transfer scenarios shouldn't need to know anything about
entity shape.

BUT: some entity scenarios require specific entity types. The indexed transfer scenario needs
`RichBenchmarkTreeNode` because it has fields for the indexers to target. A plain
`BenchmarkTreeNode` wouldn't work with indexers that look for `f_int_0`.

Options:
(a) The indexed scenario only pairs with "rich" entity scenarios — we curate the matrix.
(b) The indexed scenario uses generic indexers that work on any entity type (e.g. index `value`
    field which all entities have). Less realistic but more composable.
(c) Entity scenarios declare which transfer scenarios they're compatible with.

I think (a) is the right call. We curate the matrix rather than trying to make everything
composable. Some pairings are just nonsensical (e.g. indexing a flat entity that has no indexable
fields). The matrix is defined in `cases.py` and can be as selective as needed.

### What does an EntityScenario look like?

```python
@dataclass(frozen=True)
class EntityScenario:
    name: str
    description: str
    generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]]
```

The `generate` function is called inside an active scope and returns (keys, entity_count).
No parameters — the scenario is fully self-contained. If you want "big tree" and "small tree",
those are two separate EntityScenario instances.

Concrete scenarios:
- `single_tree_1k`: depth 10, 1KB payload → 2047 entities, 1 pin key
- `many_small_1k`: 500 independent flat entities, 1KB each → 500 entities, 500 pin keys
- `single_tree_1k_rich`: depth 10, 1KB payload, RichBenchmarkTreeNode → 2047 entities, 1 pin key
  (needed for indexed transfer)

These are defined at module level, not parameterized by CLI flags. To add a new entity shape,
you add a new EntityScenario instance. The `--entity-depth`/`--payload-size` CLI flags go away.

### What does a TransferScenario look like?

A TransferScenario needs to manage the lifecycle of both source and target. The tricky part:
different transfers have different source requirements.

- memory→sqlite: source is a memory scope, target is a sqlite namespace
- sqlite→memory: source is a sqlite namespace (pre-populated), target is a memory scope

So the TransferScenario needs to:
1. Create the source from the entity scenario's output
2. Create the target per iteration
3. Execute the transfer

```python
class TransferScenario(Protocol):
    name: str
    description: str

    @contextlib.asynccontextmanager
    async def global_context(
        self,
        entity_keys: list[tuple[str, FxtrID]],
        entity_count: int,
        source_scope: FxtrScope,
    ) -> AsyncIterator[None]:
        """One-time setup. Receives entities from the entity scenario's scope."""
        ...

    @contextlib.asynccontextmanager
    async def iteration_context(self) -> AsyncIterator[None]:
        """Per-iteration setup/teardown."""
        ...

    async def run(self) -> None:
        """The timed operation."""
        ...
```

Wait, but for sqlite→memory, the global_context needs to pin everything into a sqlite namespace
first (as part of source setup), then close the scope, then yield. The entity scenario's scope
would need to stay alive during pinning but can be closed after.

Actually, I think the cleanest approach is:

1. The runner creates a FxtrScope and calls entity_scenario.generate() inside it
2. The runner passes the scope + keys to transfer_scenario.global_context()
3. global_context can do whatever it needs with the source entities (e.g. pin them into sqlite),
   then yields. The runner's FxtrScope stays alive around global_context.
4. After global_context yields, the runner enters the iteration loop.

Hmm but for sqlite→memory, we want to CLOSE the memory scope after pinning into sqlite (to avoid
caching effects). So the transfer scenario needs control over the source scope lifecycle.

Let me reconsider. Maybe the transfer scenario should own the ENTIRE lifecycle, and the entity
scenario is just a callable that gets invoked inside the right scope:

```python
class TransferScenario:
    @contextlib.asynccontextmanager
    async def global_context(
        self,
        generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]],
    ) -> AsyncIterator[None]:
        ...
```

The transfer scenario creates the scope, calls generate() to populate it, then does whatever
source setup it needs (e.g. pin into sqlite), then yields. This gives it full control.

For memory→sqlite:
- global_context: create FxtrScope, call generate(), keep scope alive, yield
- iteration_context: create fresh sqlite namespace
- run: pin from scope into namespace

For sqlite→memory:
- global_context: create FxtrScope, call generate(), pin into sqlite, CLOSE scope, yield
- iteration_context: cold-open sqlite namespace, create isolated target scope
- run: retrieve from namespace into target scope

This works! The entity scenario is just a named callable, and the transfer scenario orchestrates
everything.

### Combining into cases

```python
@dataclass
class BenchmarkCase:
    entity_scenario: EntityScenario
    transfer_scenario: TransferScenario

    @property
    def name(self) -> str:
        return f"{self.entity_scenario.name}/{self.transfer_scenario.name}"
```

The case name is `entity/transfer`, e.g. `single_tree_1k/memory_to_sqlite`.
`-k` filtering works on this combined name, so `-k memory_to_sqlite` runs all entity scenarios
with that transfer, and `-k single_tree` runs all transfers with that entity scenario.

### What about entity_count and pin_count?

The runner needs entity_count for reporting. It also might be nice to report pin_count (number of
pin/retrieve operations) separately since that's what drives the per-operation cost.

EntityScenario.generate() returns `(keys, entity_count)` where `len(keys)` is the pin count.
The transfer scenario stores these after generate() is called. The runner reads them from the
transfer scenario for reporting.

### Summary of final types

```python
@dataclass(frozen=True)
class EntityScenario:
    name: str
    description: str
    generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]]

class TransferScenario(Protocol):
    name: str
    description: str

    def global_context(
        self, generate: Callable[[], tuple[list[tuple[str, FxtrID]], int]]
    ) -> AbstractAsyncContextManager[None]: ...

    def iteration_context(self) -> AbstractAsyncContextManager[None]: ...
    async def run(self) -> None: ...

    @property
    def entity_count(self) -> int: ...
    @property
    def pin_count(self) -> int: ...

@dataclass
class BenchmarkCase:
    entity: EntityScenario
    transfer: TransferScenario

    @property
    def name(self) -> str:
        return f"{self.entity.name}/{self.transfer.name}"

    @property
    def description(self) -> str:
        return f"{self.entity.description} → {self.transfer.description}"
```

### What changes in existing files

- `entity_generators.py`: entity types stay the same. Generator functions become 0-arg callables
  wrapped in EntityScenario dataclasses. Remove the `depth`/`payload_size` params — each scenario
  is a fixed configuration.
- `cases.py`: TransferScenario classes replace the current BenchmarkCase classes. The matrix of
  entity × transfer combinations is defined here. `_make_indexers()` stays.
- `runner.py`: BenchmarkCase protocol changes. `global_context` takes a `generate` callable
  instead of `entity_depth`/`payload_size`. Remove those params from `run_benchmarks` and
  `run_case`. The reporting table gets a `Pins` column.
- `run.py`: Remove `--entity-depth` and `--payload-size` CLI flags. Case names are now
  `entity/transfer` format.
