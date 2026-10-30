# Services: task fns, steps, and bindings

Entities are the data half of fxtr; **services** are the code half.
This guide explains what a service is, the three service-shaped things
an fxtr_experiment workflow uses (task fns, step fns, and other bound
services), and how you attach your services to a running workflow —
focusing on the same-process case, with sandboxing covered at a high
level (and in guide 9).

## What a service is

A **service** is a live capability: a Python object mounted into a
scope, callable through a `ServiceRef` via fxtr's internal RPC system.
Like an entity it has a **type** (an id naming its protocol/contract),
but unlike an entity it has a *lifetime*: it stays callable while its
mount scope is open, and tears down when that scope drains.

The typed flavor looks like this (full guide:
`v2/notes/scope_service_lifetimes.md`):

```python
from typing import Protocol
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

@service_protocol
class JudgeProto(Protocol):
    async def judge(self, rubric: str, transcript_text: str) -> float: ...

class JudgeImpl(TypedServiceImpl[JudgeProto]):
    def _target_type_annotation(self): return JudgeProto
    def _service_type_id(self): return MY_JUDGE_TYPE_ID

    @service_method
    async def judge(self, rubric: str, transcript_text: str) -> float:
        ...  # e.g. call an LLM API

ref = await scope.mount(JudgeImpl())          # ServiceRef
proxy = await ref.resolve(using_scope)        # typed proxy: JudgeProto
score = await proxy.judge(rubric, text)
```

Two properties make services the right seam for experiments:

- **Location transparency.** A `ServiceRef` can be passed across
  process boundaries and resolved remotely; calling code cannot tell
  (and should not care) whether the impl is in-process, in another
  process, or in a sandbox.
- **Services can carry services.** Methods can accept and return
  `ServiceRef`s, so you can express things like "a connector that,
  given a config, hands back a configured session service".

For per-mount resources (an HTTP client, a background task), implement
the `_fxtr_mount_lifespan` hook; for the cross-scope lifetime rules
(when you need `bridge_service_from_scope` and which `keepalive_via` to
pick), see `v2/notes/scope_service_lifetimes.md`. Most experiment
authors only need the mount/resolve basics above.

## The three service-shaped things in a workflow

An fxtr_experiment workflow's *code surface* is exactly three kinds of
things, all of which sit behind service boundaries:

### 1. Task fns

A **task fn** is the outer-level program of a task grid: any async
callable of shape `(ctx, /, **kwargs)` decorated with `@task_fn`
(`fxtr_experiment.task_service`). Its kwargs arrive as **views** (typed
windows onto grids — guide 4), it performs effectful ops through them
(reads, writes, opens, gathers, steps), and it returns its outputs
(entity ids and/or polars frames).

The crucial contract: a task fn's outer level must be
**deterministic given its recorded inputs** — same reads in, same ops
and outputs out. The framework *verifies* this (the safety pass,
guide 5) rather than trusting it. All real nondeterminism — LLM calls,
sampling, wall-clock — belongs inside steps.

### 2. Step fns

A **step fn** is the checkpointed unit *inside* a task: an async
callable decorated with `@step_fn(type_id=define_type(NS, "name"))`
(`fxtr_experiment.view_runtime`). The `type_id` is the step's durable
identity — it names "what this step does" in the checkpoint records.

Steps consume and produce **plain data only** (JSON-ish values and
`FxtrID`s — never views), and they are where nondeterminism is legal:
each `run_step` invocation is recorded durably with its arguments and
result, so on a re-run a completed step's result is served from the
checkpoint instead of re-executing. Steps are also the **retry unit**:
a step that raises fails the current run but leaves no checkpoint, so a
later re-demand re-attempts exactly that step (completed steps replay).

The one workflow operation available inside a step is
`ctx.connect(key)` — exchanging a previously-read handle key for a live
service proxy (below).

### 3. Bound services (handles + bindings)

