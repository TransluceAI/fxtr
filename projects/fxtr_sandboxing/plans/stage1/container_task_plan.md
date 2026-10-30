# Plan: Container-Based Task Execution

Run tasks in OCI containers instead of WASM sandboxes. Designed to sit alongside the existing eryx-based system with a parallel set of entities and a shared communication protocol, eventually unified under type-driven dispatch in `run()`.

## Motivation

The eryx/WASM sandbox only supports pure Python. Containers can run anything: native extensions, system packages, other languages entirely. The tradeoff is heavier isolation (container startup vs. ~20ms sandbox creation) but much more flexibility.

## Container runtime

The system supports two OCI-compatible container runtimes:

- **Apple Container** (`container` CLI) — for macOS development. Uses lightweight VMs via Apple's Virtualization framework. This is the primary development target since we're prototyping on macOS.
- **Docker** (`docker` CLI) — for Linux/CI. Standard Docker daemon.

The `run()` implementation is configurable: both runtimes use the same OCI image format, same `-v` / `-e` / `--rm` / `--network` flags (Apple Container intentionally mirrors Docker's CLI). Key notes:

- `--network none` works on both (Apple Container sets `config.networks = []`, Docker uses the `none` network driver).
- Volume mounts via `-v host:container:ro` work on both (Apple Container uses virtiofs).
- Socket mounts via `-v` work on both. Apple Container also has `--publish-socket` for exposing container sockets to the host, but we mount from host→container so regular `-v` suffices.

## Design overview

```
Author time     Dockerfile + source code         (user builds image, pushes to registry)
                fxtr_container_task.toml          (declares image ref, source mounts, env)
                        │
                        ▼  prepare()
Prepare         read toml → snapshot source dirs → store entities
                        │
                        ▼
Bundle          DockerTaskBundle entity           (content-addressed)
                  - image: EntityRef[DockerImage]   (registry + digest)
                  - source_mounts: list[EntityRef[SourceMount]]
                  - cmd_override: list[str] | None
                  - env: dict[str, str]
                        │
                        ▼  run()
Execute         container run --network none → Unix socket communication
                (or docker run --network=none on Linux)
```

Key difference from the WASM system: we don't build the image. The user builds and pushes it themselves (to any OCI registry). We just reference it by digest. This is analogous to how `PyWheelPackage` references a remote wheel by URL + hash — we pin the artifact, not the build process.

## Config file: `fxtr_container_task.toml`

```toml
[image]
# OCI image reference. Together these form: registry@digest
registry = "localhost:5000/my-task"
digest = "sha256:abc123..."

# Optional. Must print "registry/repo@sha256:hex" to stdout.
# Used by the `fxtr_container_rebuild` CLI tool.
rebuild_command = "./rebuild.sh"

[[source_mount]]
path = "../shared_lib/src"     # relative to toml file
mount_at = "/app/shared"       # absolute path in container, mounted read-only

[[source_mount]]
path = "./config"
mount_at = "/app/config"

[env]
SOME_KEY = "some_value"

[run]
# Optional override of the image's CMD. Exec form (list of strings).
cmd_override = ["python", "-m", "fxtr_task_runner"]
```

## Entities

### DockerImage

```python
class DockerImage(DataclassEntity):
    digest: str      # "sha256:abc123..."
    registry: str    # "ghcr.io/myorg/my-task"
```

Content-addressed from `(digest, registry)`. The full OCI reference is `registry@digest`.

### SourceMount

```python
class SourceMount(DataclassEntity):
    files: dict[str, str]   # relative path → text content
    mount_path: str          # where to mount in the container
```

Content-addressed from file contents + mount path. Text-only for now (same constraint as `PySourcePackage`; binary support can be added later). Mounted read-only.

### DockerTaskBundle

```python
class DockerTaskBundle(DataclassEntity):
    image: EntityRef[DockerImage]
    source_mounts: list[EntityRef[SourceMount]]
    cmd_override: list[str] | None
    env: dict[str, str]
```

Content-addressed from all fields. This is the Docker analog to `TaskBundle`.

## prepare()

```python
def prepare_container_task(task_dir: Path) -> EntityRef[DockerTaskBundle]:
```

1. Read `fxtr_container_task.toml` from `task_dir`.
2. Create `DockerImage` entity from `[image]` section.
3. For each `[[source_mount]]`:
   - Resolve `path` relative to the toml file.
   - Walk the directory, read all files as text (error on binary for now).
   - Create `SourceMount(files=..., mount_path=...)`.
4. Read `[env]` and `[run].cmd_override`.
5. Store all entities, assemble `DockerTaskBundle`, store and return ref.

No Docker daemon needed at prepare time. This is a pure snapshot operation.

## run()

```python
async def run_container(bundle: DockerTaskBundle, ...) -> AsyncContextManager[TaskSession]:
```

1. Resolve `EntityRef`s: load `DockerImage` and all `SourceMount`s.
2. Create a tmpdir for the socket. Create a Unix domain socket listener at `tmpdir/task.sock`.
3. For each `SourceMount`: write files to a separate tmpdir, record `(host_path, mount_path)`.
4. Build and start the container via the configured runtime (`container run` or `docker run`):
   - `--rm` (auto-cleanup)
   - `--network none` (no network access)
   - `-v socket_tmpdir/task.sock:/run/fxtr/task.sock` (direct socket file mount — see note below)
   - `-v source_tmpdir:/mount/path:ro` (for each source mount)
   - `-e KEY=VALUE` (for each env var)
   - `registry@digest` (image reference)
   - `cmd_override` args if set
5. Accept a connection on the Unix socket.
6. Wrap the connection in a `TaskSession`.
7. On context exit: stop the container (`container stop` / `docker stop`), clean up tmpdirs.

The runtime selection is a simple configuration choice (e.g., `FXTR_CONTAINER_RUNTIME=container` or `docker`). Both CLIs share the same flags for the operations we need.

**Important: socket mount must be a direct file mount.** On Apple Container, Unix sockets inside virtiofs directory mounts (`-v dir:/dir`) return `ENOTSUP` on connect. Mounting the socket file directly (`-v /path/task.sock:/run/fxtr/task.sock`) works correctly. Source mount directories use regular directory mounts since they only contain regular files.

### Communication protocol

Unix socket at `/run/fxtr/task.sock` with length-prefixed framing:

- **Send**: encode message as UTF-8, write 4-byte big-endian uint32 length, write message bytes.
- **Receive**: read 4 bytes (length), read N bytes, decode as UTF-8.

This avoids newline-in-message issues and matches the existing `send(str)`/`receive(str)` contract. The `TaskSession` implementation uses `anyio` Unix socket streams directly (cleaner than the queue-based approach used for eryx).

### Differences from WASM run()

| Aspect | WASM (eryx) | Container |
|--------|-------------|-----------|
| Communication | eryx callbacks → queues → anyio | Unix socket → anyio streams |
| Isolation | WASM sandbox, no FS access | `--network none`, read-only mounts |
| Source delivery | Write to `site_packages/` dir | Bind mount from tmpdir |
| Startup | ~20ms (factory.create_sandbox) | ~1-2s (container run) |
| Capabilities | Pure Python only | Anything |
| Runtime | eryx (WASM) | Apple Container (macOS) or Docker (Linux) |

## Python helper library: `fxtr-task-runner`

A small Python package that users install in their Docker image to get the same `TaskContext` protocol as WASM tasks. Optional — users can also write their own socket client.

```
fxtr_task_runner/
  pyproject.toml
  fxtr_task_runner/
    __init__.py
    __main__.py     # python -m fxtr_task_runner
    context.py      # TaskContext wrapping the socket
    protocol.py     # length-prefixed message framing (shared with host)
```

`__main__.py` flow:
1. Connect to `/run/fxtr/task.sock`.
2. Discover the `fxtr.task` entry point from installed package metadata (`importlib.metadata.entry_points(group="fxtr.task")`).
3. Import the module, get the function.
4. Create a `TaskContext` wrapping the socket, call the entry point.

This matches the WASM pattern: the entry point is declared in the task package's `pyproject.toml`, and the runner discovers it at startup. The host doesn't need to know the entry point — it just starts the container.

A user's Dockerfile for a Python fxtr task would look like:

```dockerfile
FROM python:3.12-slim
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN pip install fxtr-task-runner
COPY src/ /app/src/
RUN pip install /app/src/my-task-package
CMD ["python", "-m", "fxtr_task_runner"]
```

For non-Python tasks, the user connects to the socket directly using whatever language they prefer. The protocol (4-byte length prefix + UTF-8 body) is trivial to implement.

## CLI: `fxtr_container_rebuild`

A convenience script that runs the user's `rebuild_command` and updates the toml.

```
fxtr_container_rebuild [--config fxtr_container_task.toml]
```

Flow:
1. Read `fxtr_container_task.toml`, extract `rebuild_command`.
2. Run the command, capture stdout.
3. Parse the last line as `registry/repo@sha256:hex`.
4. Update `[image].registry` and `[image].digest` in the toml file.

This is generic — it doesn't care how the image was built. The user's script just has to print the reference.

## Build convenience script (buildctl + registry caching)

For building images with layer caching, we use buildctl connected to a BuildKit daemon running inside a container. This works on both macOS (Apple Container) and Linux (Docker), and gives us access to BuildKit's `--cache-to type=registry` / `--cache-from type=registry` for fast incremental rebuilds.

### Why buildctl instead of `container build` / `docker buildx`?

Apple Container has a built-in builder (`container build`), but it doesn't expose `--cache-to type=registry`, `--cache-from type=registry`, or `--metadata-file`. Docker's `docker buildx` has all of these, but isn't available on macOS without Docker Desktop. Using buildctl directly works everywhere and gives us full control.

### Architecture

```
┌─────────────────────┐     ┌──────────────────────────────┐
│  Host (macOS/Linux) │     │  BuildKit container           │
│                     │     │  (moby/buildkit)              │
│  buildctl ──────────┼─────┼→ buildkitd (port 1234)       │
│                     │     │                               │
└─────────────────────┘     └──────────────────────────────┘
                                      │
                                      ▼ push
                            ┌──────────────────────┐
                            │  OCI Registry         │
                            │  (localhost:5000 or   │
                            │   remote registry)    │
                            └──────────────────────┘
```

### Sample rebuild.sh (macOS / Apple Container)

```bash
#!/bin/bash
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"
REPO="${REPO:-my-task}"
CACHE_REF="$REGISTRY/$REPO:buildcache"
IMAGE_REF="$REGISTRY/$REPO"
CONTEXT="${1:-.}"
METADATA_FILE=$(mktemp)

# Ensure buildkitd is running (reuse if already up)
if ! container inspect buildkitd &>/dev/null; then
  container run -d --name buildkitd -p 1234:1234 \
    moby/buildkit --addr tcp://0.0.0.0:1234
  # Wait for buildkitd to be ready
  sleep 2
fi

BUILDKIT_HOST="tcp://localhost:1234"

buildctl --addr "$BUILDKIT_HOST" build \
  --frontend dockerfile.v0 \
  --local context="$CONTEXT" \
  --local dockerfile="$CONTEXT" \
  --output "type=image,name=$IMAGE_REF:latest,push=true" \
  --export-cache "type=registry,ref=$CACHE_REF,mode=max" \
  --import-cache "type=registry,ref=$CACHE_REF" \
  --metadata-file "$METADATA_FILE"

DIGEST=$(jq -r '.["containerimage.digest"]' "$METADATA_FILE")
rm "$METADATA_FILE"

echo "$IMAGE_REF@$DIGEST"
```

On Linux/Docker, the same script works with `docker run` instead of `container run` for starting buildkitd. Or users can use `docker buildx` directly as their `rebuild_command` — the contract is just "print `registry/repo@sha256:hex` to stdout."

With `--export-cache type=registry` and `--import-cache type=registry`, BuildKit stores and retrieves layer caches from the registry itself. Rebuilds that only change the final COPY layer hit the cache for everything else. `mode=max` caches all intermediate layers, not just the final image layers.

## Demo / example

An example in `examples/container_task/` that demonstrates the full flow using a local registry:

```
examples/container_task/
  demo.py                    # Script that runs the full flow
  Dockerfile                 # Simple Python task image
  rebuild.sh                 # buildctl + local registry push script
  task_src/
    pyproject.toml           # With [project.entry-points."fxtr.task"]
    my_task/__init__.py      # Echo task, same pattern as hello_task
  fxtr_container_task.toml   # Points to localhost:5000/demo-task
```

`demo.py`:
1. Start a local registry (`container run -d --name fxtr-registry -p 5000:5000 registry:2`).
2. Build and push the image via `rebuild.sh` (starts buildkitd if needed, builds, pushes to local registry).
3. Update the toml with the resulting digest.
4. Run `prepare_container_task()` → `DockerTaskBundle`.
5. Run `run_container(bundle)` → exchange messages via `TaskSession`.
6. Demonstrate rebuild: modify source, rebuild (fast due to caching), show new bundle.
7. Clean up (stop registry + buildkitd containers).

This exercises the full pipeline: build → push → prepare → run → communicate.

## Implementation steps

- [x] 1. Add `DockerImage`, `SourceMount`, `DockerTaskBundle` entities to `entities.py`.
- [x] 2. Write `fxtr_container_task.toml` parser (new file, `container_config.py`).
- [x] 3. Write `prepare_container_task()` in a new `container_prepare.py`.
- [x] 4. Write `fxtr-task-runner` package at `fxtr_task_runner/` with protocol.py (length-prefixed framing), context.py (TaskContext), __main__.py (entry point discovery). Host imports protocol from this package.
- [x] 5. Write `run_container()` in a new `container_runner.py`. Reuses `TaskSession` interface but backed by Unix socket instead of queues. Configurable runtime (`container` or `docker`).
- [x] 6. Update CODE_OVERVIEW.md with container task concepts and new files.
- [x] 7. Write `fxtr-task-runner` package at `fxtr_task_runner/` (included in step 4).
- [x] 8. Write the `fxtr_container_rebuild` CLI tool (`container_rebuild.py`).
- [x] 9. Write the sample `rebuild.sh` script (buildctl + local buildkitd + registry caching).
- [x] 10. Write the demo example in `examples/container_task/` with local registry.
- [x] 11. Write tests for config, prepare, protocol, rebuild, and container runner (39 tests, all passing).
- [x] 12. Update CODE_OVERVIEW.md (final pass).

## Open questions / deferred

- **Binary files in SourceMount**: Currently text-only. Could extend to `dict[str, bytes]` later.
- **Image verification at prepare time**: Could optionally `container image inspect` / `docker manifest inspect` to verify the digest exists. Not strictly needed.
- **Unified `run()` dispatch**: Eventually `run()` should accept either `TaskBundle` or `DockerTaskBundle` and dispatch to the right runner. Deferred until both runners are working.
- **Resource limits**: Memory/CPU limits on the container. Not part of the task definition for now.
- **GPU access**: Deferred. Would require `--gpus` flag and probably network access for drivers.
- **Streaming / multiple connections**: Current design is one socket connection per task. Could extend to multiple connections or multiplexed channels later.
- **Container stdout/stderr capture**: Should we capture and surface container logs? Probably useful for debugging but not part of the protocol.
- **buildctl installation**: The demo assumes `buildctl` is on PATH. May need to document how to install it (e.g., `brew install buildkit` on macOS).
