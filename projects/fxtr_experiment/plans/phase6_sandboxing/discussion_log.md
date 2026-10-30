# Phase 6 — sandboxed service bindings: discussion log

Working log for the phase-6 design rounds. Same policy as
`notes/discussion_log.md`: this is a rationale trail, never the source
of truth — by the time a commit lands, every decision here must also
live in the self-contained docs rooted at `notes/design_overview.md`.

## Round 2 (2026-07-20) — chunk-3 start: serving design

Settled the chunk-3 open questions; the decision record is the
rewritten `chunk3_serving.md` (+ `chunk3pre1` / `chunk3pre2` /
`chunk3post`). The rationale highlights beyond what those files carry:

- **Task-fn guest portability (Q1)**: tracing the import graph found
  *two* independent wasm blockers — `TaskFnAdapter` lives in
  `task_service.py`, which eagerly imports `ops_engine` →
  `state_store`/`storage` (sqlite), and the package hard-depends on
  polars (no wasm wheel). Decision: v0 targets runtimes where
  `fxtr_experiment` installs directly (container now; pyodide
  ideally later), with the *module* split in-chunk and a *physical
  package split* as a followup commit. A `fxtr2-portable`-style
  force-include twin was proposed and **rejected**: that pattern was
  a bootstrap hack justified by full-fxtr2's heavy deps; the task
  side splits physically with zero duplication, so the honest shape
  wins. One real container task-fn e2e is in scope — without it
  nothing proves the pieces compose (`ViewGrant`/capability-ref
  bridging across the sandbox RPC boundary is genuinely new surface).
- **Module placement (Q2)**: a serving layer *above* a surviving
  `BindingRegistry` was rejected as bolt-on; the from-scratch design
  is **one binding authority** (`BindingHost`) owning ephemeral
  entries, durable-record consultation, the process cache, launch
  states, and the funnel — a thing named "registry" that launches
  sandboxes would be misnamed.
- **Kind discovery (Q3, → pre-commit 1)**: the record's triple has no
  runtime discriminator while `spawn` dispatches on entity type.
  Storing `kind` in the record (drifts from the flake-owned fact) and
  evaling nix in `fxtr_experiment` (wrong system) were both rejected
  in favor of an auto-dispatch mode in the sandboxing system itself,
  scoped as its own commit.
- **Shutdown-while-running (Q4)**: an initial concern that dropping
  the cached process would force-cut in-flight guests was **wrong** —
  connect results are chain-bridged, so a run's hold keeps the
  process alive regardless of the cache's own hold. Hence
  release-and-forget: non-blocking lease release, connected guests
  drain naturally, verified by an in-flight-survives-shutdown test.
- **Failure messages (Q6)**: defensively bounding nix stderr was
  rejected — no evidence of a size problem; presentation-layer
  concern if one appears.
- **Safety-pass classification (→ pre-commit 2)**: reading the
  executor surfaced that the pass *re-invokes the task fn*, so its
  setup shares the run's not-ready surface, but its blanket except
  arm lands `errored` — already wrong for an ephemeral binding
  released between run and pass, and about to get worse with
  per-connect pin checks. Fixed as one shared escape-classification
  rule (fault-class ⇒ interrupted at both sites), its own commit.
- Q5/Q8 landed as leaned: ephemeral uniformity via a shared bind-time
  pin helper; the cycle check as an explicit in-progress parameter
  (no ContextVar). `cache_dir` is an `open_local` kwarg with a
  machine-local default, parallel to `sandbox_repo`.

## Round 1 (2026-07-17) — the design shape

### Goal

Let a user register a *sandboxed service definition* (the
`fxtr_sandboxing` defn currency: a git commit + flake attr) as the
thing that serves a handle key, instead of manually spawning a sandbox
and binding the resulting live service. The registration is durable
runner-level state: saved in the state db, exported/imported as a
human-readable bindings file (lockfile-like), and **never
identity-bearing** — it does not enter any handle fingerprint or
config equality, so swapping it never changes cache identity. Whenever
the runner needs the service (task-fn resolution, step `connect`), it
spins the sandbox up via `fxtr_sandboxing` and connects; the sandbox
may be shut down when no longer used.

