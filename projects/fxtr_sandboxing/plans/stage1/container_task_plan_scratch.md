# Container Task Plan — Scratch Notes

## Socket protocol details

Who connects to whom?
- Host creates a tmpdir, creates a Unix domain socket, starts listening
- Container has the tmpdir mounted at `/run/fxtr/`
- Container connects to `/run/fxtr/task.sock`
- Simple, standard pattern

Message framing:
- Can't use newline-delimited because messages may contain newlines
- Length-prefixed: 4-byte big-endian uint32 length, then UTF-8 encoded message body
- Matches existing send(str)/receive(str) semantics exactly
- Max message size: ~4GB (uint32), way more than needed

Connection lifecycle:
- Host starts listening before docker run
- Container connects on startup
- Either side can close the connection to signal shutdown
- Host detects container exit via docker wait or process exit

## fxtr_container_task.toml structure

```toml
[image]
registry = "ghcr.io/myorg/my-task"
digest = "sha256:abc123..."
rebuild_command = "./rebuild.sh"  # optional, must print registry/repo@sha256:hex to stdout

[[source_mount]]
path = "../shared_lib/src"     # relative to toml file location
mount_at = "/app/shared"       # absolute path inside container

[env]
SOME_CONFIG = "value"

[run]
cmd_override = ["python", "-m", "fxtr_task_runner"]  # optional
```

Questions:
- Should env be part of the bundle (content-addressed) or runtime config?
  → Part of bundle. It defines what the task needs. Runtime shouldn't vary.
- Should cmd_override be a list or a string?
  → List (exec form), consistent with Docker CMD.

## Entity design

DockerImage:
- digest: str (sha256:hex)
- registry: str (registry/repo, no tag)
- Together these form the OCI reference: registry@digest

SourceMount:
- files: dict[str, str] for text, but what about binary files?
  → For now, support text only (same as PySourcePackage). Can extend later.
  → Actually, we should probably support bytes too. Docker tasks are more general.
  → Let's use dict[str, bytes] and just read files as bytes.
  → Hmm, but then content-addressing hashing needs to handle bytes. fxtr entities
    handle this via mashumaro serialization. bytes should be fine.
  → Actually let's keep it simple and do str for now, matching PySourcePackage.
    We can add binary support later. Most source mounts will be code.
- mount_path: str (where to mount in container)

DockerTaskBundle:
- image: EntityRef[DockerImage]
- source_mounts: list[EntityRef[SourceMount]]
- cmd_override: list[str] | None
- env: dict[str, str]

## prepare() flow

1. Read fxtr_container_task.toml
2. Parse [image] → create DockerImage entity
3. For each [[source_mount]]:
   a. Resolve path relative to toml location
   b. Walk directory, read all files as text
   c. Create SourceMount entity with files dict + mount_path
4. Create DockerTaskBundle with refs to image + mounts + cmd_override + env
5. Store all entities, return EntityRef[DockerTaskBundle]

Should prepare() verify the image exists in the registry?
- Nice to have but not strictly necessary
- Could do `docker manifest inspect registry@digest` as a quick check
- Let's make it optional / best-effort. Skip for initial implementation.

## run() flow

1. Load DockerTaskBundle, resolve refs
2. Create tmpdir for socket
3. Create Unix socket at tmpdir/task.sock, start listening
4. For each SourceMount: write files to a tmpdir, note the host path + mount_path
5. Build docker run command:
   - docker run --rm
   - --network=none
   - -v tmpdir:/run/fxtr:rw  (socket dir, needs rw for socket connection)
   - -v source_tmpdir_1:/app/shared:ro (for each source mount)
   - -e KEY=VALUE (for each env var)
   - image_ref (registry@digest)
   - cmd_override args (if set)
6. Start docker run in background (subprocess)
7. Accept connection on socket
8. Return TaskSession wrapping the socket
9. On context exit: stop container (docker stop), clean up tmpdirs

TaskSession changes:
- Currently uses queue.Queue bridged to async via anyio.to_thread
- For socket: use anyio streams directly
  - anyio.connect_unix (client) or anyio.create_unix_listener (server)
  - Read: read 4 bytes (length), read N bytes (message), decode utf-8
  - Write: encode utf-8, write 4 bytes (length), write N bytes
