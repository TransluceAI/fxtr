# Runner — code overview

The runner (`fxtr/runner/`) executes experiment definitions. This document covers the parts every
backend shares: the run-session protocols, the worker that runs user code, the per-attempt
contexts and the node representation; and, at the end, the `experimental/` package,
the multiplexed shape of execution (a worker elsewhere, fed over one
message stream). The Postgres-backed runner has its own overview at
`postgres/CODE_OVERVIEW.md`. For the surrounding concepts (entities and content
addressing, arrays and queries, experiment definitions) see `fxtr/CODE_OVERVIEW.md`.

```
runner/
├── session.py      the worker-facing protocols and their vocabulary: the per-run
│                   WorkflowRunSession / StepRunSession, attempt ids, node tokens, assignments,
│                   errors, the failure policy, the id contract
├── worker.py       run_assignment and the step / workflow variants: the in-process executor's
│                   job, over a FunctionRegistry (experiment/registry.py)
├── contexts.py     RunnerWorkflowContext, RunnerArrayHandle, RunnerStepContext: the user-facing
│                   API of one run, over its run session
├── query_codec.py  the strict canonical codec for query plans, which a query node carries
├── assembly.py     the shared host side of a run: WorkflowAttemptOps / StepAttemptOps (what a
│                   backend's attempt object offers), WorkflowRun / StepRun (the run sessions over
│                   them: token translation and dispatch by mode), the NodeAssembler, the Verifier
│                   behind determinism verification
├── driver.py       RunExecutor (the seam: execute one run), drive_workflow_attempt (the primary
│                   run, then a verification run if verification held the return), and the
│                   in-process executor
├── nodes.py        Node: the content-addressed form of a DAG node, its identity, its row shape,
│                   and its semantics: node_type, compute, expand, collect
├── steps.py        StepRequest (the fingerprint entity: one concrete call of a step) and the
│                   cache key formula
├── cache_addresses.py   cache addresses read back into literal text and mapped key groups; AddressPattern
│                   (a key whose values may be *, and the keys it selects), summarize (keys
│                   grouped by shape), under_prefix
├── experimental/   the multiplexed shape (see "Experimental" below), off the local launch path
│   ├── multiplexed.py  the WorkerSession protocol (the run sessions with the attempt id as an
│   │               argument, plus registration, the message stream, the drain), MultiplexedHost
│   │               (the host end: an executor toward the driver, a WorkerSession toward the
│   │               worker; credit, drain, Shutdown) and bind_run (the worker end: a run session
│   │               over a WorkerSession and one assignment)
│   ├── worker.py   the Worker loop for a multiplexed session, and function_specs
│   └── remote/     a WorkerSession across a process boundary
│       ├── wire.py     the frames (frozen dataclasses) and their codec (a dedicated cattrs
│       │               converter over cbrrr's DAG-CBOR); WireValue (a value's bytes); the
│       │               error envelope; WireError, ProtocolError, HostError
│       ├── known_ids.py  HostKnownIds / WorkerKnownIds: each end's per-attempt table of the ids
│       │               the worker may use
│       ├── proxy.py    RemoteWorkerSession: the WorkerSession a worker runs on, over a channel
│       └── serve.py    serve_session(channel, session): drives any WorkerSession from a channel
└── postgres/       the durable runner (see its CODE_OVERVIEW.md)
```

## The split: body and host

The design separates the code that runs a user step or workflow **body** and owns no experiment
state from its **host**, whatever owns that state and drives attempts: the Postgres runner or a
remote service. The body's side talks to the host through a **run session** (`session.py`:
`WorkflowRunSession` / `StepRunSession`), the protocol scoped to one
execution of one body, and the host's implementation is the source of truth for what work exists
and what each attempt has done; it drives attempts, and may wait on, cancel, or replay them. The
protocol is message passing throughout: every method is a request, emitting is synchronous and
one-way, waiting happens only where an answer is needed, so an implementation can proxy a remote
service and neither side is tied to the other's concurrency machinery. Whether a body runs in the
host's own process or in a worker elsewhere is decided above the protocol, by the **executor**
(`driver.py`): in process, the body is called directly on the host's run session; multiplexed, a
`MultiplexedHost` (`experimental/multiplexed.py`) sends the run's assignment down a message stream
to a `Worker` loop (`experimental/worker.py`) that calls it on a run session bound over the
multiplexed `WorkerSession`, the same vocabulary with the attempt id as an argument.