### Decisions

**1. Two binding classes, one serving path.** The existing ephemeral
connector bindings (`BindingRegistry`) stay as they are; *sandboxed
bindings* (naming provisional) are a second, durable class. Resolution
consults both — the executor's fn-resolution prologue and steps'
`ctx.connect` are unchanged; a registry miss in one class falls to the
other. Registering a handle key in **both classes at once is an
error** (whichever registration comes second is rejected; since
double-registration is impossible, the maps can be checked in either
order). No shadowing: an ephemeral bind over a sandboxed binding was
considered as a dev override and **rejected** — it would change
behavior without changing identity, and being "assumed temporary" is
exactly the shape the compatibility ratchet (decision 3) forbids.

**2. The durable binding record.** Keyed on the handle *key id* (like
ephemeral bindings — any cell holding that key is served). Fields:

- **defn**: `git_commit`, `flake_attr`, optional `flake_path` — the
  same reference triple as `NixWasmServiceDefn` /
  `NixContainerServiceDefn` / `NixPyodideServiceDefn`; the `kind` is
  read from the attr at spawn time, as the spawner already does.
- **dependency map**: name → *pinned scalar handle-cell selection*
  (grid path, fully-bound coords, expected key id) — decision 4.
- **config**: optional JSON blob (plain JSON types only, no entities)
  — the home for machine-local, non-identity tuning (concurrency,
  timeouts, base URLs). Identity-bearing args belong in the key
  entity, never here.

Stored as **structured columns in the state db** (a new table), not as
an entity — human readability and the lockfile role win; the defn
*entity* is minted transiently at launch purely for interop with
`fxtr_sandboxing.spawn`. Exported as a sorted, deterministic TOML
bindings file in the at-rest git repo; import restores it. This
**amends the at-rest doctrine**: "bindings are deliberately not
exported" becomes "*ephemeral* bindings are not exported; sandboxed
bindings are — they are the lockfile." Liveness note: recording the
key id (and the pinned dep key ids) in the state db obligates their
closures into the entity store; `bind` persists the key's closure from
the call arena (dep keys are read from cells, hence already
persisted).

**3. The invalidation doctrine and the compatibility ratchet.**
Bindings of either class are invisible to currency and grounding.
The invariant to document wherever the trust boundary is described:
**swapping a binding without changing its identity is an assertion by
the swapper that every successful past execution would have produced
the same result (or a result in the same distribution)** — i.e. any
input that was well typed and met the old program's preconditions
stays well typed, meets the new program's preconditions, and yields an
identical-in-distribution result. The one permitted direction of
drift: the new program may accept a *larger* well-typed input set /
have *weaker* preconditions (previously-rejected inputs may gain
defined behavior). The sanctioned pattern for dependencies that
*should* invalidate consumers is to put their identity in the key
entity; the binding-level dependency map is for genuinely swappable
wiring.