Everything *external* a workflow uses — LLM providers, user simulators,
judges, and the task fns themselves — enters through **handle grids**
and **bindings**:

- A **handle cell** holds a **key**: an ordinary content-addressed
  entity that *names* a service slot (e.g. "the Anthropic-backed judge
  service"). Keys are data — identity, not connectivity. Writing a
  different key into a handle cell is an identity-level change and
  fans staleness through everything computed against the old key.
- A **binding** is runner-side configuration answering "what actually
  serves this key in this runner". Bindings are deliberately *not*
  identity-bearing: swapping a binding (same key, new impl) never
  invalidates results — which is exactly what you want for "same
  service, new credentials/endpoint", and exactly what you must *not*
  use for "actually a different judge" (change the key for that).

Task code's authority model is simple and auditable: **handles cross
as keys; authority follows the record.** A task's outer level reads a
handle cell's key (`await view.read_key(...)` — an ordinary recorded
observation), passes the key into a step as plain data, and the step
calls `ctx.connect(key)`. Only keys the current generation actually
read from handle grants may be connected; ids smuggled through data
confer nothing, and tasks can never create handle grids or write
handle cells. Consequence: the complete external-authority surface of
a workflow is the handle grids the user populated plus the bindings
registered on the runner — auditable without reading any task code.

## Attaching services in the same-process case

The demo's runner mounts are the pattern to copy
(`examples/fxtr_experiment_demo/experiment_runner.py` and
`behavior/behavior_runner.py` — read their module docstrings; the
scope topology comments are load-bearing). The skeleton:

```python
# 1. The durable session (two sqlite files) + the runner machinery.
async with contextlib.AsyncExitStack() as stack:
    host_scope = await stack.enter_async_context(FxtrScope(name="my-host"))
    session = await stack.enter_async_context(
        WorkflowSession.open(str(d / "state.sqlite"), str(d / "entities.sqlite")))
    runner_scope = await stack.enter_async_context(
        FxtrScope(name="my-runner", isolated=True,
                  include=[session.entity_scope, GLOBAL_SCOPE]))
    bindings = BindingRegistry(runner_scope)
    executor = Executor(session, bindings, runner_scope)
    impl = UserCapabilityImpl(session, executor, bindings, runner_scope)
    cap_ref = await runner_scope.mount(impl)
    proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(runner_scope)
    wf = await Workflow.connect(proxy)

    # 2. Bind a service: mount the impl, wrap it as a connector,
    #    register it under its key.
    key = MyJudgeKey(...).store(runner_scope).entity_id   # content-addressed
    svc_ref = await host_scope.mount(JudgeImpl(...))
    connector = await wrap_singleton(host_scope, svc_ref)
    release = await wf.bind_handle(key, connector)

    # 3. Bind task fns the same way — a @task_fn adapter IS a service impl.
    rollout_key = RolloutFnKey(...).store(runner_scope).entity_id
    fn_ref = await host_scope.mount(rollout_task)         # the decorated fn
    release2 = await wf.bind_handle(rollout_key, await wrap_singleton(host_scope, fn_ref))
```

**NEEDS CONFIRMATION**: in `behavior_runner.py` the task-fn adapters are
passed to `host_scope.mount(...)` directly (the `bind` helper mounts
`service: TypedServiceImpl`), so the sketch above matches — but please
check whether `wrap_singleton` + `bind_handle` is still the recommended
minimal spell, and whether `user_service.open_local` should be shown
instead as the "just give me a workflow in a notebook/test" bootstrap
(it exists and is described as the local bootstrap; the demo uses the
full mount because it hosts the capability in a workstation).

Points worth internalizing rather than cargo-culting:

- **Keys are content-addressed entities**, so every relaunch that
  constructs "the same" key entity binds the same key id the workflow's
  handle cells already name — that's what makes re-demand-after-relaunch
  work with zero ceremony. Put everything identity-bearing (model name,
  provider, role) *in the key entity*; put machine-local tuning
  (timeouts, base URLs) in binding config, never in the key.
