# Sandboxed service bindings

How handle keys get served, durably. This note is the canonical
statement of the binding subsystem's design: the two binding classes,
the uniform connect contract, the pinned dependency map, the
compatibility ratchet that governs swapping, sandbox lifecycle, and
the exported bindings file. Authority doctrine (handles cross as
keys; authority follows the record) is the design overview's
"Authority and trust" section; this note is about what stands behind
a bound key.

## The two binding classes

A **binding** is runner-level state answering "what serves this
handle key in this runner". There are two classes, and one key may be
bound in **at most one** of them at a time (whichever registration
comes second is rejected — no shadowing, no precedence order to
reason about):

- **Ephemeral bindings** (`bind_handle`): a live connector service
  (`ConnectorProto`) registered for a key. Obligation-based: the
  binder must keep the connector alive; the binding's lifetime is
  bounded by the connector's (fxtr2 scopes/leases), and `bind_handle`
  returns a release *capability*. Never persisted, never exported —
  a reopened or imported workflow re-binds.
- **Sandboxed bindings** (`bind_sandbox`): a *sandboxed service
  definition* — a nix flake reference in the `fxtr_sandboxing` defn
  currency — registered for a key, plus a pinned dependency map and
  an optional config blob. Data, not a live object: saved in the
  state db, exported/imported as the bindings file, owned by the
  runner. There is no release capability — removal is a plain state
  mutation (`remove_sandbox_binding`) — and the record outlives the
  registering connection (the common case: bind from a notebook,
  export, reopen elsewhere).

Serving is uniform: task-fn resolution (the executor's prologue, both
runs) and steps' `connect` go through one funnel
(`bindings.BindingHost.spawn`) that consults both classes; which class
serves a key is invisible to task code.

## The sandboxed binding record

Keyed on the handle **key id** (like ephemeral bindings: the binding
serves that key wherever it appears; it is not attached to a cell).
Fields:

- **defn**: `git_commit`, `flake_attr`, optional `flake_path` — the
  same reference triple as `NixWasmServiceDefn` /
  `NixContainerServiceDefn` / `NixPyodideServiceDefn`; the attr's
  `kind` is read at spawn time: the record spawns as the kind-agnostic
  `fxtr_sandbox_types.NixServiceDefn`, whose auto-dispatch arm in the
  spawner evals the attr's `kind` (cached per environment per triple)
  and runs the matching runtime — the flake attr stays the sole owner
  of its kind. No nix eval happens at bind time (bind is cheap and
  repo-independent; attr validation happens at first spawn).
- **dependency map**: name → *pinned cell selection* — grid path,
  fully-bound coords, and the **expected key id** (see below).
- **config**: an optional JSON value (plain JSON types only — no
  entities, no refs). The home for machine-local, non-identity
  tuning (concurrency limits, timeouts, base URLs).
  Identity-bearing arguments belong in the key entity, never here.

The record is **structured data, not an entity**: state-db columns,
exported as TOML. The defn *entity* is minted transiently at launch
for interop with `fxtr_sandboxing.spawn`; nothing about the binding
lives in the entity store except the obligations below. Liveness:
recording the key id (and each pinned dependency key id) in the
state db obligates their closures into the entity store — `bind`
persists the bound key's closure from the call arena; pinned keys
are read from cells and hence already persisted.

## Bindings are not identity; the compatibility ratchet

No binding, of either class, is identity-bearing: bindings enter no
handle fingerprint and no config equality, and are invisible to
currency and grounding. Consequently swapping a binding never
invalidates results — and that freedom carries an obligation:

> **The ratchet invariant.** Whoever swaps a binding without changing
> its key asserts that every successful past execution of the old
> program would have produced the same result (or a result in the
> same distribution) under the new one: any input that was well typed
> and met the old program's preconditions remains well typed, meets
> the new program's preconditions, and yields an
> identical-in-distribution result. The one permitted direction of
> drift: the new program may accept a *larger* well-typed input set /
> weaker preconditions (inputs the old program rejected with an error
> may gain defined behavior under a new contract).

The sanctioned pattern for dependencies that *should* invalidate
consumers is to put their identity in the key entity (e.g. a judge
handle whose key carries the model name); the binding-level
dependency map is for genuinely swappable wiring (which concrete
connection object serves it).

