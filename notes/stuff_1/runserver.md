Goal: Decouple launching of tasks from managing their state, to support alternative launchers.

# Current state

There is a command line program at `lib/fxtr/fxtr/api/fxtr_cli.py` which can be used to view and launch tasks. It uses other files in the `api` subpackage. In particular `lib/fxtr/fxtr/api/task_launcher.py` is how tasks are launched.

Run data is stored in a "state directory" which includes both a log of all runs of each task, and some "runsets" which each store compatible collections of runs.

The launcher handles all parts of task execution including the setup of plans and saving of the results.

# Desired state

We refactor the launcher CLI program so that it talks to a "runserver", which is responsible for running tasks and instantiating plans.
- When launched without an existing runserver, the launcher CLI creates a temporary one, uses it, and shuts it down.
- `fxtr runserver` should spawn a persistent runserver, and serve an API on some {host}:{port} pair, saved to a `runserver.json` file in the state dir.
- When the launcher CLI wants to materialize tasks, if there is an existing runserver it dispatches the command to that runserver.

The runserver should use an object `BackendRunner`, constructed in a contextmanager (classmethod), which acts similarly to `run_all_tasks` in `lib/fxtr/fxtr/api/task_launcher.py`, but is dynamic. (the contextmanager should create the `BackendRunner` with `__init__` passing a task group and a shutdown event, similar to the pattern of e.g. `UVManagedPyImplProvider`)

BackendRunner state: The BackendRunner should keep track of
- an active plans map from plan to a new PlanWorkerState type
    - each storing
        - its plan
        - PlanWorkerStates for all of its direct depenencies
        - a `ready` event
        - a `shutdown` event
        - a `plan_provenance`
        - an optional FxtrProtocolImpl that gets set to a plan when ready
        - a `user_count`
        - a `provider`
        - a status and a message
- a "stale plan states" list of PlanWorkerState
- a tasks map from task to a TaskWorkerState type
    - each storing
        - its task
        - a PlanWorkerState for its task function
        - a `done` event
        - an optional run CID that gets set to a specific run when ready
        - a status and a message
- a "stale task states" list of TaskWorkerState

How BackendRunner works:
- When requesting a plan:
    - if it is in the active plans map, increment its user count
    - if not, set it up
- When setting up a plan:
    - Figure out how we will be providing the plan
        - e.g. is it a python plan or a secrets plan or something else
        - if it is a task result handle, if it is currently in the runset, use that. else if there currently is a scheduled task for it in the active tasks map, use a provider that waits to resolve that task and uses its run
    - Walk all deps of the plan and request them
    - Create a PlanWorkerState with the plan, initialized with one user and the active PlanWorkerStates for all the dependencies
    - Create an anyio task to manage that PlanWorkerState
        - it should wait for all direct dependencies to be ready
        - it should check if any direct deps were cancelled or errored and if so cancel this one (marking as ready with a cancelled status and no impl), then clean itself up, set its own shutdown event, remove itself from the active plans map
        - else it should provide itself via its provider, set its ready event, wait for shutdown event
        - in a finally block if it errors out while providing or holding its plan, it should set its own shutdown event and remove itself from the active plans map
- When marking a plan stale
    - Move the plan out of the active plans to the stale plan states
- When launching a task:
    - make sure its not in the active tasks map
    - request the taskfunction plan for the task
    - create a TaskWorkerState
    - create an anyio task to manage the TaskWorkerState
        - it should wait for its impl from the PlanWorkerState
        - it should then build its provenance by walking the PlanWorkerState
        - it should then run the task
        - if it fails it should mark itself done without a run, and remove itself from active tasks
        - if it succeeds it should set its run, mark itself as done, remove itself from active tasks
- When marking a task stale
    - Move the task out of the active tasks to the stale task states


