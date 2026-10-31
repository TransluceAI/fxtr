# Postgres runner — code overview

The durable runner (`fxtr/runner/postgres/`) keeps all experiment state in a Postgres database and
runs jobs in the process that launches them. It implements the host side of the run-session
protocols described in `../CODE_OVERVIEW.md` (the run sessions, the contexts, the driver, and
nodes are shared with every backend and are not repeated here). It needs the `fxtr[runner]` extra
(SQLAlchemy Core on the asyncpg driver; `uv sync --all-packages --all-extras` installs it) and a
server, for which `postgres_env/` at the repo root runs one locally; the tests run against the
server named by `FXTR_TEST_DATABASE_URL` and are skipped without it.

```
postgres/
├── database.py      Database (engine, pool, schema lifecycle, the in-process wake) and Transaction
├── schema.py        the tables, and the source of truth for what the state means
├── entities.py      PostgresEntityStorage: the write-behind entity store
├── json_values.py   schema-directed JSON encoding of values, keys, and array types
├── logs.py          attempt_logs: per-attempt free-form ctx.log lines (record_logs, list_logs)
├── project_arrays.py  ProjectArrays: the project's named, versioned arrays users edit and a
│                    launch snapshots into a job's inputs; the runner never reads them
├── ledger.py        the facts and the rules that keep them valid: jobs, invocations, nodes
│                    and names, spawns, job attempt facts, step requests, results, and
│                    bindings
├── status_queries.py  canonical payload-free invocation/step status relations, queryable singly or in batches
├── execution.py     the execution state and the policy, and the ownership model: job attempts
│                    and their leases, what is due and starting it, the write guard, settling,
│                    the step cache and its resolution, status, resume, cancel, evict; the
│                    concurrency rules
├── runner.py        JobRunner: the process that runs one job, holding its lease, its queue of
│                    work, and one task per attempt; run_job
├── attempts.py      Attempt / StepAttempt: one live attempt's shared state and lifecycle (ending
│                    in memory, closing in the ledger); the items a body sends
├── recorder.py      Recorder: the task that commits what a body does, one batch per transaction
├── evaluator.py     NodeState / Dag, derive, evaluate (pure); Evaluator: the task that computes
│                    values, expands run nodes, collects results, answers observes, ends attempts
├── jobs.py          Jobs: create, list, status, wait, result, describe, cancel, resume
├── job_records.py   a job's JobRecord built from the ledger (build_record) and exported with
│                    every entity it reaches as a CAR (export_job, reachable_blocks); a record
│                    file read (read_record) and inserted (insert_record)
├── client.py        LocalProjectClient: the ProjectClient over a Database, running the jobs it
│                    submits in this process; LocalJobHandle
└── debug_web/       an isolated, removable read-only GraphQL viewer (its own CODE_OVERVIEW.md)
```

`debug_web/` is a self-contained, removable stopgap: a read-only GraphQL server (the `fxtr[debug-web]`
extra) that a developer-facing viewer reads the runner's state through. Nothing in the runner
imports it; it composes the client's readers and never writes. See its `CODE_OVERVIEW.md`.

## How the system works

**Terminology.**

- A **workflow** is a user-written async function that builds a DAG of array operations and may
  await some of their values along the way. It may run other workflows as **children**, mapped
  over dims of their inputs, and **steps**: user functions that compute an array, expensive and
  individually nondeterministic, whose results are saved under a **cache key** and replayed.
- A **job** is one request to run a workflow on given inputs.
- An **invocation** is the intent to run a workflow at one place in the experiment. A job creates
  one for its root workflow; a run node of a parent's DAG, once its inputs have values, creates one
  child invocation per joint key. The invocation owns the DAG the body describes.
- A **node** is one entry of that DAG, identified by its content, so the same operation on the
  same inputs is the same node however many times it is emitted.
- An **attempt** is one actual call of user code: of a workflow body on behalf of an invocation, or
  of a step for a request at a cache key. If a workflow attempt dies, a later attempt of the same
  invocation runs the body again against what the first one left behind.
- A **job attempt** is one process's episode of running a job, and the owner of every attempt that
  process runs. A job has at most one *active* job attempt, and it holds the job's **lease**: an
  owner string and a heartbeat its process refreshes.
- **The runner** (`JobRunner`, `runner.py`) is that process's side of a job attempt: the one actor
  that decides when each unit of due work starts, runs every body in process, and stops when the
  job attempt settles. There is no separate worker process and no daemon; a launch runs the whole
  job and returns.
- **The body** is the user function while it is running.

**All state is in the database.** Nothing in a process's memory is authoritative: an attempt keeps
an in-memory view of its DAG, but that view is a cache of what has been committed; the runner keeps
a queue of work, but the queue is derivable from the tables: evaluators push the work they create,
and the runner re-derives the whole queue when it has nothing live and on an interval.
Killing every process loses nothing that matters. Owning an attempt is a temporary assignment of
durable work to a process; a fresh attempt replays the body against the same record and continues.

**Three rulebooks say what the state means and how it may change.** They are ordinary functions,
not tasks: nothing happens until an actor calls them.

- The **ledger's transitions** (`ledger.py`) record what is true about the experiment: a job was
  created with its root invocation, a body emitted these nodes and gave them these names, expanded
  a run node into these children or requested these steps, returned this node, and its invocation
  has this result; a job took this result for its request at this key. Facts only, written once;
  nothing here says who ran what or how it is going. Each transition checks the rules that keep
  the facts valid before it writes: a body re-run after a crash must do what the earlier run did
  (the same names for the same nodes, at least the same nodes by the time it returns), or the run
  is rejected as nondeterministic; a result needs a return and is set once.
