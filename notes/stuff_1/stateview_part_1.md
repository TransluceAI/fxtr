Goal: Build a stateful debug UI for launching and monitoring tasks.

# Current state

The fxtr library exists and is designed as described in lib/fxtr/README.md.

Tasks can be specified in "blueprints" like `lib/fxtr/examples/demo_09_15/user/demo_09_15_user/__init__.py`, which depend on library helper types like `lib/fxtr/examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py`. These use Tasks which use a set of types defined in lines 3811 (TaskFunction) to 4029 (Run) of `lib/fxtr/fxtr/object_model/core.py` (at time of writing).

Blueprints define collections, which are as defined in `lib/fxtr/fxtr/object_model/collection_tracking.py`.

There is a command line program at `lib/fxtr/fxtr/api/fxtr_cli.py` which can be used to view and launch tasks. It uses other files in the `api` subpackage. In particular `lib/fxtr/fxtr/api/task_launcher.py` is how tasks are launched.

While tasks are running, a temporary UI is shown, defined in `fxtr.api.task_launch_view`. This UI shows task progress as tasks run, in a dag, but closes when they finish.

Afterward, you can use `fxtr serve` to launch a custom debug UI from `lib/fxtr/fxtr/debug_visualizer`. This can pull in the results of tasks when you refresh but it doesn't have any sort of live view.

If you change the module package, `fxtr serve` will update the next time you run it. It also does an autorefresh trick if you manually go back to the index page. But it is very manual.

To summarize, these views are all separate:
- the `fxtr ls` and `fxtr materialize` commands that show and launch tasks in the CLI
- the temporary web view that runs while tasks are launching
- the debug UI where you look at results and task data

# Desired state

All of these interfaces should be combined into a single interactive web UI that manages state and runs locally. This should have two parts, both in Python.

**The Backend**
The backend should be a hybrid of `lib/fxtr/fxtr/debug_visualizer/locator_server.py` and the current logic in `lib/fxtr/fxtr/api`. It should live in `lib/fxtr/fxtr/api` as a new file `run_manager.py`.

The backend should be launchable over CLI via typer. It should take as input a python module name or entry point string, which should resolve to a fxtr collection using the current rules. When run, it should start a server that has a few different routes:
- GET `/block/{cid}`: a block loading route as it currently exists
- GET `/state/collection_stream`: a server-sent-events route (sse-starlette) that continually yields the CID of the active collection, once at start and then whenever it updates.
- GET `/state/task/{cid}/run_stream`: a server-sent-events route that continually yields runs for the given task, as a list of run states.
    - run states should be modeled on how they are currently represented in `lib/fxtr/fxtr/api/api_lib.py`
- GET `/state/plan/{cid}/state_stream`: a server-sent-events route (sse-starlette) that continually yields states for the given plan
- GET `/state/debuginfo`: a JSON of all of the tasks and plans that are currently being tracked by the runner
- POST `/launch/launch_tasks`: takes a JSON with the same structure as the CLI arguments to `fxtr materialize` and launches those tasks.
- POST `/launch/reset`: shut down and restart the task runner

The backend should maintain state for the collection, tasks, and runs that it has, so that it can serve the above.

The backend should have a few moving parts that maintain their state.

*Server*: The server component responds to routes. It should also maintain some pubsub state, so that we can push updates for the collection tasks and plans and have it maintain the SSE over this and also send the most recent state at each step.

*Collection watcher*: The backend should identify the file where the collection module resolved to, and watch this file. Every time it changes, it should re-execute it using importlib, and send an event out.

*Task runner*: There should be an object `BackendRunner`, constructed in a contextmanager (classmethod), which acts similarly to `run_all_tasks` in `lib/fxtr/fxtr/api/task_launcher.py`, but is dynamic.

Its state:
- Essentially the internal state of the `run_all_tasks` (from e.g. `plan_done`, `plan_provenance`, `task_done`, `run_cid_for_task_handle`), but with connections to pubsub so that it can send messages.

Its primary methods:
- `async def _setup_plan(self, plan)`: Set up a plan, and register it. Assumes all of the deps must have been set up already.
- `def launch(self, tasks: Collection[Task])`: Prepare the deps of all plans in this set. This implies checking the dependencies, identifying runs for task result handles (either to previously-running tasks or to saved run data), calling `prepare` on the py provider for the new plans, starting `_setup_plan` workers in a BG task group for plans that aren't yet scheduled, and starting `_run_task` workers for the tasks.
    - Note that if the previous `_setup_plan` call has failed, we should re-start it here. But if a previous `_setup_plan` call succeeded or is still setting up, we should just re-use it.
    - If one of these tasks is already running, `launch` should fail immediately with an error.
    - However `launch` should return once the task workers start running, not wait for them to finish.
    - Running a task should _unset_ the `task_done` for that task if it is set, and _reset_ the provider for its handle, so that later tasks will point to the new run by default.


**The Frontend**
TODO TODO TODO


# Work plan

## Backend
TODO

## Frontend
TODO

# Work status

Not started
