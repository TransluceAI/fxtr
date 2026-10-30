# Isolation Spike Plan

## Goal

Explore sandboxed Python execution using [Eryx](https://docs.eryx.run/latest/book/) (CPython 3.14 in WASM), with a focus on designing a content-addressed "task module" format that users author as normal Python projects, build into reproducible bundles, and run in isolation.

This is a temporary spike to learn how the system works and validate the packaging design. The communication protocol is intentionally minimal — the real RPC system will be layered on later.

## Three-stage model

```
Author time     pyproject.toml + *.py files        (user writes, normal Python project)
                        │
                        ▼  prepare()
Prepare         uv lock → uv export --format pylock.toml → read source files
                        │
                        ▼  bundle()
Bundle          TaskBundle fxtr entity             (content-addressed, platform-independent)
                  - source_files: dict[str, str]     path → content
                  - pylock_toml: str                 full pylock.toml contents
                  - entry_point: str                 e.g. "scorer.main:run" (module:function)
                  - requires_python: str
                        │
                        ▼  run()
Execute         parse pylock.toml → fetch/cache wheels → load into eryx → run
```

### Stage 1: Authoring

A task module is a directory containing a normal Python package:

```
my_task/
  pyproject.toml
  src/
    scorer/
      __init__.py
      main.py
      helpers.py
```

The pyproject.toml is a standard Python project. The task entry point is declared using the standard `[project.entry-points]` mechanism:

```toml
[project]
name = "my-scorer"
version = "0.0.0"
requires-python = ">=3.14"
dependencies = ["pyyaml>=6.0", "jinja2>=3.1"]

[project.entry-points."fxtr.task"]
main = "scorer.main:run"
```

The entry point function receives a context object with `send()` and `receive()` methods — no magic globals:

```python
# scorer/main.py
import json

async def run(ctx):
    msg = json.loads(await ctx.receive())
    result = do_work(msg)
    await ctx.send(json.dumps(result))
```

This is a completely normal Python package — importable, testable outside the sandbox by passing a mock context. The user can develop locally with `uv run` as usual.

For simpler tasks that don't need a `src/` layout, a flat structure also works:

```
hello_task/
  pyproject.toml
  hello.py           # contains the entry point function
```

### Stage 2: Prepare + Bundle

`prepare(task_dir: Path) -> TaskBundle`:

1. Validate: check pyproject.toml exists, read `[project.entry-points."fxtr.task"]` for entry point(s), verify the referenced module file exists.
2. Resolve deps: run `uv lock` (if needed) then `uv export --format pylock.toml --output-file pylock.toml` as a subprocess. This produces a fully resolved lockfile with URLs and hashes for every transitive dependency.
3. Collect source files: glob all `.py` files in the task directory, read their contents, store as `dict[str, str]` (relative path → content).
4. Construct a `TaskBundle` entity containing source files, pylock.toml contents, entry point spec, and python version.
5. Store the entity into a fxtr scope (caller provides). Return the `EntityRef[TaskBundle]`.

The TaskBundle is content-addressed — identical task modules produce identical bundles. The pylock.toml pins exact versions + hashes, so the bundle is fully reproducible.

### Stage 3: Execute

`run(bundle: TaskBundle, ...) -> TaskSession`:

1. Parse pylock.toml from the bundle using `packaging.pylock.Pylock.from_dict()`.
2. For each package, find a `py3-none-any` wheel (pure Python, universal). Error if no compatible wheel found. sdists are ignored — we only accept pre-built wheels (building from source would require running arbitrary code and defeats the reproducibility/sandbox goals).
4. Check a local wheel cache (directory, keyed by sha256 hash). If not cached, download from the URL in pylock.toml using `httpx`, verify hash, cache.
3. Create an Eryx `SandboxFactory` with all the fetched wheels.
4. Create a sandbox from the factory.
5. Mount the task source files into the sandbox's VFS so they're importable.
6. Register `_send_to_host` and `_receive_from_host` as eryx callbacks.
7. Execute the internal runner script (`_runner.py`), which imports the entry point module, constructs a `TaskContext`, and calls the entry point function.

### Communication protocol (minimal for spike)

The system ships an internal **runner script** (`_runner.py`) that eryx executes. This script:
1. Sets up the eryx callbacks as internal plumbing
2. Constructs a `TaskContext` object wrapping send/receive
3. Resolves the entry point (from the bundle manifest)
4. Calls the entry point function, passing the context

Two eryx callbacks provide the raw transport:

- `_send_to_host(data: str)` — sandbox sends a string to the host
- `_receive_from_host() -> str` — sandbox blocks until the host sends a message

These are **not** visible to the task code. The task code receives a `TaskContext`:

```python
class TaskContext:
    async def send(self, data: str) -> None: ...
    async def receive(self) -> str: ...
```

Host-side async interface:

```python
class TaskSession:
    async def send(self, data: str) -> None: ...
    async def receive(self) -> str: ...
```

Internally, each direction uses an `anyio.Event` or memory channel for synchronization. The sandbox runs in a single `execute()` call that runs the runner script.

The task entry point is a normal async function:

```python
# scorer/main.py
async def run(ctx):
    msg = await ctx.receive()
    result = process(msg)
    await ctx.send(result)
```

Or for a long-running message loop:

```python
async def run(ctx):
    while True:
        msg = await ctx.receive()
        if msg == "shutdown":
            break
        await ctx.send(process(msg))
```

This is testable outside the sandbox by passing a mock context.

## Implementation steps

### Step 0: Validate eryx basics

- [x] Install pyeryx and verify it works
- [x] Run a hello-world sandbox (basic execute)
- [x] Test callbacks (register a callback, call it from sandbox code)
- [x] Test package loading (install a pure-Python wheel, import it in sandbox)
- [x] Test VFS (mount files, import them from sandbox code)
- [x] Test full pattern: SandboxFactory (wheels) + volume mount (source files) + callbacks (bidirectional comms)
- [x] Document any surprises or API differences from the docs

**Step 0 findings:**
- `volumes` parameter takes `List[Tuple[str, str, bool]]` — `(host_path, guest_path, read_only)`. Not documented in Python help.
- Top-level `await` works in eryx `execute()` — no need for `asyncio.run()`.
- `Session` cannot use `SandboxFactory` packages directly. Use `factory.create_sandbox()` + volumes for source files instead.
- Callbacks that block (like `queue.Queue.get()`) work fine — the sandbox blocks on the WASM side while the host callback runs in a real thread.
- Sandbox `execute()` is blocking (not async), so it must run in a thread pool for async host code.
- `SandboxFactory` creation takes ~3-5s (one-time), but `create_sandbox()` is fast (~10-20ms). Factory can be saved/loaded for reuse.

### Step 1: TaskBundle entity + prepare()

- [x] Define `TaskBundle` as a `DataclassEntity` in `entities.py`
- [x] Write `prepare(task_dir: Path) -> TaskBundle` in `prepare.py`:
  - Parse pyproject.toml, validate, read `[project.entry-points."fxtr.task"]`
  - Run `uv lock` + `uv export --format pylock.toml` as subprocess
  - Collect .py source files
  - Construct and return TaskBundle
- [x] Write a simple example task module in `examples/hello_task/` (with entry point)
- [x] Test: prepare an example task, verify entity is created, verify content-addressing (same input → same ID)
- [x] Update CODE_OVERVIEW.md

### Step 2: Wheel fetching

- [x] Write `fetch_wheels(pylock_toml: str, cache_dir: Path) -> list[Path]` in `wheels.py`:
  - Parse pylock.toml via `packaging.pylock`
  - For each package, find `py3-none-any` wheel
  - Check cache by hash, download if missing, verify hash
  - Return paths to all fetched wheels
- [x] Test: fetch wheels for a simple dep set (six), verify caching works
- [x] Update CODE_OVERVIEW.md

**Step 2 findings:**
- `packaging.pylock.Pylock.from_dict()` requires `created-by` field — uv includes this, but hand-crafted pylock files need it too.
- `PackageWheel` has `url`, `hashes`, `size`, `name` (often None), `path`, `upload_time`.
- The wheel filename must be extracted from the URL path. `name` attribute on PackageWheel refers to the optional `name` field in pylock.toml, not the filename.
- pylock.toml with no dependencies omits `[[packages]]` entirely, which causes `Pylock.from_dict()` to fail — we handle this by checking for the key before parsing.
- pyyaml has no `py3-none-any` wheel (C extensions only) — switched example to use `six` instead.

### Step 3: Sandbox execution

- [x] Write the internal `_runner.py` script (executed inside sandbox):
  - Sets up sys.path, defines TaskContext inline (callbacks only in top-level scope), imports entry point, calls it
- [x] Write `run(bundle, cache_dir) -> AsyncContextManager[TaskSession]` in `runner.py`:
  - Call `fetch_wheels` to get wheel paths
  - Create Eryx SandboxFactory with wheels
  - Volume-mount source files
  - Register send/receive eryx callbacks backed by blocking queues
  - Run sandbox in background thread via anyio task group
- [x] Write `TaskSession` class with `send()`, `receive()` (async wrappers over blocking queues)
- [x] Test: run hello_task and yaml_task examples, exchange messages, verify isolation
- [x] Update CODE_OVERVIEW.md

**Step 3 findings:**
- Eryx callbacks are only available in the top-level execution scope — imported modules can't see them. TaskContext must be defined inline in the runner script.
- `run()` is an `@asynccontextmanager` that owns a task group; the sandbox thread is cancelled when the context exits.
- The WASM sandbox has no host filesystem access — `os.listdir('/')` raises `No such file or directory`. Good isolation.
- `_task_context.py` turned out to be unnecessary; removed.

### Step 4: End-to-end example

- [x] ~~Write a more interesting example task~~ — yaml_task with `six` dep already demonstrates this
- [x] ~~Test full flow~~ — test_runner.py covers the full prepare → fetch → run → communicate flow
- [x] ~~Update CODE_OVERVIEW.md~~ — already up to date from step 3

## Package structure (target)

```
projects/fxtr_sandboxing/
  pyproject.toml
  CLAUDE.md
  CODE_OVERVIEW.md
  plans/
    spike_plan.md              # this file
    spike_plan_scratch.md      # working notes
  fxtr_sandboxing/
    __init__.py
    entities.py                # TaskBundle DataclassEntity
    prepare.py                 # prepare(): task dir → TaskBundle
    wheels.py                  # fetch_wheels(): pylock.toml → wheel paths
    runner.py                  # run(): TaskBundle → AsyncContextManager[TaskSession]
    _runner.py                 # Internal script executed inside the sandbox
  tests/
    __init__.py
    test_prepare.py
    test_wheels.py
    test_runner.py
  examples/
    step0_eryx_demo.py         # Standalone demo of eryx basics
    hello_task/                # Minimal task (no deps)
      pyproject.toml
      hello.py
    yaml_task/                 # Task with external dep (six)
      pyproject.toml
      yaml_task.py
```

### Step 5: Pure Python source dependencies

- [ ] Design how task modules can depend on shared pure-Python code that isn't published to PyPI
- [ ] Options to evaluate:
  - **Option A**: Build wheels from path/git deps during prepare(), embed wheel bytes in bundle
  - **Option B**: Bundle source files directly (skip wheels for pure Python), mount at runtime
  - **Option C**: Hybrid — uv for resolution, source bundling for delivery (likely best)
  - **Option D**: Allow certain sdists with "safe" build backends (e.g. hatchling, flit — no arbitrary code). Interesting but complex.
- [ ] Test what pylock.toml produces for path deps and git deps (what info is available?)
- [ ] Decide on a format and implement
- [ ] Update CODE_OVERVIEW.md

## Open questions / deferred

- **WASI native extensions**: There is currently no standard platform tag for WASI wheels (as of March 2026). Once a convention is established, the wheel selection logic could fall back to WASI-tagged wheels when no `py3-none-any` wheel is available. Deferred.
- **Eryx pre-compilation**: Could bake wheels into a pre-compiled runtime for faster startup. Deferred — not needed for spike.
- **Bundle storage**: Currently the TaskBundle entity lives in a fxtr scope. For the web platform use case, it would be stored in a namespace and pinned. Deferred.
- **Security of wheel sources**: Currently we trust PyPI URLs from pylock.toml. For production, we'd want to verify the index is trusted. Deferred.
- **Non-.py source files**: Currently we only collect .py files. Task modules might eventually need data files. Deferred.
- **run_uncached / ephemeral execution**: Running a task without caching. Deferred.