- The **execution policy** (`execution.py`) owns how the facts get made and decides what runs, and
  its module docstring is the reference for the *ownership model*: which process may write what,
  when, and how the database enforces it. A job's work is directed by its active job attempt.
  What the job attempt is *due* to start is derived (`pending_work`), never stored; its process
  starts it (`start_attempt`) and settles the episode when the work is finished or exhausted
  (`settle`). The write guard (which attempt may write), the step cache (which request owns a key
  and which attempt is current for it, and how a job's request resolves against it), and the one
  question the evaluator asks about something it waits on (done, ready to take, pending, or
  stopped) are the policy's too.
- The **evaluation rules** (the pure half of `evaluator.py`, and `expand` and `collect` in
  `../nodes.py`) say what a node's array type is, from the operation and its inputs' types, and
  what its value is once its inputs have values: a derived node's by computing the operation, a
  run node's by expanding it into child invocations or step requests and stacking their results
  back. A query node is a derived node: its value is its plan run over its inputs' arrays
  (`nodes.compute`). Values are stored for the DAG's leaves (inputs and arrays the body supplied)
  and the targets' results; every other node's value is computed from them by these rules
  whenever someone needs it.

**Actors apply the rules.** Each is a loop or a command with exactly one trigger; each applies
rules and publishes the result; none holds authoritative state.

- **User commands** (`Jobs` in `jobs.py`, and the cache methods of the client), triggered by a
  person: create a job (the ledger facts only: the job and its root invocation), take a job
  (resume, which is also how a process comes to hold a job), cancel a job's active job attempt
  (soft, or hard), clear cache entries, read status.
- **The runner** (`runner.py`), triggered by taking a job (`run_job`: one `execution.resume` under
  its owner string, then `JobRunner.run`): heartbeats its lease, seeds its queue from
  `pending_work`, takes pushes from its attempts' evaluators, starts an *item task* per unit of
  work under a step capacity limiter, and settles the job attempt when the work is finished or
  exhausted. Its module docstring is the reference for the loop, the queue, and the four ways a
  run ends.
- **The attempt task** (one per attempt, inside the runner): starts the attempt in one transaction
  (`execution.start_attempt`), builds the live `Attempt` and its two helper tasks, runs the body
  through the shared driver and the in-process executor, waits for the attempt to end, and finally
  makes sure its outcome is closed in the ledger, whatever cancelled it.
- **The recorder** (one per workflow attempt, `recorder.py`), triggered by the body acting: each
  time the body emits nodes, asks to observe a node, or returns, the
  recorder writes that to the ledger, in the order it happened, and then tells the evaluator what
  is new.
- **The evaluator task** (one per workflow attempt, the other half of `evaluator.py`), triggered by
  values becoming available (the recorder committed something, or work elsewhere finished): applies
  the evaluation rules to the attempt's view, expands run nodes whose inputs have values (creating
  child invocations, or recording step requests and resolving them against the cache; what it
  created for this job to run it pushes onto the runner's queue as the transaction commits),
  collects run nodes whose targets have all finished (asking the policy's `status` about each
  child and `step_status` about each key, and taking a completed step's result by writing this
  job's binding), hands the body the values it asked for, and, once the body has returned and
  every node has a value, stores the returned value as the invocation's result and completes the
  attempt. If a node the body asked for has failed, or nodes could not be computed by the time the
  body returned, the attempt ends, naming those nodes: *failed* if any of them is this invocation's
  own fault, *interrupted* if every one of them is a target that stopped.

The recorder and the evaluator task are siblings: two per-attempt actors, one driven by the body
and one by values, both writing to the ledger. The recorder has no rules of its own; every rule it
enforces is the ledger's. The evaluator's module holds both a rulebook and an actor, which is a
fact about the file, not about the roles.

## Code layering

The section above is about roles. This one is about code: which module may do what with the
database, and in particular where a transaction begins and ends.

*Plumbing* (`database.py`, `schema.py`) knows connections and tables. *Operations* (`ledger.py`,
`execution.py`, `status_queries.py`, the query functions inside `project_arrays.py` and `entities.py`) are functions doing one
thing against the database given a `Transaction`, never opening one; between them they are the only
code that writes SQL. *Services* hold in-memory state spanning transactions (the entity store, a
live attempt). *Use-case logic* (`ProjectArrays`, `Jobs`, the runner, the recorder and evaluator)
decides transaction boundaries and composes the rest. *The client* (`LocalProjectClient`) wires
things together and owns the task group jobs run in. Rules: anything may hold a `Database`; nothing holds a `Transaction` beyond one
operation; transactions stay short and never span slow external work; in-memory state reflecting
a database write changes only in an `on_commit` callback.

## What the state means

`schema.py`'s module docstring and its table and column `comment`s (written into the database too)
define the meaning of every table and column, so that the schema and a database together describe
the system's state without reading code. The tables fall into three groups: the entity store and
the project arrays (`entities`, `project_arrays`, `project_array_rows`, `project_array_snapshots`);
the ledger (`jobs`, `workflow_invocations`, `spawns`, `nodes` with `node_inputs` and
`node_names`, `job_attempt_provenance`, `step_requests`, `step_results`, `job_step_requests`,
`job_step_bindings`); and the execution state
(`job_attempts`, `attempts`, `attempt_events`, `attempt_logs`, `step_cache`). A ledger row refers
only to ledger rows and entities, never to the execution state, so a job's ledger rows, the rows
they refer to, and the entities those reach are a self-contained record of it; the facts and the
execution rows about the same thing share ids (a job attempt's fact and episode, a step result and
the step attempt that produced it), so a database may hold the facts without the execution rows.

The states things can be in, and how the tables encode them:

- An **invocation** is *finished* when `result_cid` is set. Everything else about it is its
  attempts': it has at most one *running* attempt at a time, a database invariant (the partial
  unique index `attempts_one_live_per_invocation`), and that attempt is its only writer.
- An **attempt** is a call of a workflow body or of a step (`kind`); the two share one table
  because a job attempt's process runs both and its attempts are swept together when it is
  evicted. An attempt is `running` from the moment its row exists (it is inserted running, by its
  owner's process; nothing is ever queued in the database) and ends in exactly one of `completed`,
  `failed` (with an error: its own body or DAG is at fault), `interrupted` (workflows only, with a
  reason: something it depended on stopped; nothing is wrong with it, and resuming once that is
  fixed lets it complete), or `abandoned` (with a reason: its process stopped it for reasons
  unrelated to it, or its job attempt was evicted). `stopped_on` names the nodes a failure or
  interruption stopped it on. `owner_job_attempt_id` is the job attempt whose process runs it.
- A **job attempt** has two independent axes. Its *state* says whether the episode is still
  deciding: `active` (at most one per job), then settled as `succeeded` (the root's result was
  materialized), `stopped` (nothing of the job runs and nothing is due), or `cancelled` (a person
  stopped it). Its *lease* says whether its process is trusted: `owner` names the process,
  `heartbeat_at` is refreshed while it runs, and the job attempt is *fresh* while the heartbeat is
  recent, *stale* when it is not, and *evicted* (`evicted_at`) once someone has taken its work
  away. `execution.py`'s docstring lays out what each combination of the two axes means and who
  may act on it. Both axes are execution state; that the job attempt existed, which code its
  process ran (a `CodeVersion`), and when it was created are a ledger fact,
  `job_attempt_provenance`, with the same id. A database has `job_attempts` rows only for the job
  attempts it ran, but may hold facts for others (and for jobs it does not have), so a job
  attempt's position among its job's (`seq`) is counted over the facts.
- A **job**'s inputs are fixed when it is created: each is an `Input` node of the root invocation
  referencing an array entity. A project array reaches a job only this way, as a snapshot the
  launch passed (`Jobs.create` takes the snapshot's bound id), so a job observes one version of it
  by construction.
- **Nodes** are stored relationally (kind, params, type, and edges in `node_inputs`), the union
  over all of the invocation's attempts. Leaves (inputs and source arrays) have a stored
  `value_cid`; derived values are never stored anywhere, and neither are derived errors: both are
  recomputed from the stored ones by whoever needs them. An attempt that a node's failure stopped
  names the node in its `stopped_on` list.
- **Spawns** record what each run node expanded into: one row per joint key, pointing at the
  target requested for it, a child workflow invocation or a step at a cache key. It is the one
  record of who requested what; an invocation's parent is the spawns row pointing at it, and the
  root has none. Any row for a node means the node is expanded, so a later attempt of the parent
  does not expand it again. A run node's value is derived from its targets' results and never
  stored.
- **Steps**: a *step request* is an entity (its cid is the fingerprint) with a relational face in
  `step_requests` holding the fields a query needs, the way nodes are rows beside their identity.
  What a job asked for at a key is a fact (`job_step_requests`, one request per key per job). A
  completed step attempt leaves a *step result* (`step_results`, with the attempt's id): the fact
  that the request, run under the attempt's owner job attempt, produced this array, so every
  result names the code that produced it. Which step result a job *took* is a fact set once, by
  the job itself (`job_step_bindings`). The *cache entry* (`step_cache`) is the mutable global
  address at a cache key, saying which request owns the key and either which attempt is current
  for it or which step result it offers; completing the attempt moves the entry to its step
  result. The rules that relate them are in the next section.
- **Results name their producers**: an invocation's result is set with the job attempt whose
  process produced it (`workflow_invocations.job_attempt_id`), a step result names its producing
  job attempt, and a job attempt's fact names its `CodeVersion`. Which code produced any result is
  read from the ledger alone, also for a result another job produced.
- **Events** (`attempt_events`) record what each attempt's body did, in order: the nodes it emitted,
  the nodes it asked to observe, and the node it returned. Requests, not results.
- **Logs** (`attempt_logs`) record the free-form `ctx.log` lines each attempt's body wrote, in
  order. Pure observability state: no rule reads it, and it may be dropped without affecting
  correctness. Kept per attempt and never deduped across a replay (a later attempt of an invocation
  re-runs the body and writes its own lines). A stopgap for an eventual event-streaming mechanism.
- **Job and invocation status is derived, never stored**: per invocation, *completed*, *running*,
  *queued* (the active job attempt is due to run it), or, idle, *failed* or *interrupted* by its
  latest attempt, else *stopped* (incomplete and idle, with no attempt under the current episode
  to explain why); the job is *succeeded*, *running*, or *stopped* (a job nothing has taken
  included). `jobs.py`'s module docstring gives the exact rules. The relations of
  `status_queries.py` are the one derivation of each step's judgement and of each target's displayed
  state, shared by the evaluator (`execution.step_statuses`) and the viewer; their cache resolution
  part restates `execution.resolve_step`, which the write paths act on, and the tests check that the
  two agree (see `status_queries.py`'s docstring). What the evaluator makes of a child invocation is
  `execution.status`.
- **Determinism.** Later attempts of an invocation replay the same body against the same ledger. An
  emitted identity already present is a match; a name mapped to a different identity is a
  violation; at return, every non-input node of the invocation must have been emitted by the
  returning attempt, so a body that emits less than before is a violation while one that emits more
  (it got further) is fine. A violation fails the attempt with `DeterminismError`. The runner's
  determinism verification (`../CODE_OVERVIEW.md`) checks the same thing proactively, by running a
  verification run of the body within the same attempt before the return is recorded; it involves
  no database state.

## The rules the ledger keeps

These are the invariants the ledger's transitions maintain, the ones that make the record valid
and consistent. Each names where it is enforced. Most of them were subtle enough to need working
out; a change that touches one should re-read the docstring it points to.

**One writer per invocation.** At most one attempt of an invocation is running at a time (the
partial unique index `attempts_one_live_per_invocation`; starting inserts against it with
`ON CONFLICT DO NOTHING`), and only a running attempt may write (`execution.guard`, which every
attempt transaction starts with and whose `ActiveAttempt` the ledger's writes take). A write by an
attempt that has since been closed, by itself or by an eviction, touches nothing. The row locks
that make this hold under concurrency are in "Concurrency and locking" below.

**Replay determinism.** A later attempt of an invocation runs the same body against what earlier
attempts recorded. An emitted node already present is a match; a name mapped to a different node
is a violation; at return, every non-input node of the invocation must have been emitted by the
returning attempt, so emitting less than before is a violation and emitting more (getting further)
is fine (`ledger.record_emissions`, `ledger.record_return`). A violation fails the attempt with
`DeterminismError`. Determinism verification checks the same thing proactively, within one
attempt, by having the body run again as a verification run before the return is recorded.

**One view per job.** A job observes a consistent snapshot of every mutable thing it depends on.
Its inputs are array entities fixed at creation (a project array enters as the snapshot the launch
passed), and its bodies read no mutable state. A job's request at a cache key is recorded once
(`ledger.record_step_request`) and, when the job takes the step result the cache entry offers,
bound to it once (`ledger.bind_step_result`, `job_step_bindings`); the binding never moves,
whatever later happens to the cache entry, and a job is done with a step only once its binding
exists. Two requests in one job at one key must carry the same request, else the body used one key
for two configurations, a failure of the requesting attempt (`ledger.CacheAddressReuseError`).

**Step resolution.** A job coordinates with other jobs only through the cache entry, and writes
only its own bindings and the entry. `execution.resolve_step` is the one rule, for a job `J`
requesting `R` at key `K`:

1. `J`'s request at `K` is bound: `Bound`. Done.
2. No entry: `RunIt`; starting creates the entry with `R`.
3. An entry with a *different* request that offers a step result or whose current attempt is
   running: a *cache conflict*, `ConflictingEntry`, an interruption of the requesting run node
   until a person clears the key and resumes. If its attempt failed or was abandoned, nothing live
   owns the key: `RunIt` takes the entry over. Other jobs' bindings are untouched. If `J`'s active
   job attempt overwrites cache conflicts (`job_attempts.overwrite_cache_conflicts`, set by the
   launch or resume that created it, and not inherited by later ones), an entry that offers a
   different request's step result is not a conflict: `RunIt.overwrites` names that step result,
   and `start_attempt` takes the entry only while it still offers it, as a clear and then a run
   would. A running attempt of a different request is still a conflict.
4. An entry with an *equal* request: its attempt running: `Pending`, another job is computing it;
   a step result on offer: `Completed`, and `J` *takes* it by writing its own binding
   (`execution.take_step_result`, which its evaluator calls), after which it is `Bound`; its
   attempt failed or abandoned: `RunIt`, a new attempt becomes the entry's current.

A `running` attempt is live whatever its owner's heartbeat says: nothing in this rule reads lease
freshness (see "Leases and stopping"). Completing a step binds nobody; each job takes the result
itself, which is what makes the binding a fact the job wrote and closes the window in which a
request recorded concurrently with the completion could be seen by neither side. The evaluator
never touches the cache itself; it asks `execution.step_status`. Clearing a key
(`execution.clear_cache_entry`) deletes the entry and nothing else. A run node stopped on cache
conflicts gives one clause for all of them, the key when there is one and the count otherwise
(`execution.conflict_reason`), so the stop reason stays short however many spawns conflict.

**Stored versus derived.** Stored values belong to leaves — inputs, source arrays, and the
results of steps and children. Every derived node's value, a query node's included, and every
derived node's error, is recomputed from those by whoever needs it and never written
(`nodes.value_cid`; no error column). What an attempt records about a failure is which nodes it stopped on
(`attempts.stopped_on`), each the node where the error originated, never a node it propagated
to; everything else about the failure is derived, by recomputing a derived node or by reading a
child's own attempts.

**Completion needs every node.** An attempt completes only when its body has returned *and*
every node of the invocation has settled (`Evaluator._finish`). An unfinished child anywhere
keeps the attempt open; a failed node anywhere stops it at return, observed or not.

**Failure versus interruption.** An attempt *fails* when the fault is its own invocation's: the
body raised, a derived node it emitted raised, it was nondeterministic, it used one key for two
configurations. It is *interrupted* when the fault is another invocation's: a child it depended
on failed, or a key it needs is in cache conflict with another job's request. Nothing is wrong
with an interrupted invocation; resuming it once the cause is fixed lets it complete unchanged.
The two are decided where the attempt ends (`Evaluator._stop_on`), from where each error
originated. Step attempts fail or are abandoned; they are never interrupted.

**What is due.** A job attempt *tries each of the job's invocations once, plus replacements for
abandonment*: an invocation is due while it is unfinished, nothing is running it, and every attempt
of it the job attempt owns is abandoned (`execution.pending_work`). A step request of the job is
due while it is unbound and `resolve_step` says `RunIt`, unless the job attempt already ran a
failed attempt for that key. A child completing never re-runs its parent (the parent is waiting on
it). A job attempt's own failure at a key is its answer for that key until a resume, whatever other
jobs do there afterwards, so what the evaluator is told does not depend on timing
(`execution.step_status`). A resume (`execution.resume`) starts the next job attempt when the
latest is settled or stale, and is refused while a live one exists, whoever holds it. Nothing is
scheduled by it: what is then due follows from the same predicate, so a failed or interrupted
invocation is due again under the new job attempt, which has not tried it. Each rule is one branch, so retries, approval flows,
or holding abandoned work are local changes there.

## Ownership: who may do what

`execution.py`'s module docstring is the reference; the shape of it:

- Every attempt is owned by exactly one job attempt, the one whose process runs it, and only a
  *running* attempt writes on its own behalf (the guard). Eviction abandons every running attempt
  the job attempt owns, and nothing can be started under it afterwards, so an abandoned row is the
  whole of a revoked right to write.
- Only the *active* job attempt decides what is due for the job and whether an unfinished target is
  pending or stopped; only its owner starts attempts.
- Anyone may settle a job attempt `cancelled`. The owner settles it `succeeded` or `stopped` as its
  work finishes or runs out, and so does a resume that finds the job finished or exhausted (the rule
  is a fact about the job, so a job attempt whose process died between its last attempt's close and
  its own settle is settled by whoever next resumes).
- Only a *user action* evicts: a takeover (a resume of a job whose active job attempt is stale), a
  hard cancel, or an explicit eviction.
- **Staleness informs; only eviction acts.** No decision in the policy reads lease freshness. A
  running attempt is live until its own process closes it or a user action abandons it, so state
  moves one way: nothing ever starts work on the assumption that a process is dead and then finds
  it alive. Freshness is shown by the viewer and gates a takeover in `resume`; that is all.

## The life of an attempt

This is the reading order for the code that executes a workflow. Each step names the function
that does it.

1. **A job is started and taken.** `Jobs.create` checks the inputs against the workflow's
   signature, stores them, and calls `ledger.create_job` (the job row, the root invocation, one
   `input` node per argument, all with values). Nothing runs until a process takes the job:
   `run_job` calls `execution.resume` under this process's owner string, which settles a job
   attempt whose process died before settling, then creates the first job attempt (active, with
   a fresh lease), refuses a fresh one
   (`JobRunningElsewhereError`), or takes a stale one over (evicting it).
   `JobRunner.run` then opens one task group with the heartbeat task and runs the scheduling loop
   in its own task. The loop's first pass is the seed: `execution.settle`, which takes the job
   attempt's row and returns what is due, here the root.
2. **The runner starts it.** For each due item the loop spawns an item task (`_run_item`), which
   takes the step capacity limiter for a `StartStep`, then runs `execution.start_attempt` in one
   transaction: take the job attempt's row (so the start serializes with an evict, cancel, or
   settle), insert the attempt `running` against the one-live index for an invocation, or take
   the cache entry for a step (create it, or replace exactly the current attempt the resolution
   saw), and load what running it needs (`Started`: the invocation, its inputs as identities and
   types, its recorded nodes and spawns; `StartedStep`: the attempt and step name). A step's
   start makes its reads (the request, the resolution, the step's name) before taking the row,
   so the row is held only across its two writes; `start_attempt`'s docstring says why that is
   sound. `None` means
   nothing to start after all. The attempt task (`_run_workflow_attempt`) builds the `Attempt`
   before its first `await`, so its `finally` covers the running row from here, seeds the attempt's
   view of the DAG from the recorded nodes (a node with a recorded value is loaded with it;
   everything derived is recomputed), starts its `Recorder` and `Evaluator` tasks in one task group, and runs the body:
   the shared driver (`../driver.py`'s `drive_workflow_attempt`) through the in-process executor,
   inside a cancel scope that is the attempt's `cancel_body`. The driver builds the primary run
   (`WorkflowRun`: a `NodeAssembler` bound to the input nodes, the `WorkflowAssignment` built from
   them) and calls `run_assignment` on it in the same task.
3. **The body runs.** The run translates the body's node tokens through its assembler (an
   `emit_<kind>` call becomes a `Node` with its identity computed there; `observe` and
   `complete_workflow` resolve their token) and dispatches, by its mode, to one of the attempt's identity-based
   operations (`attempts.py`, which satisfies `../assembly.py`'s `WorkflowAttemptOps`). On the
   primary run each becomes an item on the attempt's stream: `emit` pushes an `Emit` and returns,
   and `emit_source_array` does the same after putting the array into the attempt's write-behind
   entity store (so the recorder finds it when it records the node and flushes it in the same
   transaction); `observe` pushes an `Observe` and awaits it.
   `complete_workflow` records the return (`record_return`, a `Return` item) unless determinism is
   being verified, in which case it holds it (`hold_return`: the verifier keeps it, the recorder is flushed)
   and returns; the driver sees the held return and executes a verification run of the attempt
   under a fresh id, whose calls go to the attempt's `verify_*` operations, which check against
   the verifier and record nothing, and whose `complete_workflow` records the held return once it
   agrees. The body's task never holds a transaction.
4. **The recorder commits what the body did.** `Recorder.record` takes each batch and, in one
   transaction starting with `execution.guard`, flushes the attempt's entity store, then records the
   items in body order: `ledger.record_emissions` (nodes, edges, names, `emit` events, with the
   name check), `ledger.record_observation` (the request), `ledger.record_return` (with the checks
   at return), and `logs.record_logs` for any `ctx.log` lines the batch carried. After the commit
   it publishes new node states, pending observes, and the returned node to the `Attempt` and
   wakes the evaluator. If anything in the batch fails (a determinism violation, a malformed
   emission), the transaction rolls back, nothing of the batch is recorded, and the attempt is
   failed with the error.
5. **The evaluator computes, expands, collects, and answers.** On each wake (the recorder, the
   job's wake topic, the topic of any cache key it waits on, or the poll interval) `Evaluator.step`
   runs the pure `evaluate` over the view, computing every derived node whose inputs have values
   and propagating input errors, each error remembering the node it originated at. Then it
   *expands* every run node whose inputs have values and is not expanded yet (`_expand_ready`):
   `nodes.expand` gives the joint keys and sliced inputs, the inputs are stored, and one
   transaction under the guard calls `ledger.expand_run_node` (the children with their valued input
   nodes, the spawns rows), registering an `on_commit` push of each created child onto the runner's
   queue (the `WorkSink` the evaluator was given); the spawns are kept on the attempt. A `run_step`
   node goes the same way with requests in place of children (`_expand_step`): each spawn's sliced
   inputs and its `StepRequest` are stored, the job's requests at their cache keys are recorded
   (`ledger.record_step_requests`) and resolved (`execution.resolve_steps`), each a few statements
   for all of the node's keys however many it has, a key nothing live owns is pushed as this job's
   to run, and the spawns are recorded, in one transaction. A sliced input
   that names an entity storage does not have (a query can write any entity ID) makes storing it
   raise `UnknownReferenceError`, which is the run node's own error (`_fail_node`), and nothing is
   recorded for the node. Then it
   *collects* (`_collect`): in one snapshot it asks `execution.status` about every expanded run
   node's children and `execution.step_statuses` about every key (one query for all of them),
   skipping targets an earlier pass found done (done is permanent, so the evaluator keeps their
   results and a pass costs what is outstanding, not the whole node); a
   step whose result an equal attempt has completed is *taken*, in a short write transaction after
   the snapshot
   (`execution.take_step_results`: this job's bindings, for all the pass's keys at once), and only
   then counts as done; a target found
   due with nothing running it is pushed onto the runner's queue (its trigger was another job's
   attempt ending without a result, which no commit here saw). Once all of a node's targets are
   done its value is `nodes.collect` of their results, and if any is stopped the node gets an error
   marked as an interruption. Then it answers pending observes whose node has settled (the value,
   or `NodeFailedError` and an ending naming the node's origin), and, once the body has returned
   and every node has settled, stores the returned value (unless storage already holds it,
   `NodeState.stored`) and calls `execution.materialize_result` (`Evaluator._finish`), which
   records the result and closes the attempt as completed in one transaction; the attempt then
   ends `Completed`. A returned value that names an entity storage does not have fails the
   attempt instead. Nodes that could not be computed by then stop
   the attempt at return (`_stop_on`): *interrupted*, with a reason naming the stopped targets, if
   every origin is an interruption; *failed* otherwise, with the origins in `attempts.stopped_on`.
   No node's error is ever written to its row, since it is recomputable like its value. A step
   attempt itself has no recorder or evaluator: its task loads the inputs from the request entity,
   runs the step through `StepRun`, and `StepAttempt.complete` writes the result's entities, then
   records the result and closes the attempt as completed (`complete_step`) in a transaction that
   writes only ledger rows; each waiting job takes the result itself. A step's `ctx.log` lines are buffered on
   the `StepAttempt` and written by a background flush task the attempt task runs for the
   attempt's life (the sole writer of its logs; completing or closing stops it and awaits a final
   flush first, so a final line is persisted while the attempt is still live).
6. **The attempt ends, then it is closed.** Two operations with two owners, defined in
   `attempts.py`'s docstring. An attempt *ends* once, in memory, when its outcome (`Completed`,
   `Failed`, `Interrupted`, `Abandoned`) is set by `Attempt.end` (`fail` and `abandon` are its
   synchronous faces, which is what lets a synchronous emit fail the attempt on a determinism
   violation): the first outcome wins, everything further is refused with `AttemptClosedError`,
   the stream ends so the recorder stops, the evaluator is woken so it stops, and the body is
   cancelled when it did not cause the ending. The deciders are exactly the parties inside the
   attempt's task: the evaluator, the recorder, the runs, and the task itself for `abandoned`. The
   attempt is *closed* when the ledger has its terminal state (`Attempt.close`: an idempotent write
   of the outcome under the guard, safe from several tasks, adopting a close someone else already
   wrote). Whoever ended the attempt closes it at once when it can (the evaluator's completing
   transaction is itself the close); the attempt task's `finally` is the closer of last resort,
   ending the attempt `abandoned` if nothing did and closing it if the ledger does not have it,
   under a shield with a deadline, so an attempt's row never stays `running` after its task ends.
7. **The runner settles.** Closing an attempt does not settle the job attempt. When the runner's
   last live item task ends, and on the reconciliation interval, the loop calls
   `execution.settle` in a transaction that takes the job attempt's row first: `succeeded` when
   the root has its result, `stopped` when nothing of the job is running and nothing is due, else
   it returns what is due for the loop to enqueue (`pending_work`, two queries over the status
   relations whatever the job's size). Evaluator pushes, not this pass, drive ordinary progress
   from one step to the next; the pass recovers what a push missed. The runner
   exits once the job attempt is settled and nothing is live. Settling wakes the job's topic, which
   is what `Jobs.wait` and other attempts' evaluators wait on.

`attempts.py`'s module docstring is the attempt's state machine (running, returned, ended; two
words for two places: *ended* is a fact about the object, *closed* about the ledger row) and the
rule that keeps the two tasks consistent: the recorder publishes to the shared state only after
its commit and without yielding, and the evaluator acts only on published state, so the ledger is
always at least as far along as the view.

## Leases and stopping

A job attempt's lease is its `owner` and `heartbeat_at`. The runner's heartbeat task refreshes it
on an interval (`execution.heartbeat`, a conditional update on the id, the owner, and the job
attempt not being evicted, returning its state); a failed refresh is logged and retried, never
fatal, since a missed heartbeat costs nothing by itself. The heartbeat is also the runner's channel
for two of the four ways a run ends (`runner.py`'s docstring):

- **The work finished.** The loop settled the job attempt `succeeded` or `stopped`; nothing is
  live; `run` returns it.
- **Interrupted here.** `JobRunner.interrupt()`, called once per Ctrl-C by a handler on the event
  loop's thread. The first call settles the job attempt `cancelled` and *drains*: nothing more
  starts, the heartbeat continues, live item tasks finish (a step in flight completes and its result
  is on the cache entry for the job to take when it is next run). The second cancels the item
  tasks, whose attempts are abandoned. Cancelling the runner's task from outside is the abrupt form
  of the second, and `run`'s shielded `finally` still settles the job attempt `cancelled`, so a
  later resume needs no takeover.
- **Cancelled elsewhere.** The heartbeat reports a settled state: another process soft-cancelled the
  job. The runner drains as for the first interrupt.
- **Evicted.** The heartbeat's conditional update finds no row: a takeover by a resume elsewhere, or
  a hard cancel, abandoned everything this runner owned and settled the job attempt. The runner
  cancels its tasks (each attempt's close finds its row already closed and adopts that), settles
  nothing, and raises `JobEvictedError`.

A resume of a job whose active job attempt is *stale* (no heartbeat for `take_over_after`; the
policy has no default of its own, since every process of a project must judge staleness by the same
threshold, which the project's configuration sets: `fxtr.project.config`'s `stale_after`. A resume
given no threshold never takes over) takes it over: `execution.evict` marks it evicted with an update
conditional on the heartbeat still being stale (so a heartbeat that lands first keeps the lease and
the takeover is refused), abandons every running attempt it owns, settles it `stopped`, and the
resume creates the next job attempt. The threshold is a protocol parameter every process of a
project shares (one process's heartbeat interval is judged by another's threshold), not a per-runner
choice. A job whose owner process died mid-step is waited on by other jobs until someone resumes or
evicts it; their status shows the stale lease.

## Concurrency and locking

Transactions run at Postgres's default read-committed level, take no advisory locks and no
`FOR UPDATE`, and lock a row by a conditional `UPDATE` on it, which blocks behind an in-flight
writer and re-evaluates its predicate once that writer commits (a no-op `SET state = state` locks
like any other). `execution.py`'s module docstring is the reference for the two rules that make
every writer sound: every writer of a job's execution state takes the job attempt's row first
(starting, settling, cancelling, evicting, heartbeating; a resume takes the job's own row before
that), an attempt's own transactions take only their attempt row except the
one that *ends* the attempt (`guard(..., ending=True)`: completing or closing takes the owner's
job attempt row first, whatever its state, so that a settle sees an attempt either running or
ended with its result in place, never in between), and rows are always taken in the order job,
job attempt, attempts, cache entry; every write at a cache key is one atomic conditional
statement, and a binding is written only by its own job. Its docstring also gives the one-line
argument for each writer. Every step of a job takes the job attempt's row twice, starting and
ending, so those two transactions keep it short: they make their reads before taking it and
read nothing back after, and the result's entities are written beforehand in a transaction of
their own. The row is what bounds a job's throughput of short steps. Readers that compose several queries into one view open a
`snapshot=True` transaction and never write.
`tests/test_runner_postgres_execution.py` and `tests/test_runner_postgres_ledger.py` stage the
races with two transactions held open at once.

## Waking and polling

`Database.wake(topic)` and `Database.wait_for(topic, timeout)` are named in-process events that a
committing transaction sets from an `on_commit` callback, so waiters in the same process re-check
the database at once instead of waiting out their poll interval. It is only an optimization for
co-located tasks: every waiter also polls, since another process's commits do not wake it. Topics:
`job:<id>` (something of the job changed state; `Jobs.wait` and the job's evaluators wait on it)
and `step:<cache_key>` (the key's current attempt ended, or the key was cleared; an evaluator
waiting on the key wakes). An evaluator waits on its job's topic and every key it is still waiting
on with one event (`Database.wait_for_any`), so arming a wait costs a set insertion per key, not a
task. The runner's own loop is woken directly by its item tasks and by
`enqueue`, and re-derives what is due on an interval besides. Operations register wakes through
`Transaction.wake(topic)`.

## Cancellation

`Jobs.cancel(job, hard=, reason=)` (`execution.cancel`) settles the job's active job attempt
`cancelled`: it stops deciding. Nothing unfinished of the job is pending any more (`status` and
`step_status` answer stopped, so a workflow attempt is interrupted at its next observe or return of
anything unfinished), nothing more is started, and the owning runner learns on its next heartbeat
and drains. Under a *soft* cancel running attempts continue and write: a step's result lands on the
cache entry, where the job takes it when next run; a workflow attempt's facts are recorded. A
*hard* cancel also evicts the job attempt: every running attempt it owns is abandoned and the
runner stops with `JobEvictedError`. A step another job waits on is not spared: under the race rule
the other job's next look sees the abandoned attempt and runs the step itself. The ledger is
untouched by either flavour, and `Jobs.resume` (or `run_job`) starts the next job attempt as
after any stop.

## The pieces in more detail

**`database.py`.** `Database` owns the async engine and pool for one database; `transaction()`
yields a `Transaction` (`execute`, `on_commit(callback)`, `wake(topic)`, `upsert(table)` for the
one dialect-specific construct; the underlying connection is exposed for metadata operations)
committed on normal exit, then its callbacks run, and rolled back on an exception; the commit and
the rollback are shielded from cancellation, so a cancelled task still returns its connection to
the pool. `create_schema` / `drop_schema` create or drop the tables registered on `schema.py`'s
`metadata`. A `Database` may name a Postgres schema (`pg_schema`, a namespace within the server's
database) that becomes every connection's `search_path`, so several independent runner states can
share one server; the `database` test fixture uses this to give each test a fresh, throwaway
namespace.

**`entities.py`.** `PostgresEntityStorage(database)` is the entity store, an
`FxtrEntityStorage` over the `entities` table (cid, `type_tag` — the type id string — for inspection
from SQL, the DRISL bytes, `created_at`; insert-only, identical content a no-op). The contract's
`store_raw_data` never touches the database (the interface is async, but nothing here awaits) and
`is_known` is synchronous, so the store is *write-behind*: stores encode
(via `encode_for_store`) into a pending map in memory, and `flush(tx)` writes everything pending into
the given transaction (in sorted cid order, so overlapping flushes cannot deadlock), registering an
`on_commit` callback that marks the snapshot confirmed and drops it from pending; `flush()` with no
argument opens its own transaction. "Known" means pending or *confirmed*, the ids this instance
has seen in the database: flushed ids, loaded ids, and the direct references of every loaded value
(in the database by the storage invariant). An entity another process wrote is unknown to this
instance until it is loaded or checked with `confirm_known(ids, tx=None)`, which queries the
database (in the given transaction, so entities flushed into it count) and returns the known
subset. Loads serve pending entities from memory and the rest from the database;
`load_raw_data` decodes the value (`load_raw_data_many` many, in one query per batch of ids), while
`load_raw_bytes(id)` returns the stored DRISL bytes and `type_tag` verbatim (or `None`), for a
caller that wants the encoded form to pass through untouched.
`assume_known(ids)`
marks ids confirmed without a query, for ids read from committed database state the system
guarantees references only existing entities. An instance is cheap, and the arrangement is *one per
writing context* with one natural commit point: a notebook client session, or one per attempt in
the runner, flushed in every transaction the attempt opens (so a node value can never reference an
entity the database lacks) and dropped when the attempt's task ends. Instances may freely store the
same content; the database deduplicates.

**`project_arrays.py`.** `ProjectArrays(database, entities)` holds the *project arrays*: the
project's named arrays, declared once with an `ArrayType` (`create_array`, idempotent for the same
type, `ProjectArrayExistsError` otherwise) and edited over time by the user, never by the runner: `upsert` merges an `Array` by key, `replace` makes the rows equal to one, `delete_rows`
/ `clear` remove rows, `drop_array` removes everything, `load` returns the current rows as an
`Array` (a scalar array with no row raises `ProjectArrayEmptyError`), `describe` / `list_arrays`
return `ProjectArrayInfo` (type, `version`, row count, timestamps). The declared type is the source
of truth: an upserted array's dims are matched by name and its schema must be assignable, and rows
are stored and loaded under the declared type. Every mutation first increments the array's
`version` in `project_arrays` (which also locks the registry row, so mutations of one array are
serialized), and every write flushes the entity store into the same transaction and then checks, in
that transaction, that every entity id in the values exists in the database, so persisting an array
persists what it references, atomically. Rows live in `project_array_rows` as jsonb: `key` a JSON
array in declared dim order, `value` encoded by the declared schema. `snapshot(name)` stores the
current rows as an `Array` entity and records it against the current version in
`project_array_snapshots` (returning a `SnapshotInfo` with the `BoundID`), or returns the
snapshot already recorded for that version; it reads under a shared lock on the registry row so
version and rows agree. Identical content at different versions is one entity under two records;
`list_snapshots` lists them, and dropping an array drops the records but not the entities. A
snapshot's bound id is what a launch passes to `Jobs.create` as a root input, so the array reaches
the job as an ordinary `Input` node; which array and version an input came from is the join of the
input's array cid to `project_array_snapshots.cid`, derived rather than recorded.

**`json_values.py`.** The codec for values, keys, and array types in jsonb columns,
*schema-directed* rather than self-describing: the schema is the source of truth and sits outside
the value, so an `id` position holds a cid string, a `bytes` position base64, a `float` position
any JSON number, and nothing is reserved (a struct field may be called anything). `value_to_json`
/ `value_from_json`, `key_to_json` / `key_from_json`, and `array_type_to_json` /
`array_type_from_json` (the declared type, stored the same way using the fixed grammar of its wire
form) all raise `JsonValueError` on data that does not fit.

**`logs.py`.** The `attempt_logs` table's write and read: `record_logs(tx, attempt_id, messages)`
appends free-form lines numbered after the attempt's last, `list_logs(tx, attempt_id)` reads them
in order (`LogRecord`), and `list_logs_many` reads many attempts' in one query. Pure observability state; no rule reads it. Deliberately small and
self-contained so it is easy to remove when an event-streaming mechanism replaces it. Workflow
attempts' lines are written from the recorder's batch transaction; step attempts' from the
`StepAttempt` flush task.

**`ledger.py`.** The facts of the durable record and the rules that keep them valid, read in the
order its module docstring gives: jobs and invocations (`create_job`, `create_invocation`), what a
running attempt records (`record_emissions`, `record_observation`, `record_return`,
`record_result`, `expand_run_node`, `record_step_request`, `record_step_spawns`,
`bind_step_result`, and `record_step_requests` / `bind_step_results` for many keys), what a completing step attempt records (`record_step_result`, called by
`execution.complete_step`), then the readers (`get_job`, `list_jobs`, `get_invocation`,
`get_invocations`, `find_invocation`, `list_invocations`, `load_spawns`, `find_parent`,
`get_step_request`, `get_job_step_request`, `list_job_step_requests`, `get_step_binding`,
`get_step_result`, `get_job_attempt_provenance`, `load_nodes`, `list_events`, and for many keys
or ids in a few statements, `get_step_requests`, `get_job_step_requests`, `get_step_bindings`,
`get_step_results`, `get_job_attempt_provenances`), which decode rows
into the record types (`JobRecord`, `InvocationRecord`, `SpawnRecord`, `StepRequestRecord`,
`JobStepRequestRecord`, `StepResultRecord`, `StepBindingRecord`, `JobAttemptProvenanceRecord`,
`NodeRecord`, `EventRecord`). Every recording function
takes an `ActiveAttempt`, the write token only `execution.guard` issues; the ledger does not
re-check who is writing. Nothing here says how work is going or decides what runs. The one piece
of execution state the ledger touches is the acting attempt's own event log (`attempt_events`):
the recording functions append its events alongside the facts they record, and `record_return`
reads its `emit` events for the determinism check, a rule about what this attempt did.

**`status_queries.py`.** `StatusQueries` scopes its relations to a set of jobs, resolving latest
and active attempts with indexed SQL lookups without hydrating attempt histories; each
relation is built on first use. The step relation answers the execution policy's question about a
job's request at a key (`target_state`) and names it for display (`display_state`); the invocation
relation names each invocation's state for display. `execution.pending_work` lists the `queued`
rows of both relations as what the active job attempt is due to start, so the runner's queue is
what the status shows as queued. The module docstring separates those from the cache resolution
question `execution.resolve_step` answers for the write paths, and gives the correspondence
between the two statements of the cache rules, which `assert_policy_agrees` in
`tests/postgres_testing.py` checks from every staging helper. `invocation_records` reads selected invocation states with their
answering attempts' errors and reasons for `Jobs`, and `named_step_records` reads every step row
of one job with its step's name, which is what `Jobs.status` shows for each step; both keep the
SQL in this operations module. `step_records` reads the step rows at many keys in one query, and
`conflict_records` the rows in `conflict`;
`execution.step_statuses` turns them into `TargetStatus`es with no further reads, and `Jobs`
hydrates requested details from them. Detailed readers and the viewer aggregate or page the same
relations. It reads no entity payloads or logs. The step relation reaches its jobs only through a
`MATERIALIZED` `status_jobs`, never a literal job ID, so a job missing from the planner's
statistics still gets a lookup by key per row instead of a join filter over all of the job's
rows; the module docstring ("Planning without the job IDs") explains, and `debug_web/versions.py`
follows the same rule for the version tokens' count of a job's bindings.

**`execution.py`.** The execution state, the policy, and the ownership model, read in the order
its module docstring gives: job attempts and the lease (`create_job_attempt`,
`active_job_attempt`, `lock_job_attempt`, `heartbeat`, `evict`, `cancel`); what is due
(`pending_work`, `start_attempt` returning `Started` / `StartedStep` / `None`); attempts
(`guard`, `guard_step`, `materialize_result`, `close_attempt`, and `settle`, which returns a
`Settlement`: the job attempt it settled or what is due); steps (`resolve_step` → `Bound` /
`Completed` / `RunIt` / `Pending` / `ConflictingEntry`, `take_step_result`, and `resolve_steps` /
`take_step_results`, the same rules for many keys, reading each kind of row once for all of them
and the one implementation the single-key forms call; `complete_step`,
`close_step_attempt`, `clear_cache_entry`, which can require the entry to offer a given step
result, `adopt_step_result`, and `cache_conflicts`, a job's conflicts from the step relation); `status` and `step_statuses` (with `step_status`, one
key's), the evaluator's one view of a child or a step (`Done`, `Ready`, `Pending`, `Stopped`), and
`invocation_state` for display; `resume` (returning the job attempt taken, or `AlreadyFinished`);
then the readers (`get_job_attempt`, `list_job_attempts`, `get_cache_entry`,
`list_cache_entries`, `select_cache_entries`, `get_attempt`, `list_attempts`,
`list_owned_attempts`) and the record types (`JobAttemptRecord` with `fresh()`, `AttemptRecord`,
`CacheEntryRecord`). Transitions wake the
`job:<id>` and `step:<key>` topics on commit. Each policy rule is one branch, so automatic retry,
approval flows, or holding abandoned work are local changes here.

**`runner.py`.** `JobRunner(database, job_attempt, *, execute, limits, settings,
verify_determinism)` and `run_job(database, job_id, registry, *, code_version, ...)`; `RunLimits` (concurrent steps;
workflow bodies are unbounded), `RunnerSettings` (the heartbeat, reconciliation, and poll
intervals), `WorkSink` (what an evaluator pushes work onto), `JobEvictedError`. The module docstring
is the reference for the queue's four sources, the item tasks, settling, and the four ways a run
ends; "The life of an attempt" above walks the attempt task. The runner logs one INFO line when an
attempt starts and one when it ends (WARNING when it failed), naming the job by its id's first
eight hex digits; this is the progress output the CLI shows.

**`attempts.py`, `recorder.py`, `evaluator.py`.** Described under "The life of an attempt" above.
The runs executed are the shared `WorkflowRun` / `StepRun` (`runner/assembly.py`), which alone
touch tokens, so an `Attempt` holds its `inputs` as identities and types and every operation on it
takes identities or nodes. An attempt takes no task group: it ends in memory synchronously and is
closed in the ledger by whoever can await, the attempt task last. `evaluator.py`'s
`derive(node, dag, leaf)` and `evaluate(dag)` are pure functions of the view, tested without a
database in `tests/test_runner_postgres_evaluator.py`; `evaluate` skips run nodes, which the
evaluator task expands and collects. It computes a query node like any other derived node,
through `nodes.compute`, which runs the plan in DuckDB on the event loop's thread; a query's value
is not stored for its own sake, so a restarted attempt runs the query again. `attempts.py`'s `StepAttempt` is a step attempt's runtime
object: an entity store and the same ending-and-closing lifecycle, with `complete` recording the
result, plus the log buffer and the background flush task (`flush_logs`, started by the attempt
task) that is the sole writer of the step's `ctx.log` lines.

**`jobs.py`.** `Jobs` (`client.jobs`) is the local inspection surface: `create(workflow, inputs,
root=, tx=)` checks the inputs against the signature and creates the job and its root invocation,
in the caller's transaction when given one (the client's `submit` creates the job and takes its
first job attempt in one) (the root
invocation's pathname and default cache key prefix are the `root` argument, `"root"` by default);
nothing runs until a process takes the job; an input is an `Array`, stored by the call, or the
bound id of an array entity already stored, a project array snapshot's typically, referenced as
it is. `list` returns every job newest first; `status` returns a `JobReport`: it lists the job
attempts and derives each invocation's state, each requested step's (from the step relation of
`status_queries.py`, the same judgement the evaluator reads, one query for the whole job joined to
the step names), and the job's, and lists its cache conflicts (`JobReport.cache_conflicts`: each
step in `conflict`, with the step and kind of what owns the key, from the step relation's
`entry_step_name` and the entry's offer); `wait` polls status behind the job's wake topic;
`result` loads the root's result; `describe` returns everything recorded (the job attempts;
invocations with nodes, spawns, attempts, and each attempt's events and `ctx.log` lines; each step
request with its binding, what the policy says the key is to the job, the attempt behind it, and
that attempt's log lines, read for all of a description's steps at once, each kind of row in one
query, so describing an invocation with thousands of mapped steps costs a few queries); `cancel(job, hard=, reason=)` cancels the active job attempt (see
"Cancellation"); `resume(job, owner=, code_version=)` takes the job for an owner and returns the new
job attempt (`NotResumableError` for a succeeded job). The descriptions also say which code ran,
read from the ledger: a `Provenance` is a job attempt's fact with its loaded `CodeVersion` (the
module-level `provenance(tx, entities, job_attempt_id)` reads one, `provenances` many); every attempt carries its
owner's, and `result_provenance` names the result's producer: an invocation's from
`job_attempt_id`, a bound step's from its step result (another job's, for a cache hit, and
available whether or not this database has the producing attempt).

**`client.py`.** `LocalProjectClient(database, project=, ...)` is the
`fxtr.project.client.ProjectClient` over a `Database`: it *is* the entity store (delegating to a
`PostgresEntityStorage`, `client.entities`), offers the project arrays under the ABC's names over
`ProjectArrays`, and runs the jobs it `submit`s or `resume`s in its own task group, one `JobRunner`
task each, returning a `LocalJobHandle` (`status`, `wait`, `result`, `cancel`, and `error`, what
this process's run raised). `submit` checks the workflow is registered, checks the environment
(`Environment.check_synced`, refusing with `EnvironmentNotSyncedError` unless verification is off
for the call or, by default, for the client), pins the code (`Environment.pin_code`), then creates
the job and takes its first job attempt in one transaction. `from_project(anchor)` locates the
project, loads its `.env` (`load_env_file`), imports its modules, connects, and creates the tables
on entry. Ctrl-C while entered: the first press interrupts every runner (drain), the second again
(abandon), the third cancels the block, which raises `KeyboardInterrupt`. The cache methods
(`list_cache_entries`, `clear_cache_entries`) are written over `execution` directly:
`execution.select_cache_entries` selects the entries, with each one's step and state, in one query.
A prefix with a wildcard value is narrowed in SQL to its literal text and then matched in Python
(`cache_addresses.AddressPattern.selects`). Their `conflicts_in` selector reads the job's conflicts
(`execution.cache_conflicts`); a clear by it removes only the completed ones, each only while its
entry still offers the step result that is in the way. `adopt_cache` makes the cache offer the step
results a job is bound to, local or imported, through `execution.adopt_step_result`: it creates or
takes over an entry where nothing live or equal is, and overwrites a different request's result only
when asked. `export_job` refuses a job with an active job attempt and otherwise writes its record in
one snapshot transaction; `import_job` reads a record file, checks its commits with the environment,
and in one transaction takes the job's row if it has the job (refusing it without `update`, or when
a process holds it) and inserts the record. `client.jobs` is the `Jobs` inspection surface, which
the viewer reads.

**`job_records.py`.** A job's record is the ledger's rule made concrete: a ledger row refers only
to ledger rows and entities, so `build_record` reads the job's rows (one select per table, each
keyed by the job or one of its invocations), the step results its bindings name, and the job
attempt facts its rows and those name, into a `fxtr.project.job_record.JobRecord`.
`export_job` encodes the record (it is not stored), collects the stored entities it reaches
(`reachable_blocks`: read in batches, decoded, followed), and writes them after the record as a CAR. Importing reverses it.
`read_record` keeps only the blocks the record's rows reach, walked from the rows (never the
record's own block), and refuses a file whose root is not one record of this format or that lacks a
block the rows reach. `insert_record` writes the entities, then merges every row by its table's
primary key (`_merge`: inserted if absent, compared if present, a difference refusing the import):
first the facts the record shares with the database (job attempt facts, `step_requests` rebuilt
from the request entities, step results), then the job's own rows, against everything the
database has of the job. That makes an existing job an *update*: refused if the database knows a
job attempt or a row of the job the record lacks, and otherwise extended, an invocation's
monotonic columns (`returned_node`, `result_cid`, `job_attempt_id`, `finished_at`) set where the
database has them null. Nothing is removed.

## Not yet built

An explicit evict command for a job whose process is known to be dead (today a resume takes it
over once its lease is stale); resuming a step directly by key.

## Tests

- `tests/test_runner_postgres_database.py`, `test_runner_postgres_entities.py`,
  `test_runner_postgres_json_values.py`, `test_runner_postgres_project_arrays.py`,
  `test_runner_postgres_client.py`: the plumbing, the entity store, the codec, project arrays and
  snapshots, the local client (submit, resume, handles, cancel and interrupt, the cache, and
  `from_project`, over a fake environment).
- `tests/test_runner_postgres_ledger.py`: the ledger's facts on their own: expansion, step spawns,
  the result.
- `tests/test_runner_postgres_job_records.py`: exporting a job's record (what it carries, a cache
  hit's producer included) and refusing a held or unknown job; importing it into a second
  project's schema (results and provenance as they were, nothing adopted), each refusal, a job
  whose producer's fact arrived with another job, and resuming an imported stopped job; updating
  an imported job from a later record, and refusing a held job, a job resumed on both sides, and
  a row the record lacks or contradicts.
- `tests/test_runner_postgres_execution.py`: the execution state and policy: the lease (heartbeat,
  a takeover racing an open heartbeat, eviction sweeping what a job attempt owns, the guard
  refusing only an attempt that is no longer running), job attempts and what they cover, what is
  due and starting it idempotently (two processes starting one invocation, a start racing an
  eviction, settling racing a start, two resumes of an untaken job, a resume settling a job
  attempt whose process died before settling), `status` and `invocation_state`, the step
  resolution rules through `resolve_step`, `start_attempt`, and `take_step_result` (two jobs
  starting one key, two takeovers of one key, a cache conflict, a failed attempt retried by
  another job, a stale owner's step waited on until evicted, each job taking a completed result
  and the binding surviving a clear), a soft cancel (of invocations, and of steps unstarted or draining) and a
  hard cancel. `tests/postgres_testing.py` holds the shared staging helpers, the runner helpers
  (`a_runner`, `runner_task`, `wait_until`), and `assert_policy_agrees`: the status relations'
  judgement of every step and invocation of a job against `resolve_step` and the due rules
  restated in Python (and that `pending_work` lists exactly what is due), which the staging
  helpers that change or read execution state run after each change.
- `tests/test_runner_postgres_attempts.py`: the attempts' lifecycle: the first outcome wins and
  ending is synchronous, a close cancelled in flight leaves the outcome for the next, a close the
  ledger already has is adopted, a step attempt completes or fails.
- `tests/test_runner_postgres_evaluator.py`: `derive` and `evaluate` on their own.
- `tests/test_runner_postgres_runner.py`: the runner over a real database: seeding, pushes,
  idempotence, settling `succeeded` and `stopped`, the running row covered from the moment it
  exists (a task cancelled while seeding or loading), the heartbeat noticing an eviction, a cancel
  from elsewhere and an interrupt draining, a second interrupt abandoning, the step limiter, a step
  held by an evicted job attempt taken over.
- `tests/test_runner_postgres_jobs.py`: jobs end to end through `run_job` and a runner in a second
  task: failures, a runner stopped mid-body and the job resumed, determinism, the verification run, a
  project array snapshot as a root input, job inspection, child workflows, steps across jobs (sharing an attempt, a clear leaving
  bindings intact, a failed step interrupting every waiting job, a cache conflict until cleared,
  one key for two requests failing, a pending job taking over an evicted step, two runners sharing a step,
  a pending step waking on its key's topic), cancellation (soft, hard, a runner started on a
  cancelled job attempt, two-level Ctrl-C, an evicted runner's late writes refused), and logging.
- `tests/test_runner_postgres_debug_web.py`: the viewer's readers over a job run through
  `run_job`.
- `tests/test_semantics_reference.py`: the backend-neutral reference examples of a correct
  execution, run on this backend through the `postgres` entry of `tests/reference_backends.py`
  (each job on its own throwaway schema, through `run_job`).