## Vocabulary

- An **attempt** is one call of a user function; nodes, observations, and entity stores are all
  attributed to it. On the host it is a backend's attempt object, satisfying the attempt
  operations of `assembly.py`.
- A **run** (`assembly.py`) is one execution of an attempt's body, under one id (its
  `AttemptID`, the "run id"), and *is* the run session the body's context talks to. A
  `WorkflowRun` executes in a *mode*, `primary` (the attempt's own execution of the body) or
  `verification` (the second execution that verifies its determinism, under a fresh id), and carries the run's
  `NodeAssembler`; a `StepRun` is the one execution of a step attempt. To the body's side every
  run is just a run; the host knows which attempt and mode it is.
- An **assignment** is what a run carries for the body's side to execute: `StepAssignment` with
  concrete inputs as array refs, or `WorkflowAssignment` with inputs as tokenized nodes. Over the
  multiplexed protocol it arrives as an `Assigned` message.
- **Function specs** are what a worker on the multiplexed protocol declares it can run, once, with
  `register`: `StepSpec` (a step name) and `WorkflowSpec` (a workflow name), from
  `experimental/multiplexed.py` with the rest of that protocol. `register` also declares
  `max_concurrency`, which the host honours by keeping at most that many runs sent and unfinished
  on the worker.
- A **node** is one entry of a workflow invocation's DAG. The worker never holds one: it
  *describes* each node with one of the synchronous `emit_<kind>` methods of the protocol
  (`emit_source_array`, `emit_combine`, `emit_stack`, `emit_query`, `emit_run_step`, `emit_run_child_workflow`; the operation's parameters, with input
  nodes named by token) and refers to it by a **`NodeToken`**, an opaque `int` naming one node of one run. The host names the inputs
  (a `WorkflowAssignment` carries them tokenized, numbered from zero) and the body's side names
  everything after, choosing each new node's token and passing it in with the call; a reused or
  unbound token is a bug on that side (`NodeTokenError`). On the host a description becomes a `Node` of
  `nodes.py` (below) with a content-derived **identity** (`NodeIdentity`, an `EntityID`), so two
  descriptions of the same operation on the same inputs are one node, and a child's inputs are
  `Input` nodes of its own. A node's contents determine its `ArrayType`, which the host derives
  itself (`node_type`) rather than taking from the worker, except a run node's declared result
  type. Steps and workflows share one name namespace within an invocation.
- Node values (source arrays, step results) are stored by the host. Pathnames and cache keys
  follow the formatting rule of `experiment/workflows.py` (`join_path`, `append_mapped_key`).
- **The id contract** (stated in `session.py`): the only entity ids the body's side passes back to
  the session are ones the session handed it in the same attempt; arrays crossing as values
  (`emit_source_array`, `complete_step`, `observe`) never give it an id. An obligation of that
  side, which the remote adapter also checks.
- **Types on the wire are presentation-free.** A context strips presentation (loaders, TypedDict
  and list presentation; see `entity_defns/schema.py`) off every type it emits
  (`ArrayType.strip_presentation`, and `Array.strip_presentation` for a source array), so a
  backend only ever stores presentation-free types and never holds a presented value.
  Presentation is a worker-side notion the handle keeps, which `observe` re-applies to the array a
  node returns.

Waiting happens only in `observe`, storing, and the completion methods;
emitting is synchronous and returns nothing, so a body can describe its DAG without awaiting (the
context already holds the token). A source array is emitted with the array itself: the host stores
it and records the node at its next commit point. Emissions are one-way and later requests refer
to their tokens, so a transport for a remote session must deliver one session's calls in order.

## How a run ends

A run ends in exactly one of three ways: it **completes** (`complete_workflow` with the returned
node, or `complete_step` with the result array); it **fails**, reported with `fail` and a
serializable `AttemptError` when user code or the runner raised; or the **host ends it**, by
cancelling its execution (the driver's scope; see `driver.py` below), because the attempt was
failed or cancelled on the host's side. On the multiplexed protocol the third case reaches the
worker as `AttemptCancelled` on the `messages` stream and needs no report, and a fourth report
exists, **abandon**, for a worker that stopped running a body for reasons unrelated to it, such as
shutting down. That stream's final message is `Shutdown`; a stream ending without one is a
disconnect, which the worker reports as an error. A worker stopping cleanly calls `drain`, after
which the host assigns nothing more and sends `Shutdown` once nothing is outstanding.

**Failure policy.** An error that originates in experiment state, rather than in user code, fails
the attempt unconditionally. A node whose computation failed makes `observe` raise
`NodeFailedError`. The attempt is already failed when the call raises, so the
body cannot recover by catching: the host refuses everything further from it with
`AttemptClosedError`. The only errors a body may usefully catch are the ones its own code raised.
Error and cancellation handling are otherwise the worker's; the host learns outcomes only from
the reports.

## `contexts.py`: the user-facing API of one run

`RunnerWorkflowContext` (over a `WorkflowRunSession`, whose assignment it reads) and
`RunnerArrayHandle` (a context, a node token, and a type) compute each operation's result type
locally with the static typing functions, then describe the node to the session with the matching
`emit_<kind>` method, naming it with a fresh token from the context's one counter (started above
the assignment's input tokens) and its inputs by the handles' tokens, keeping the type on the
handle. The context computes no identities and imports nothing from `nodes.py`; a source array is
sent presentation-free with its description.

A query the workflow function builds (`handle.query()` or one of the handle's query shortcuts,
or `fxtr.sql` over handles) is an
`experiment/handles.py` `ArrayHandleQuery`: a `PlannedQuery` whose sources are the handles it
reads, type-checked and frozen at each call, and emitting nothing. Its `collect()` hands the
query to `RunnerArrayHandle._stored`, which checks every source belongs to the attempt, replaces
each with its node's token, and emits the whole chain as one `emit_query`, returning a handle to
the new node. A query that only reads one handle and changes its presentation names that node
instead. A `retype` that changes more than presentation emits its query node at once. `node_token_of` refuses a query that was
never collected, so one passed to a context operation or returned says to call `collect()`.

`RunnerStepContext` (over a `StepRunSession`) exposes the cache key. A step receives its inputs
as concrete arrays and queries them locally. Both forward the raw storage methods to the session, and both expose
`ctx.log(str)`, forwarded to the session's `log_message` — free-form per-attempt log lines, kept for
observability and never deduped across a replay (each attempt writes its own). The host drops a
verification run's lines, so a line is recorded once even though the body runs twice. Their public
surface is exactly the user-facing interface; the worker is privileged, constructing
`RunnerArrayHandle`s for an attempt's input nodes directly and using the module-level
`node_token_of(context, handle)` to read the token behind the handle a body returns. Handles are
bound to the context that made them (`ForeignArrayHandleError` otherwise), and source array, step,
and child workflow names share one per-attempt namespace (`DuplicateNodeNameError`).

## `nodes.py`: what a node is (host-side)

A node is a `DataclassEntity` of one kind (`Input`, `SourceArray`, `Combine`,
`Stack`, `Query`, `RunStep`, `RunChildWorkflow`; the union `Node`) whose
contents determine it completely: parameters, input nodes by identity, and for leaves a `BoundID`
to the array. Its **identity** (`node.content_id()`) is the cid it would have as a stored entity, computed with
the codec and never stored in the entity store, so identical operations on identical inputs
coincide whatever order a body emitted them. A `Query` node's contents are its inputs, in the
order the plan's `QuerySource` positions index them, and the plan in the canonical encoding of
`query_codec.encode_query`, so two equal plans over equal inputs are one node. Only run nodes carry an `ArrayType`, the result type
the worker computed from the user-declared signature (the one place declared types enter); every
other kind's type follows from its contents, and `node_type(node, input_type, array_type)` derives
it by that rule. Postgres records it in the durable `nodes.type` column as a derived attribute.
Nodes are built only on the host, by the `NodeAssembler` (below) from the worker's descriptions and
by the host itself for `Input`
leaves. `node_inputs` yields the edges (argument names,
stack keys, or, for a query node, the position its plan's `QuerySource` reads the input
at — the same input read twice appears twice), `node_name` the name a node claims, and
`compute(node, inputs)` a derived node's value from its inputs' arrays — for a query node, by
running its plan over them with `duckdb_engine/local_query.py`'s `query_arrays`, imported inside
the call so a process that only describes a DAG does not load DuckDB and PyArrow. Run nodes have the other half of the semantics:
`expand(node, inputs)` gives the invocations a run node requests once its inputs have values (an
`Expansion`: the mapped dims and one `Spawn` per joint key and replica key, each with the mapped
values that name the invocation and the inputs sliced to that key), and `collect(node, values)`
gives the run node's value back from the spawns' results (widened to the declared schema, stacked
along the mapped and replica dims, transposed to the declared dim order). Expansion slices each
input once along the mapped dims it has and looks each joint key's slice up, so it reads every row
once however many keys there are; replicas and inputs that lack a mapped dim share slices. Every
backend expands
and collects with these two; what a backend does with a spawn (create a step or child workflow
invocation) is its own. `to_row` / `from_row` convert to and from the
relational shape (`NodeRow`: a `kind` string from `KINDS`, `params` as JSON with array references
as cid strings and a run node's type via the JSON codec, and the edges), from which the same
identity is recomputed.

## Query nodes

A workflow's query nodes run on the host, over their inputs' arrays. A query the body collects (see
`contexts.py` above) becomes one `Query` node whose contents are its inputs and its plan in the
canonical encoding of `query_codec.py`: a strict DAG-CBOR codec for the plan nodes and their
recursive expression nodes (arithmetic, cast, null replacement, membership, range, conditional,
and Boolean), which writes `group_by` as a list of pairs, since DAG-CBOR sorts map keys and the
order of the groups is the order of the result's dimensions. `nodes.compute` runs a query node's
plan over its input arrays with `duckdb_engine/local_query.py`'s `query_arrays`, synchronously and
on one DuckDB thread, as it computes any other derived node, and the node's value holds only the
result table. That result is stored only where any value is: when it feeds a step or a child
workflow, or is returned. A result naming an entity the storage does not have
(`UnknownReferenceError`) makes the run node it feeds fail, or fails the attempt when it is
returned. Queries themselves (the query builder, SQL, the plan nodes, compilation, DuckDB execution
and its limits) are described in `../entity_defns/CODE_OVERVIEW.md`.

The experimental remote transport does not carry `emit_query`: a workflow that collects a query
while running on a remote worker fails with `experimental/multiplexed.py`'s
`QueryNotSupportedError`.

## `steps.py`: what a run node asks for at one key

Once a `RunStep` node is expanded, each spawn is a concrete call: this step, on these exact input
arrays. That call is a `StepRequest`, a `DataclassEntity` holding the step name and the sliced
inputs by reference; its cid (`fingerprint_of`) is the call's **fingerprint**, everything about
the call and nothing about who asked. Two requests with equal fingerprints are the same call and
may share a result; a cache key already holding a different fingerprint is a conflict. The
declared result type is deliberately not part of it: the requesting node declares that, and a
result must be assignable to it where it is consumed. Nothing about the step's code is part of it
either: the step's name is its whole identity, so a cached result stays matched across code
changes until its key is cleared (`clear_cache_entries` on the client).
`cache_key(node, mapped)` is the address a spawn's result is saved under, the node's prefix with
the mapped key appended; equal requests at different keys are different calls, which is how a key
breaks the symmetry of a nondeterministic step. `request_for(node, refs)` builds the request for a
spawn. The Postgres step cache uses these keys and compares fingerprints to detect a conflict.

`cache_addresses.py` reads a key back into its parts: literal text, and the `[dim:value,...]` groups
`append_mapped_key` wrote, values as JSON (so a value may hold `/`, `[`, or `,`). A `AddressPattern`
is a key whose values may be `*`; `summarize` groups keys by shape (the key without its values)
under the pattern that keeps the values a group shares, which is how a job's cache conflicts are
shown. `under_prefix` is the prefix test the cache selections use; `AddressPattern.selects` extends
it to patterns, which the cache selections accept wherever they take a prefix (a `*` anywhere but
a whole value is a `PatternError`).

## `assembly.py`: attempt operations, runs, and the assembler

Everything a body's side is handed is a **run** (see "Vocabulary"): `WorkflowRun` (id, attempt,
mode, assembler, `verify_determinism`) or `StepRun` (id, attempt). A run *is* the run session its body's context talks to (`WorkflowRunSession` /
`StepRunSession` in `session.py`), and it is generic over the backend's attempt object, which
must satisfy the **attempt operations** protocol `WorkflowAttemptOps` / `StepAttemptOps`: the
identity-based operations of one attempt (recording: `emit`, `emit_source_array`, `log`,
`observe`, `record_return`, `hold_return`; verifying: the `verify_*` twins,
`verification_failed`, `verification_phase`; ending: `fail`, `abandon`, `flush`; plus `ended`,
`entities`, and the invocation's `workflow_name`, `pathname`, `cache_key_prefix`,
`inputs`). The split is what is per execution versus per attempt: the run owns the assignment,
the token table, and the mode, and every session method on it is "resolve tokens, build the node,
dispatch to the attempt by mode"; the attempt owns the DAG, the record, the verifier, and the
lifecycle, speaks only in identities, and outlives its runs. Written once here, the dispatch is
shared by every backend. `WorkflowRun.start(attempt, mode, run_id=, verify_determinism=)` seeds the assembler
from the attempt's inputs and builds the assignment. Ending begins synchronously on the attempt
(`fail` / `abandon` end it and arrange the durable write of its outcome), which is what lets a
synchronous `emit_<kind>` fail the attempt on a determinism violation.

The **`NodeAssembler`** is one `WorkflowRun`'s translation from the worker's tokens to nodes: a
token → identity table (`bind`, `resolve`) and one builder per `emit_<kind>` (`source_array`,
`combine`, `stack`, ...), each of which resolves the description's input tokens, builds the `Node`,
computes its identity (the one place a node is hashed), binds the worker's token to it, and returns
the node for the session to dispatch on the run's mode. `seed_assignment` binds the invocation's
input nodes to tokens `0, 1, ...` and builds the run's `WorkflowAssignment`; the assembler never
mints a token otherwise. It does not deduplicate (two descriptions of one operation are two tokens
bound to one identity; the backends' "same identity is a no-op" rule handles the node), holds no
values or types, and sorts `stack` inputs by key. Tokens are per run, so a verification run has its
own assembler and its own numbering. Both backends assemble in the session, before dispatch, so
attempts and records stay identity-based.

## `driver.py`: the driver and the executor seam

Determinism verification needs a body to run twice, and deciding that the second run happens is the one piece
of sequencing above a run. `drive_workflow_attempt(attempt, execute, run_id=, verify_determinism=)` does it,
once, for every host: execute the primary run; if the attempt is still open and its phase is
`verifying` (the return was held), execute a verification run under `verification_run_id(run_id)`.
A run never starts runs. `execute` is a `RunExecutor`, a callable that executes one run session
until it has ended, however that happens: in process by running the body in the current task, or
by a multiplexed host sending the assignment to a worker elsewhere and awaiting its completion.
Everything above the seam is shared, so the two never differ in what an execution means.

## `worker.py`: running attempts

A `FunctionRegistry` (`experiment/registry.py`) holds the steps and workflows a worker can run by
name; the decorators fill the process's `GLOBAL_REGISTRY` at import time, which every runner uses
unless handed another, and a name registered by more than one function fails only when it is looked
up. `run_assignment(session, registry)` takes a run session, dispatches on its assignment's kind to `run_step_attempt`
(loads the inputs through the step context, calls `Step.invoke`, completes) or
`run_workflow_attempt` (builds the context and input handles, calls the body, checks the returned
handle's owner and type against the signature, completes with its node), reporting any `Exception`
to the session via `fail` and letting cancellation propagate. This is all the in-process executor
does (`driver.in_process_executor(registry)`).

The worker runs each assignment exactly once and verifies nothing itself; see the next section.

## Determinism verification: the host's verification run

**Determinism verification** (`verify_determinism`, a setting of each session, on by default) is a
lint for workflow bodies in the manner of React's strict mode. When a body returns, the host does not record the return: it holds
it, hands the worker a **verification run** of the same attempt (the same assignment under a fresh
id, which the worker runs like any other), and checks everything that run does against what the
attempt's body did instead of recording it. The verification run must emit the same nodes,
observe the same nodes, and return the same node; then the host records
the held return through the normal path and the attempt completes as usual. A difference, or the
verification run's body raising, fails the attempt with `DeterminismError`, recorded by the host;
the run's body sees only `AttemptClosedError`, as for any host-originated failure. Nothing of the
verification run is recorded: its emissions are checked, its observes are served from the live
DAG (complete, since the body observed them), its log lines dropped, its stores idempotent.

`assembly.py` also holds the `Verifier`, one
per workflow attempt, which notes what the body did (`note_emit`, `note_observe`,
`note_return`, which holds the return and starts the `verifying` phase) and checks the
verification run (`check_emit`, `check_observe`, `check_return`, which ends in
the `agreed` phase or, on the first difference, `failed`). The comparisons are on sets, since a
deterministic body using a task group acts in varying order. Observed values are not compared
(the verification run is served the live values, which cannot differ), but the set of nodes
observed is: a body that looks at different things took a different path. The body's side effects
(prints) happen twice. The verification run is handed to the same worker, so nondeterminism through
process-local state (a module-level cache) is shared by both runs and not caught; the ledger's
replay check on a real resume catches it. The semantics reference runs with verification on. A
job's setting comes from whoever runs it: the local project client's `submit` and `resume` take
`verify_determinism`, defaulting to the client's own setting (on unless the client was made with it
off).

## Experimental: the multiplexed shape

`experimental/` holds the other implementation of the executor seam: a worker in another process,
fed assignments over one message stream, with the host implementing that stream's worker-facing
end. Nothing on the path a local launch takes touches it — a launch runs its bodies in its own
process, through the in-process executor — and it is kept in running form for a persistent-worker
executor (a worker process that outlives one job and is fed work over a connection). What keeps it
honest is the reference suite's remote Postgres entry, which runs the whole semantics suite
through it.

`WorkerSession` (`experimental/multiplexed.py`) is the multiplexed protocol: every method of the
run sessions but `emit_query` with the attempt id as an argument, so one worker's session carries many runs, plus
what belongs to the worker rather than to one run — `register` (the function specs and
`max_concurrency`), `messages()` (the host's stream of `Assigned`, `AttemptCancelled` and, last,
`Shutdown`), and `drain`. The errors that belong to it, `AlreadyRegisteredError` and
`HostDisconnectedError`, live there too.

### The multiplexed host

`MultiplexedHost` is the host end of a multiplexed `WorkerSession`, with two faces. Toward the
driver, its `execute(run)` is a `RunExecutor`: register the run under its id, wait until a worker
has registered and fewer runs than its concurrency are sent and unfinished, send
`Assigned(run.assignment)`, and await the run's end, which the forwarded completion, `fail`, and
`abandon` calls set. **Stopping a body is cancelling the driver**: the driver owns the runs it
executes, so a backend's `cancel_body` cancels the scope the driver runs in; through this executor
the cancelled `execute` sends `AttemptCancelled` for a run already sent (nothing for one still
waiting), ends it, and no verification run starts. Toward the worker it implements
`WorkerSession` by forwarding each attempt-keyed call to the run session registered under that id
(a wrong-kind or unknown id is an `AttemptClosedError`), and owns what is not a run's business:
`register` (once), `room` (how many runs `execute` would send at once rather than queue: the
concurrency minus every unfinished run handed to it, for a scheduler that wants nothing waiting),
`drain` (stop accepting; `Shutdown` once no run is outstanding and the backend's
`ready_to_shutdown` says so, re-checked by `maybe_finish_drain`), `shutdown`, and an `on_credit`
callback fired when a run ends. It holds no policy: what runs exist, when they are executed, and
when they are stopped is the caller's.

### The worker loop

`Worker(session, registry, max_concurrency)` (`experimental/worker.py`) is the loop for a worker on
a multiplexed `WorkerSession`: `run()` registers `function_specs(registry)` and the concurrency,
then reads `messages`, starting each `Assigned` attempt in its task group under its own cancel
scope, on `bind_run(session, assignment)` (the run session that binds the attempt id into every
call), so the contexts and `run_assignment` are the same code whichever session is behind them (the
worker runs whatever it is assigned; the host enforces the concurrency). `AttemptCancelled` cancels
the attempt's scope silently, while any other cancellation (`Shutdown`, a broken stream, the worker
cancelled) makes the attempt report `abandon` under a shielded, time-bounded scope before
propagating; a broken stream additionally raises `HostDisconnectedError` from `run()`.

### The remote session

`experimental/remote/` carries the `WorkerSession` protocol across a process boundary without
changing it or the backends. Everything rides one **channel**: an `anyio.abc.ObjectStream[bytes]`,
an ordered, reliable, bidirectional stream of byte messages; in process a stapled pair of memory object
streams, later a socket with length-prefixed framing or a websocket. What crosses are **frames**
(`wire.py`): frozen dataclasses tagged by kind, encoded and decoded by a cattrs converter that is
the wire's own (nothing shared with the entity layer's conversion rules: exact leaf types so a
bool is not an int, no extra or missing fields, ids as their cid, array types as their
wire form, bound ids as their id, frozendicts as maps, the frame set and the reply results as
tagged unions) over `cbrrr`'s DAG-CBOR used as a plain serialization. From the worker: `Hello`
(the protocol version) and then requests carrying a worker-chosen `request_id` (`Register`,
`Drain`, `Observe`, `CompleteWorkflow`, `CompleteStep`, `Fail`,
`Abandon`, `StoreRequest`, `LoadRawData`) and one-way frames never answered (the five
`Emit<Kind>` frames, one per `emit_<kind>` method but `emit_query`, `LogMessage`, and a
pipelined `Store`); from the host, `Reply` (with a `NoResult`, `ValueResult`, or `IdResult`) and `Refused` for the requests, and the
`messages()` stream verbatim (`Assigned`, `AttemptCancelled`, `Shutdown`, the session's own
dataclasses). A `Refused` carries a `Refusal`
naming one of the protocol's exception classes, which the worker re-raises, or wrapping any other
exception as `HostError`; `ProtocolError` is what either end raises when the other broke the
protocol.