**4. Pinned dependencies, checked at every spawn.** Dependency
selections are read **at spawn time** (not snapshotted values), but
each carries a pin: at bind time the runner reads each dependency
cell **in declaration order**, requires it populated, and records its
current key id. At every subsequent connect, the cell is re-read and
compared to the pin; an empty cell, a missing grid, or a **key
mismatch** produces a fault-class not-ready error ("the pinned cell's
behavior has changed") in the same family as `HandleUnboundError` —
run interrupted, retry after fixing. Rationale: compatible swaps
happen at the *dependency's own binding* layer under the ratchet
contract; changing the dependency cell's key is an identity-level
declaration of changed behavior, and downstream sandboxed bindings
should fault loudly rather than silently absorb it. This also makes
the bindings file actually pin what gets connected. Alternatives
considered: pure read-at-spawn with no pin (silent absorption —
rejected), snapshot-at-bind of the key with no cell address
(uncompositional, unreadable file — rejected).

- **Re-pin flow (v0)**: no dedicated method — `remove` + re-`bind`
  re-reads the cells and pins their current keys, which is exactly the
  explicit "I assert compatibility" act. A `rebind`/`refresh_pins`
  sugar is a deferred follow-up.
- **Bootstrap order**: bind-time pinning requires the dependency
  cells to be populated first (write handle cells → bind the things
  that depend on them). Accepted as healthy; an explicit
  expected-key argument for pre-declaration is a deferred escape
  hatch.

**5. Eager dependency resolution; no spawner in the guest.** At
connect, the runner reads + pin-checks each dependency, spawns each
through the same registry machinery (recursively — a dependency may
itself be sandbox-bound; an in-progress set detects cycles), and
passes **pre-spawned ServiceRefs** into the connect config. Chosen
over a lazy per-name spawn capability because missing/unbound
dependencies surface *before* spawning the outer sandbox. wf2's
design (pass the whole spawner + handle entities, guest spawns on
demand) is dropped — it was an authority leak (a guest could spawn
any registered handle) that wf2's own sketch flagged.

**6. One uniform connect contract for both classes.** A single shared
dataclass — provisionally `BindingConnectRequest`:

```python
@dataclass
class BindingConnectRequest:
    key: ...            # the handle key entity (fingerprint identity)
    config: ... | None  # the binding's JSON blob
    handles: Mapping[str, ServiceRef[UnknownService]]  # pre-spawned deps
```

used **both** as the argument to `ConnectorProto.connect(request)`
(external bindings) and as the sandbox guest entrypoint's connect
config (riding the existing `SandboxConnectProto.connect(config)` seam
unchanged — so the per-connect sandbox provenance c-node captures the
key + replaced dependency-service leaves for free). External bindings
get the dependency map and config blob too (`bind_handle` grows the
same optional fields), so an implementation moves between external and
sandboxed mechanically; concrete use case: an external connector
consuming a project-scoped sandboxed dependency without managing the
sandbox itself. The dataclass lives in **`fxtr_sandbox_types`**,
described as a standard interface used by fxtr_experiment consumers
(guests must import it; fxtr_experiment is taking the sandboxing dep
anyway). It is **not** wf2's `StatelessServiceConfig` revived — all
three fields differ (pre-spawned refs instead of handle entities + a
spawner; plain JSON instead of a config entity; adds the key).
Open mechanics: FxtrID vs EntityRef for `key` (the guest needs the
closure covered by the connect arena; see the design overview's open
question 3).

**7. Registration lifecycle asymmetry is deliberate.** Ephemeral
`bind_handle` keeps its release *capability* and its obligation
model — the binder must keep the connector service alive until
release, and the binding's lifetime is bounded by the connector's.
Durable `bind_sandbox` (naming provisional) is a plain state mutation
with a `remove_sandbox_binding`-style method: the record is just data
the *runner* owns the lifecycle of, and it stays valid (and
exportable) after the binder's RPC connection goes away — which is
the common case.

**8. Repo configuration.** One configured repo per runner
(`set_sandbox_repo`-ish), **ephemeral, machine-local** runner state —
never exported, exactly like external bindings needing reconnection.
Sandboxed bindings with no repo configured fault with a distinct
fault-class error (`SandboxRepoUnconfiguredError`, name provisional) —
same not-ready family as unbound handles, but naming the different
user action required. The lockfile contract: every bound `git_commit`
must be fetchable in the configured repo; a missing commit surfaces as
a spawn fault naming it. Envisioned project layout: two repos — a
user-managed code repo (code, renderers, configs) and the exported
experiment repo (bindings + data + grids) pinning commits from the
first, cloned together; per-binding repo imports are the *flake's*
job (one flake importing other pinned flakes), keeping runner config
1:1 with a repo.

**9. Lazy nix eval.** No eval at bind time — bind stays cheap and
repo-independent (you can import a workflow and rebind before ever
configuring a repo); attr validation happens at first spawn. Recording
the drv hash into the bindings file (bind-time eval would be the
moment) is deferred — it is the natural future anchor for the
"sandboxed-task code identity in the trust boundary" deferred item.

**10. Process lifecycle.** One `SandboxEnvironment` per runner (runner
scope). Process cache keyed **per binding** (not per defn): clean
shutdown semantics — shutting down one binding's process can never
disconnect a sibling binding sharing the defn; per-defn sharing is a
deferred optimization with a documented blast radius. `connect()` per
use (fresh guest dispatch scope per run, matching the
promised-stateless contract), with in-flight dedup so concurrent runs
share a launch. v0 release policy: live until runner shutdown, plus an
explicit per-binding shutdown affordance (surfaceable in the UI).
`max_guests_per_sandbox`-style rollover: deferred.

**11. Dependency layering.** `fxtr_experiment` adds a dependency on
`fxtr_sandboxing` (the sandboxed case is the common case); the
guest-facing request dataclass sits in `fxtr_sandbox_types`
(decision 6).

**12. Monitoring touchpoints.** `BindingInfo` and the bindings watch
family grow a class discriminator (ephemeral vs sandboxed-durable)
plus launch state (unlaunched / launching / running / faulted); the
repo-configured state is surfaced alongside. `lint` stays out of
binding health in v0 (lint is target-rooted intent structure); "repo
unconfigured / commit missing / pin mismatch" live in the bindings
panel and fault messages.

**13. The setup commit (uniform fingerprint passing).** Today
`ConnectorProto.connect()` takes no arguments — externally-bound
connectors never see which key they serve. Independently useful fix
(one connector serving a family of handles differing only in key
args), landing as a separate setup commit ahead of the rest.
**Confirmed**: one wire change only, going **straight to the request
shape** (`connect(request: BindingConnectRequest)` with `config=None`,
`handles={}` dormant) — this is the moment the `fxtr_sandbox_types`
dep arrives, and it ripples to every `ConnectorProto` implementor
(the demo drivers in `examples/fxtr_experiment_demo` and the test
connectors). Chunk 1 of `overview.md`.

### Deferred follow-ups (this round)

- `rebind` / `refresh_pins` sugar over remove + re-bind.
- Explicit expected-key argument to pre-declare pins for unpopulated
  cells.
- Per-defn process sharing (with documented shutdown blast radius);
  `max_guests_per_sandbox` rollover.
- drv-hash recording in the bindings file (bind-time eval), as the
  trust anchor for sandboxed code identity.
- Negative-caching of repeated failing builds within a session.
- Idle-timeout shutdown of unused sandboxes.

### Open questions (to settle in the next rounds / plan chunking)

- Naming: "sandboxed binding", `bind_sandbox` / `remove_sandbox_binding`
  / `set_sandbox_repo` / `shutdown_sandbox`, `BindingConnectRequest`,
  the bindings file name (`bindings.toml`?) — all provisional (the
  design note uses these pending implementation).
- The exact TOML schema (coords encoding should reuse the at-rest
  conventions) and the state-db table shape.
- FxtrID vs EntityRef for the request's `key` field (closure coverage
  mechanics for guests).
- Registry/spawn mechanics: `BindingRegistry.spawn` must now build the
  request (dep reads + pin checks need a session seam); where the
  sandbox launcher + per-binding process cache live relative to the
  registry.
- Pin-check reads are runner machinery — unrecorded, no generation
  observation (consistent with binding ≠ identity); to state
  explicitly in the execution-semantics doc when consolidating.
- Error taxonomy names: `SandboxRepoUnconfiguredError`, the
  pin-mismatch fault, commit-missing surfacing.