## Pinned dependencies

The dependency map lets a bound service consume other handles. Each
entry is a scalar cell selection **pinned at bind time**: `bind`
reads each dependency cell, in declaration order — every cell must be
populated — and records its current key id as the pin.

At **every** connect, each dependency cell is re-read and checked
against its pin. An empty cell, a missing grid, or a **pin mismatch**
(the cell now holds a different key) is a fault-class not-ready error
in the `HandleUnboundError` family: the consuming run is interrupted,
retryable after fixing. Rationale: compatible swaps happen at the
*dependency's own binding* under the ratchet; rewriting the
dependency *cell* is an identity-level declaration that behavior
changed, and downstream bindings fault loudly instead of silently
absorbing it. The pins also make the exported bindings file actually
pin what gets connected.

Re-pinning after an intentional dependency change is deliberately
explicit: remove the binding and `bind_sandbox` again (which re-reads
and re-pins) — exactly the "I assert compatibility" act. Pin-check
reads are runner machinery: unrecorded, no generation observation
(consistent with bindings never being identity).

Ephemeral bindings carry the same optional dependency map and config
blob, with identical pin semantics (stored on the in-memory entry,
dying with the release) — the two classes are uniform in everything
except durability and registration lifecycle. Use case: an external
connector consuming a project-scoped sandboxed dependency without
managing the sandbox itself.

## The connect contract: `BindingConnectRequest`

Both classes deliver one request shape to the bound implementation —
`fxtr_sandbox_types.BindingConnectRequest`, a standard interface
`fxtr_experiment` consumers implement against:

```python
@dataclass
class BindingConnectRequest:
    key: ...            # the handle key entity — the fingerprint identity
    config: ... | None  # the binding's JSON config blob
    handles: Mapping[str, ServiceRef[UnknownService]]  # pre-spawned deps
```

- **Ephemeral**: `ConnectorProto.connect(request)` — the connector
  receives the request and returns the service.
- **Sandboxed**: the request *is* the sandbox connect config, riding
  the existing `SandboxConnectProto.connect(config)` seam unchanged —
  the guest entrypoint declares it as its config parameter. The
  per-connect sandbox provenance c-node therefore captures the key
  and the dependency services' replacement markers for free.

An implementation moves between the two classes mechanically (wrap
the entrypoint as a connector or vice versa).

Dependency resolution is **eager and host-side**: at connect the
runner reads + pin-checks each dependency, spawns each through the
same serving funnel (recursively — a dependency may itself be bound
in either class; an explicit in-progress set threaded through the
recursion detects binding cycles), and passes the resulting refs in
`handles`. Missing or unbound dependencies fault *before* the outer
sandbox spawns. The guest receives no spawner and no registry
capability — only the named refs it was wired. All connect-time
failures — unbound key, dependency empty/missing/pin-mismatch, cycle,
repo unconfigured, launch failure — are fault-class (`errors.py`, the
`CellFaultError` family): the consuming run interrupts, retryable
after fixing; launch failures carry the full underlying message (nix
stderr included, unbounded).

## Repo configuration

Sandboxed bindings resolve their `git_commit`s against **one
configured repo per runner** (`open_local(sandbox_repo=…)`; a
runner-side path — configuring it is what constructs the sandbox
launcher, so a repo-less runner never touches the sandbox machinery).
A sibling `sandbox_cache_dir` kwarg locates the environment's cache
(default `~/.cache/fxtr_experiment/sandboxes`); both are ephemeral,
machine-local runner state — never exported — exactly parallel to
ephemeral bindings needing reconnection after reopen/import. Using a
sandboxed binding with no repo configured is a distinct fault-class
error naming the missing configuration
(`SandboxRepoUnconfiguredError`; same not-ready family as unbound
handles, different remediation). The lockfile contract: every bound
commit must be fetchable in the configured repo; a missing commit
surfaces as a spawn fault naming it.

Multi-repo needs are the *flake's* job (one flake importing other
pinned flakes), keeping runner config 1:1 with a repo. The intended
project shape is two repos: a user-managed code repo (code,
renderers, configs) and the exported experiment repo (bindings +
grids + data) pinning commits from the first, cloned together.