BackendRunner public methods:
- `async def launch(self, tasks: list[Task])`: Prepare the deps of all plans in this set. This implies checking the dependencies, identifying runs for task result handles (either to previously-running tasks or to saved run data), calling `prepare` on the py provider for the new plans, starting `_setup_plan` workers in a BG task group for plans that aren't yet scheduled, and starting `_run_task` workers for the tasks. Returns a dict of events that can be used to wait for tasks to finish.
    - Note that if the previous `_setup_plan` call has failed, we should re-start it here. But if a previous `_setup_plan` call succeeded or is still setting up, we should just re-use it.
    - If one of these tasks is already running, `launch` should fail immediately with an error.
    - However `launch` should return once the task workers start running, not wait for them to finish.
    - Launching a task should only be possible if any previous TaskResultHandle items it has are either (a) already in the runset, or (b) are scheduled for execution in the BackendRunner.
    - In the `_run_task` worker:
        - Plans should be set up as before
        - However, immediately before invoking the task function, we should check all RunProvenance and make sure they are currently in the active runset, and that there is _not_ currently a run for this task in the runset. If this fails we should cancel the task execution.
- `async def watch(self) -> Watchable`: Gets a Watchable object that yields state updates. State updates include the current state of all plans and tasks, and are emitted for plans spinning up, plans spinning down, tasks scheduling, tasks starting execution, tasks finishing, generally any update to the state of a plan or a task. (Watchable is defined in `fxtr/builtin_util/watchable.py`)

Additionally, each plan should be associated with a user counter. Each time a plan is depended on by another plan or by a task, its user counter gets incremented; this happens right before we check if the `_setup_plan`  worker is still running and launch it if not. Each time a task completes, it decrements the user count of its task function. When a user count reaches zero, we set a "shutdown" event, which the plan worker should use to shut down; we also clear the state so that the next depender re-launches the _setup_plan worker. When the plan worker shuts down, it reduces the user count of everything it depends on.

Whenever any task finishes, we should immediately write the run to the run state dir and also update and write the runset to the runset file. (To best-effort try to prevent conflicts, we do this by reading the runset, adding the run if possible, and writing it back if we succeeded updating it. If we can't add it due to a conflict, we still save the results of this run, but do NOT add it to the runset.)

When the BackendRunner provides results for TaskResultHandle, it should always provide results from the current runset. However, if a task depends on another task that is currently running, the provider should wait on an event that will be set when the task completes, so that we don't try to retrieve its data before it is done.

The BackendRunner should also watch the active runset (using watchfiles). Whenever the runset changes:
- for every run that was _removed_ from the runset or _added_ to the runset, we should find every plan OR task that transitively depends on the task-result-handle for that task (if any), and immediately mark their worker states as stale, preventing new users from finding them.

The current `task_launch_view` can be disabled by just not using it. We will replace it with something more powerful soon.

The `fxtr runserver` command should create a BackendRunner and expose it over RPC as an implserver while it runs.

# Phase 1
## High-level plan
- Implement backend runner object as described above, in a new file `api_lib/backend_runner.py`.
- Rewire `fxtr materialize` to start a BackendRunner and drive it, then shut it down after. Have `fxtr materialize` print state updates to rich console.
- Remove `task_launcher.py`


# Phase 2
## High-level plan
- Refactor BackendRunner to be an implementation of a fxtr protocol
    - Create Protocols registered with @core.define_protocol similar to how _UV_SPAWNER_PROTOCOL in fxtr/providers/uv_py_provider.py is implemented by fxtr/providers/uv_py_provider_entrypoint.py
        - Create a Protocol called TaskWaiter, which has one method which is `wait` which returns a string
        - Create a Protocol called BackendRunnerInterface, which has the public methods of BackendRunner, except that the launch command returns TaskWaiter instead of events
        - Implement TaskWaiter with a concrete class TaskWaiterImpl which takes a task worker state, and when wait is called it waits on the task_done event and returns the status
        - Configure BackendRunner to be an implementation of BackendRunnerInterface
    - Change caller in fxtr/api/fxtr_cli.py to use new api

- Add a command `fxtr runserver`
- Have it start an implserver serving a BackendRunner, and write its host and port to the state dir. It should also error out if there is already another server with its state in the state dir.
- Intercept ctrl-c in `fxtr runserver` to clean up, using anyio's signal handling https://anyio.readthedocs.io/en/stable/signals.html and a shutdown event.
- Remove the host and port pair from the state dir right before shutting down the server (in a finally block so it removes it when killed for any reason)

- Modify `fxtr materialize` to look for a running runserver in the statedir file
- If it finds it, it should attach to the existing server as a RemoteImpl
- If not it should spawn one locally and use that one (which is the current behavior)
- `_materialize_with_runner` can be changed to take the runner as input, and then the caller can create the runner or attach to the existing one
