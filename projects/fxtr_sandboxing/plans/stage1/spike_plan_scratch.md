# Spike plan scratch notes

## Key design decisions to think through

### What goes in the TaskBundle entity?

The entity needs to fully describe "what to run" in a content-addressable way. Contents:
- Source files: the actual Python source code, keyed by relative path
- pylock.toml: the full contents (or parsed form) — this pins every dep to exact version + hash
- Entry point: e.g. "main:run" or "main.py"
- Python version constraint (from pylock.toml's requires-python)

The source files are small (kilobytes), so inlining them is fine. The pylock.toml is also small.
Wheel files are NOT in the entity — they're referenced by URL+hash in the pylock.toml.

### How does the entity relate to fxtr2?

Options:
1. TaskBundle as a DataclassEntity — source files as Child entities, pylock as a field
2. TaskBundle as a simpler structure — just store as a single blob/dict

For the spike, option 1 is cleaner. Source files could be individual ChildRef[SourceFile] entities
or just a dict[str, str] field (path -> content). The dict is simpler and the files are small.

Going with: a single DataclassEntity with dict fields. No need for child entities in the spike.

### Wheel fetching and caching

At runtime we need to:
1. Parse pylock.toml from the bundle
2. For each package, find a compatible wheel (py3-none-any for pure Python)
3. Check local cache (keyed by hash)
4. If not cached, download from URL, verify hash, cache
5. Provide all wheels to eryx SandboxFactory

Cache location: could be a simple directory, e.g. ~/.cache/fxtr_sandboxing/wheels/
Keyed by sha256 hash of the wheel.

### Communication protocol

Simple for the spike:
- Host sets up two callbacks: `send_message(msg: str)` and `receive_message() -> str`
- Actually, it's the opposite direction: the sandbox calls these to communicate with the host
- `send_to_host(data: str)` — sandbox sends a message to the host
- `receive_from_host() -> str` — sandbox blocks waiting for the next message from the host

On the host side, we expose an async interface:
- `send(data: str)` — host sends a message to the sandbox
- `receive() -> str` — host receives the next message from the sandbox

This is a simple bidirectional channel. Messages are strings (JSON-encoded by convention).

The sandbox's entry point function would look like:
```python
async def run():
    msg = await receive_from_host()
    # process msg
    await send_to_host(result)
```

### Eryx session vs sandbox

For a task module that processes a single request, a Sandbox with one execute() is fine.
For a long-running task that handles multiple messages, we need a Session.
For the spike, probably start with Sandbox (single execution) and move to Session if needed.

Actually, for bidirectional communication, we need the sandbox to be running continuously and
calling callbacks. A single execute() that runs an async loop with receive/send callbacks
should work. The execution doesn't return until the sandbox is done.

### Build step details

`prepare()`:
1. Validate the task directory (has pyproject.toml, has entry point file)
2. Run `uv lock` if no uv.lock exists
3. Run `uv export --format pylock.toml` to get the lockfile
4. Read all .py files from the directory
5. Read entry point from [tool.fxtr-task] in pyproject.toml
6. Construct TaskBundle entity

### What about the entrypoint format?

Options:
- "main.py" — execute a script
- "main:run" — call a specific async function

For the spike, "main:run" is cleaner — we can generate a wrapper that imports and calls it.
But "main.py" is simpler — just execute the file.

Let's support both: if there's a ":" it's module:function, otherwise it's a script to execute.
For the spike, start with script execution (just execute main.py).

### How does the sandbox access its own source files?

Eryx has VFS — we can mount the source files into the sandbox's virtual filesystem.
The sandbox sees its code at e.g. /task/main.py and can import from /task/.

Actually, looking at the eryx API more carefully: we can use SandboxFactory with packages
for wheels, and then execute Python code that includes our source. The simplest approach:
- Install wheels via SandboxFactory packages
- Use VFS to mount source files
- Execute the entry point script

### Platform tag filtering

For WASM/eryx, we want `py3-none-any` wheels (pure Python, universal).
When parsing pylock.toml, filter each package's wheels to find one matching this tag.
The wheel filename encodes the tags: {name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl

### Error cases to handle in the spike

- Package has no py3-none-any wheel (error: native extension required)
- Wheel download fails (network error)
- Hash verification fails (corrupted download)
- Entry point file missing
- Python syntax error in task code
- Task code raises an exception