**Entity ids on the wire.** A value (stored data, a loaded value, an array) crosses as a
`WireValue`: its canonical DAG-CBOR bytes, references as links, so a claimed cid is verifiable
from the bytes (`cid_of`). The worker may use only the ids its attempt was given or stored, and
each end keeps that set per attempt (`known_ids.py`): `HostKnownIds` on the host side records the
ids given out (a step assignment's inputs, the ids in replies) and checks every id a frame from
the worker carries, holding a pipelined store's cid as *pending* from its store frame until the
host's store returns, which checks await (a second store of a pending cid joins the first);
`WorkerKnownIds` on the worker side records the same sets and answers `is_known` locally.

**The proxy** (`proxy.py`, `RemoteWorkerSession`) is the `WorkerSession` a worker runs on: an
async context manager around the worker's run that sends `Hello`, then drains an unbounded queue
of outbound frames (so the synchronous emits, `log_message`, and pipelined stores queue and
return) and routes inbound frames, replies to the request waiting on their id and the host's
messages onto `messages()`. Per attempt it keeps a `WorkerKnownIds` from `Assigned` until the run
ends; a call for an attempt it has no table for raises `AttemptClosedError` locally, and a value,
array, or load naming an id the attempt does not know raises `UnknownReferenceError` before
anything crosses. Its `store_raw_data` validates the value as an engine would, then either
pipelines (the default: encode, hash the bytes, queue a one-way `Store`, return the id) or sends
a `StoreRequest` and waits (`pipeline_stores=False`). On
disconnect `messages()` ends, waiting requests raise `HostDisconnectedError`, and later one-way
calls and reports are dropped.

**The adapter** (`serve.py`, `serve_session(channel, session)`) drives any `WorkerSession` from
the frames it receives until the channel ends; the caller owns the session's lifecycle. It checks
`Hello`, then pumps `session.messages()` out (giving a step assignment's input ids; closing the
channel if the stream ends without a `Shutdown`, since the worker must see a disconnect) and
reads frames sequentially: checking ids first, in frame order, awaiting any pending store a frame
refers to; then a one-way frame calls the session inline, a `Store` starts the host's store as a
task, and a request starts a task that awaits the session and replies. Per assigned attempt it
opens a *scope*, the attempt's `HostKnownIds` plus a task group its tasks run in, closed once the
run has ended and those tasks have finished. Errors stay with their frame: a request that fails
is `Refused`; a one-way frame the session refuses (or a store whose claimed cid the host does not
reproduce) fails the attempt (`session.fail`, idempotent) and cancels the run
(`AttemptCancelled`); a frame for an attempt with no scope is refused, or cancelled, without a
session call; only an undecodable frame or an unknown protocol version ends the connection.

The reference suite runs every example through the proxy and adapter over an in-process channel
(`tests/reference_backends.py`'s `remote_postgres` entry), and `tests/remote_testing.py` holds
the memory channel pair and the helper that serves a session to a worker over it.

## Tests

- `tests/test_runner_session.py`, `test_runner_contexts.py`, `test_runner_assembly.py`,
  `test_runner_nodes.py`, `test_runner_steps.py`, `test_runner_worker.py`: the protocol's
  vocabulary; the contexts against a mocked session (asserting on the `emit_<kind>` calls and
  tokens); the assembler (each builder's node, the token rules); node identities and the row round
  trip; step requests and cache keys; the function registry and the attempt runners.
  `tests/test_cache_addresses.py`: parsing keys (values holding delimiters, a stray `[`, replica
  groups), matching and selecting with patterns, refused patterns, summaries, and the prefix
  test.
- `tests/test_runner_runs.py`, `test_runner_driver.py`, `test_runner_multiplexed.py`, over
  `tests/runs_testing.py`'s fake attempt (the attempt operations recorded over a `Verifier` and a
  `MemoryStorage`): the runs as sessions (token translation, dispatch by mode, the held return, a
  verification difference failing the attempt, failure and abandonment); the driver's sequencing;
  the host (capacity, the run's end, cancelling an `execute` sent or still waiting, cancelling
  the driver, forwarding and refusals, drain and shutdown, and the whole path through a real
  `Worker` loop with verification on).
- `tests/test_runner_experimental_worker.py`: the worker loop against a mocked multiplexed
  session (registration, running what it is assigned, host cancellation, shutdown, disconnect,
  and the bounded abandon report).
- `tests/test_runner_remote_wire.py`, `test_runner_remote_known_ids.py`: the value codec, every
  frame's round trip and strict decoding, the error envelope; the known-id tables (giving and
  checking, pending and repeated stores, the worker's local knowledge).
- `tests/test_runner_remote.py`: proxy and adapter over a memory channel against a mocked
  session, for what only they can show: both store modes, refusals delivered as the attempt's
  failure, the handshake, disconnects on both sides, the per-attempt scope, a repeated store, and
  an id carried between attempts. Ordinary traffic
  (registration, the message stream, emits, request routing, completions, id checking) is
  covered end to end by the reference suite's `remote_postgres` entry.
- `tests/test_workflow_queries.py`: a workflow's query nodes, run as Postgres jobs: one node per
  collected chain, SQL over handles, a query feeding a step, and determinism verification.
- `tests/test_semantics_reference.py`: the reference examples of a correct execution (scalars,
  mapping, joins, handle operations, child workflows, the cache, replicas, dynamic control flow,
  empty mapped key sets), each run on every backend in `tests/reference_backends.py`'s `BACKENDS`. That
  module defines the backend-neutral contract the tests assert on, `Execution`: the result, a
  storage to load references through, the workflow invocations (workflow, prefix, parent, node
  types), the steps by cache key (step, request count, input refs, result type), attempt counts,
  and the observe count. Each entry is an async context manager yielding an `Executor` (the
  Postgres entries run each job on a throwaway schema, kept open until the test ends, either through
  the local client or a multiplexed worker over the wire, and read `status` and `describe`), so a
  new backend is checked against the whole module by adding one entry. The reference covers successful executions
  only: how failures surface is each backend's own business, tested in its own module.
