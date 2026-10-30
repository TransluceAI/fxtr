# mcp_plugin_spike

A prototype Claude Code plugin that bundles a Python MCP server with two
facilities:

1. **Channel-based message scheduling.** A `schedule_message(delay_seconds,
   message)` tool delivers a self-directed follow-up after the delay via the
   [`claude/channel`](https://code.claude.com/docs/en/channels-reference)
   notification mechanism.
2. **Sandboxed Python script execution.** A `set_working_dir(path)` +
   `run_script(script)` pair runs user-supplied Python in an eryx WASM
   sandbox using `fxtr_sandboxing`'s direct launcher. The script can declare
   values back to the agent, schedule channel messages from inside the
   sandbox, call back into a host service over fxtr RPC, and `import` from a
   `packages/` subdir of the working dir.

See `CODE_OVERVIEW.md` for the design.

## Layout

```
mcp_plugin_spike/
├── README.md
├── CODE_OVERVIEW.md                   architecture / how things fit together
├── .claude-plugin/
│   └── marketplace.json               local marketplace catalog
├── sandbox_runtime/                   bootstrap stub task — its dep closure
│   ├── pyproject.toml                 (fxtr-sandbox-rpc-adapter[fxtr2-portable])
│   └── mcp_sandbox_runtime/           is what the SandboxFactory bakes in
├── sample_workdir/                    drives the subprocess test + manual smoke
│   ├── README.md
│   ├── preamble.py
│   └── packages/demo_pkg/
├── plans/phase1/                      design docs
└── plugin/                            the actual Claude Code plugin
    ├── .claude-plugin/plugin.json     manifest, declares the channel
    ├── .mcp.json                      stdio launcher (uv run ...)
    ├── skills/
    │   └── delayed-followup/SKILL.md  custom instructions
    └── scripts/                       Python packages (FastMCP server +
        ├── pyproject.toml             sandbox-side runner)
        ├── scheduler_mcp/             host-side: lifespan, tools, McpHostImpl
        ├── _fxtr_mcp_runner/          mounted into sandbox via volume:
        │                              entry-point + script_runner + protocols
        └── tests/                     pytest-anyio test suite
```

`mcp_plugin_spike/` doubles as the **marketplace root** (it contains
`.claude-plugin/marketplace.json`) and ships the plugin at `./plugin`.
The marketplace name is `mcp-spike`.

## Running it

There are two ways to load the plugin: ad-hoc via `--plugin-dir` (no install),
or by adding the local marketplace and installing into your user scope. Both
need `--dangerously-load-development-channels` to enable the channel during
the research preview.

### Option A: ad-hoc with `--plugin-dir`

No install — Claude Code loads the plugin directly from the directory for the
session only. The channel allowlist token is the fully qualified internal
server name `plugin:mcp-plugin-spike:scheduler`.

```bash
claude \
  --plugin-dir ./plugin \
  --dangerously-load-development-channels plugin:mcp-plugin-spike:scheduler
```

### Option B: install from the local marketplace

Add the marketplace once, then install (and later update / uninstall) like any
other plugin. This is closer to how end users would consume it.

```bash
# from a Claude Code session, anywhere:
/plugin marketplace add /Users/daniel/fxtr-worktrees/fxtr-bg2/spikes/mcp_plugin_spike
/plugin install mcp-plugin-spike@mcp-spike
```

(Or non-interactively: `claude plugin install mcp-plugin-spike@mcp-spike`
after the marketplace is added.)

Then start a session with the channel enabled. With the marketplace install,
the canonical `plugin:<name>@<marketplace>` token works cleanly (no display
warning):

```bash
claude --dangerously-load-development-channels plugin:mcp-plugin-spike@mcp-spike
```

To pick up edits to `plugin/...` after install, refresh the marketplace and
update the plugin:

```
/plugin marketplace update mcp-spike
/plugin update mcp-plugin-spike@mcp-spike
```

Or fall back to Option A while iterating — `--plugin-dir` always reads the
current files on disk.

### Heads-up: harmless display bug (Option A only)

When using `--plugin-dir` with the fully qualified server token, you'll see:

> server:plugin:mcp-plugin-spike:scheduler · no MCP server configured with that name

**Ignore it.** Notifications are still routed to the channel correctly. You
can confirm in `~/.claude/debug/<session-id>.txt`: the absence of
`Channel notifications skipped: ... not in --channels list` means the gate is
open. Option B (marketplace install + `plugin:...@mcp-spike`) avoids the
warning entirely, so prefer it once you've added the marketplace.

## Trying it out

### Channel-based scheduling

> remind me in 5 seconds to say hi

Claude should:

1. Call `schedule_message(delay_seconds=5, message="say hi to the user")`
2. Acknowledge that the reminder is queued
3. ~5s later, receive `<channel source="scheduler" delay_seconds="5">say hi to the user</channel>` and follow up

### Sandboxed script execution

`set_working_dir(path)` + `run_script(script)` execute Python in an eryx
WASM sandbox. The working dir must contain `preamble.py` (re-exec'd before
each script) and may have a `packages/` subdir of importable Python packages.

Inside the script:

```python
from _fxtr_mcp_builtin import declare_value, schedule_message, host_info
```

- `declare_value(key, value)` — JSON-shaped values returned to the agent.
- `schedule_message(delay_seconds, message)` — queues a delayed channel
  message (delivered after the script returns).
- `host_info()` — round-trips back to the host via fxtr RPC; returns a
  descriptive string. (Top-level `await` is supported.)

Try the sample workdir at `sample_workdir/` (see its README for sample
scripts). For example:

> Use `set_working_dir` to point at `<repo>/spikes/mcp_plugin_spike/sample_workdir`,
> then use `run_script` to declare answer=42 and schedule a 5-second reminder.

## Tests

```bash
cd plugin/scripts
uv run pytest tests/ -v
```

18 tests; ~70s total (factory bootstrap + sandbox spawn dominate). See
`CODE_OVERVIEW.md` for what each test covers.

## Debugging

- **`/mcp`** in the session — shows whether the `scheduler` server connected
- **`~/.claude/debug/<session-id>.txt`** — captures the MCP subprocess's stderr
  and channel-routing decisions. Grep for `scheduler` and `Channel`.
- **`claude --debug`** — verbose plugin/MCP loading on stderr.
- **Run the server by hand** to rule out the wrapper:
  ```bash
  uv run --directory plugin/scripts scheduler-mcp
  ```
  It should sit silently waiting for stdio JSON-RPC. Ctrl-C to exit.

## Notes about the implementation

Three things differ from a vanilla FastMCP server (see `server.py` docstring
for details):

1. **`ChannelFastMCP`** subclasses `FastMCP` to override `run_stdio_async` and
   pass `experimental_capabilities={"claude/channel": {}}` during
   initialization. FastMCP doesn't expose this directly.
2. **Lifespan task group**: deliveries need to outlive the originating tool
   call, so a long-lived `anyio.create_task_group()` is opened in a
   `lifespan` context manager and the tool calls `start_soon(...)` on it.
3. **Raw notification write**: `notifications/claude/channel` isn't in MCP's
   typed `ServerNotification` union, so the helper builds a `JSONRPCNotification`
   by hand and pushes it through the session's write stream.