## Sandbox lifecycle

One `fxtr_sandboxing` environment per runner (opened lazily on the
first launch, inside the launcher — `sandbox_launcher.py`, the
package's sole `fxtr_sandboxing` importer; the serving funnel itself
is `bindings.BindingHost`).
Sandbox processes are cached **per binding** (not per defn:
shutting down one binding's process can never disconnect a sibling
binding that happens to share the defn), with in-flight dedup so
concurrent runs demanding the same binding share a launch — its
failure included; a *later* demand re-attempts a failed launch (no
negative caching). `connect()` happens per use — each run/step
connect gets a fresh guest dispatch scope, matching the
promised-stateless contract, and takes an independent lease on the
process's holder scope, so the process lives at least as long as
every run using it.

Shutdown is **release-and-forget**: `remove_sandbox_binding` and the
explicit per-binding `shutdown_sandbox` (surfaceable in the UI) drop
the cache entry and signal teardown without waiting — already-
connected guests stay connected, the process exits in the background
when the last connected run's scope releases its lease, and the next
use relaunches fresh. Nothing force-kills in the normal path.
Teardown at runner close orders guests before the environment before
the runner scope. Launch state (unlaunched / launching / running /
faulted, with the faulted attempt's full failure message) is
runtime-only — never persisted (the `run_log` precedent) — and feeds
`BindingInfo`.

**Task-fn guests and the runtime split.** A step-connected *plain
service* guest can run on any sandbox runtime (it implements its own
protocol against `fxtr_sandbox_types` + its runtime's fxtr2 flavor).
A *task-fn* guest must additionally run the task-side runtime
(`task_adapter.TaskFnAdapter` + the view runtime), which it gets by
installing the **`fxtr-experiment-task`** distribution — the task-side
half of the project, deliberately separate so a guest never installs
the runner's state db, executor, or sandbox host machinery. Its
`[frames]` extra (polars, the one dependency with no wasm wheel) is
needed only by task fns that touch dataframe cells or declare frame
outputs; an entity-only task fn installs without it.

What remains runtime-specific is therefore **fxtr2**, not this
project: a task-fn guest needs whichever fxtr2 flavor its runtime can
install. Containers run full Linux CPython and take the plain
dependency, which is what the gated e2e exercises. A wasm guest needs
the `[fxtr2]`/`[fxtr2-portable]` extras dance (the
`fxtr_sandbox_rpc_adapter` precedent); pyodide takes emscripten /
PEP-783 wheels and may resolve full fxtr2 as-is, which would make it
the cheaper first non-container task-fn runtime.

## Export and import

Sandboxed bindings are part of the at-rest state: exported as a
sorted, deterministic, human-readable TOML bindings file in the
at-rest git repo (they act like a lockfile), and restored by import.
This is the deliberate exception to "bindings are ephemeral runner
configuration": *ephemeral* bindings remain unexported and are
re-registered after reopen/import; *sandboxed* bindings travel with
the workflow. An imported workflow's sandboxed bindings sit inert
until `set_sandbox_repo` points at a repo containing the pinned
commits. File-format specifics live with the at-rest format
(`notes/at_rest_format.md`); coords in dependency selections reuse
the at-rest encoding conventions.

## Monitoring

`list_bindings` / the bindings watch family serve both classes with a
class discriminator and, for sandboxed bindings, launch state
(unlaunched / launching / running / faulted); the repo-configured
state is surfaced alongside. `lint` deliberately stays out of binding
health (lint is target-rooted intent structure); repo-unconfigured /
missing-commit / pin-mismatch conditions surface through the bindings
panel and fault messages.

## Deferred

- `rebind` / `refresh_pins` sugar over remove + re-bind.
- An explicit expected-key argument to pre-declare pins for
  not-yet-populated dependency cells.
- Per-defn process sharing (with a documented shutdown blast radius)
  and `max_guests_per_sandbox`-style rollover.
- Recording the defn's drv hash in the bindings file (bind-time
  eval) — the natural anchor for sandboxed code identity in the
  trust boundary.
- Idle-timeout shutdown of unused sandbox processes.
- Negative-caching of repeated failing builds within a session.