- Much cleaner than the queue-based approach actually

## fxtr-task-runner Python helper

Small package. Structure:
```
fxtr_task_runner/
  pyproject.toml
  fxtr_task_runner/
    __init__.py
    __main__.py    # entry point for python -m fxtr_task_runner
    context.py     # TaskContext wrapping socket
    protocol.py    # length-prefixed message framing
```

__main__.py:
1. Connect to /run/fxtr/task.sock
2. Read FXTR_ENTRY_POINT env var (module:function format)
   - Or: scan installed packages for [project.entry-points."fxtr.task"]
   - Env var is simpler and more explicit. Let's use that.
   - Actually: the entry_point info is in the DockerTaskBundle. The host sets
     FXTR_ENTRY_POINT env var when launching the container.
   - Wait, but fxtr_entry_point was removed from the bundle...
   - The user's Dockerfile CMD would be `python -m fxtr_task_runner`
   - The user's pyproject.toml would have [project.entry-points."fxtr.task"]
   - fxtr_task_runner discovers it from the installed package metadata
   - This is cleaner — no env var needed, matches the WASM pattern
3. Create TaskContext wrapping the socket
4. Import module, get function, call it with context

Actually, on reflection: the fxtr_task_runner should discover the entry point
from installed package metadata (pkg_resources / importlib.metadata), scanning
for the "fxtr.task" entry point group. This matches how the WASM system works
where the entry point is declared in pyproject.toml.

But wait — the host also needs to know the entry point is set up correctly.
In the WASM system, prepare() reads the entry point from pyproject.toml.
For Docker, the entry point is "baked into" the image. The host doesn't need
to know — it just starts the container and the container figures it out.

Hmm, but then the DockerTaskBundle doesn't know the fxtr entry point at all.
Is that OK? I think so — the bundle says "run this image with these mounts
and env vars." What happens inside is the image's business.

## Buildx convenience script

Goal: make it easy to build + push + update the toml.

Script: `fxtr_container_build` (or a subcommand of some CLI)

Usage: `fxtr_container_build [--config fxtr_container_task.toml] [--context .]`

Flow:
1. Read fxtr_container_task.toml for registry info
2. Run: docker buildx build \
     --push \
     --cache-to type=registry,ref=<registry>/cache:buildcache \
     --cache-from type=registry,ref=<registry>/cache:buildcache \
     --metadata-file /tmp/metadata.json \
     -t <registry>:latest \
     <context>
3. Read /tmp/metadata.json → extract containerimage.digest
4. Format: <registry>@<digest>
5. Print to stdout
6. Update fxtr_container_task.toml with new digest

Wait, but the rebuild_command convention is that it prints the reference.
So this script IS the rebuild_command. And a separate CLI tool runs the
rebuild_command and splices the output into the toml.

Let me reconsider the flow:

Option A: Two scripts
- `fxtr_buildx_push.sh` — the rebuild_command. Builds, pushes, prints ref.
- `fxtr_rebuild` CLI — reads toml, runs rebuild_command, updates toml.

Option B: One script that does everything
- `fxtr_container_build` — reads toml, builds, pushes, updates toml.

I think Option A is cleaner. The rebuild_command is pluggable (any script),
and there's a generic CLI that runs it and updates the toml.

For the demo, we provide a sample rebuild script that uses buildx with
registry caching.

## Demo script

1. Start a local registry: docker run -d -p 5000:5000 --name fxtr-registry registry:2
2. Create a simple Python task:
   - Dockerfile: FROM python:3.12-slim, COPY source, install fxtr-task-runner, CMD
   - pyproject.toml with fxtr.task entry point
   - Simple echo task
3. Build and push with buildx + cache-to/cache-from local registry
4. Create fxtr_container_task.toml pointing to localhost:5000/demo-task@sha256:...
5. Run prepare() → DockerTaskBundle
6. Run run() → exchange messages
7. Modify source slightly, rebuild (should be fast due to caching)
8. Show that new prepare() produces different bundle (source mount changed)
9. Clean up: stop registry, remove container

Actually the demo is probably a test or example script, not a formal test.
Let's put it in examples/.