- **Ephemeral bindings are per-mount runner config.** They live as long
  as the connector service (and can be released early via the returned
  release capability). A reopened or imported workflow re-binds — by
  design; if nothing rebinds a key, demands that need it fault with a
  "handle unbound" error until something does. Bind-then-rerun is the
  normal recovery, not an exceptional one.
- **The scope topology matters**: the host scope (where connectors are
  mounted) must *enclose* the runner scope, because the binding
  registry bridges connectors into the runner scope with independent
  leases. Copy the demo's topology; the reasoning is in
  `experiment_runner.py`'s docstring and
  `v2/notes/scope_service_lifetimes.md`.

### Hosting in a workstation

To get the dashboard, you host the runner inside a **fxtr
workstation** (`projects/fxtr_workstation`) and register the user
capability as an external service (the demo registers
`experiment-workflow` / `behavior-workflow`). Any workstation client —
the browser shell, the agent MCP plugin, your own scripts — then speaks
the same `UserCapabilityProto` wire contract. Copy
`examples/fxtr_experiment_demo/run_experiment_demo.py` +
`experiment_runner.py` for the full assembly (workstation, mounts, UI
preset, Vite frontend).

## Sandboxed bindings, at a high level

Same-process bindings require *you* to run the service code, trusted,
in your host process. The **sandboxed binding** class removes both
constraints: instead of a live connector, you register **data** — a nix
service definition (a git commit + flake attribute in the
`fxtr_sandboxing` currency), an optional JSON config, and a pinned map
of dependency handles. The runner saves the record durably in the
state db, exports it in the workflow's bindings file (a lockfile), and
spins the service up on demand through `fxtr_sandboxing` — an OCI
container, a wasm sandbox, or a pyodide/deno sandbox, per the
definition.

```python
await wf.bind_sandbox(
    key,
    defn={"git_commit": "<commit>", "flake_attr": "fxtr.services.my_judge"},
    dependencies={"llm": <handle-cell selection>},   # pinned at bind time
    config={"max_concurrency": 4},
)
```

**TODO**: the exact `bind_sandbox` signature above is a sketch — phase 6
chunk 2 (`plans/phase6_sandboxing/`) defines the real wire surface;
update this snippet when it lands. As of this writing, chunk 1 (the
uniform `BindingConnectRequest` contract) is implemented and the durable
records/serving/export chunks are in progress, so **only ephemeral
bindings actually work end to end today**.

What stays uniform across both classes:

- One key is bound in at most one class at a time (no shadowing).
- The bound implementation receives one standard request shape —
  `fxtr_sandbox_types.BindingConnectRequest(key, config, handles)` —
  whether it's an in-process connector or a sandbox guest entrypoint,
  so moving an implementation between classes is mechanical.
- Neither class is identity-bearing. Swapping any binding without
  changing its key carries the **compatibility ratchet** obligation:
  you are asserting that past results would have been the same (in
  distribution) under the new implementation.

Sandboxed bindings additionally need the runner pointed at a repo
containing the pinned commits (`set_sandbox_repo` — machine-local,
never exported); until then imported sandboxed bindings sit inert and
demands fault with a "repo not configured" error. Guide 9 covers how to
package a service for sandboxing; `notes/sandboxed_bindings.md` is the
canonical design writeup.

## Which kind of service should this be?

| you are writing... | make it a... |
| --- | --- |
| the per-cell program of a task grid | task fn (`@task_fn`), bound under a handle key |
| an LLM call / anything nondeterministic inside a task | step fn (`@step_fn`), invoked via a checkpoint grid |
| an external capability tasks consume (model API, simulator, tool) | a service impl bound under a handle key (ephemeral or sandboxed) |
| interactive glue driving the workflow from outside (buttons, scripts) | an ordinary workstation service holding the `Workflow` wrapper — not part of the workflow at all (see the demo's driver services) |
