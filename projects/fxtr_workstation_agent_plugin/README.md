# fxtr_workstation_agent_plugin

Claude Code plugin that gives an LLM agent a sandboxed Python
environment for driving a persistent
[fxtr workstation](../fxtr_workstation/) session.

See [`CODE_OVERVIEW.md`](./CODE_OVERVIEW.md) for the package
layout. The plan documents in
[`projects/fxtr_workstation/plans/phase0/part2_ws_mcp/`](../fxtr_workstation/plans/phase0/part2_ws_mcp/)
walk through the design.

## What it gives the agent

The plugin installs an MCP server (`workstation_mcp`) plus a
skill that explains how to use it. The tool surface:

- `set_working_dir(path)`, `connect()`, `open_session()`,
  `close_session()` — session lifecycle.
- `run_script(source)` — execute Python in a sandbox. The
  sandbox can `lookup_entity` / `lookup_service_by_name` /
  `lookup_entity_by_name`, queue `export_entity` /
  `export_service` / `forget`, dispatch services via
  `schedule_invoke`, and read async results via
  `retrieve_result`. See
  [`skills/use_fxtr_workstation/SKILL.md`](claude_plugin/skills/use_fxtr_workstation/SKILL.md)
  for the full agent surface.

Asynchronous slot promotions (invoke results) surface to the
agent as `notifications/claude/channel` frames so they can act
on a result the moment it lands.

## Working dir contract

The agent's working directory must contain:

```
.fxtr_workstation.toml          # workstation_url + (auto-managed) active_session
preamble.py                     # re-executed in the sandbox before each run_script
packages/                       # optional; subdirs become sys.path entries
    your_pkg/...
```

Minimal toml:

```toml
workstation_url = "ws://127.0.0.1:8123"
# active_session = "<uuid>"     # written by open_session, cleared by close_session
```

`preamble.py` runs before every `run_script` call so the script
body can stay terse — imports + helpers go in the preamble.
Each `packages/<name>/` subdir that follows the **src layout**
(``pyproject.toml`` + ``src/<name>/``) is mounted into the
sandbox and its `src/` is added to `sys.path`, so the package
is importable from the script body. Subdirs with a
`pyproject.toml` but no `src/` get a warning prepended to the
`run_script` response (move the modules under `src/<name>/` to
silence it). See the workstation skill for the full convention.

## Try it (basic_test_demo)

[`examples/basic_test_demo/`](examples/basic_test_demo/) bundles
a standalone workstation script + a sample agent workdir. End to
end:

```bash
# Terminal 1: the workstation (registers a delayed-echo service
# and a demo-note entity, then waits).
uv run python projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py
```

```bash
# Terminal 2: set up the agent workdir.
cd projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir
cp .fxtr_workstation.toml.example .fxtr_workstation.toml
# edit workstation_url to match the URL printed by the workstation
```

Then in Claude Code (with the plugin installed — see below):

- `set_working_dir("/abs/path/to/agent_workdir")`
- `connect()` → `open_session()`
- `run_script("await scenarios.schedule_basic_echo()")`
- Wait for the channel notification.
- `run_script("print(await scenarios.retrieve_basic_echo())")` → `"hello"`

See [`examples/basic_test_demo/agent_workdir/README.md`](examples/basic_test_demo/agent_workdir/README.md)
for the full scenario list.

## Loading the plugin

The plugin is shipped as a local marketplace under
[`claude_plugin/`](claude_plugin/). There are two ways to load
it: ad-hoc via `--plugin-dir` (no install), or by adding the
local marketplace and installing. **Both need
`--dangerously-load-development-channels` to enable the
`claude/channel` capability during its research-preview gate.**
Without that flag, invoke-result channel notifications will be
silently dropped — the agent has to fall back to polling
`retrieve_result`.

### Option B (recommended): install from the local marketplace

Add the marketplace once, then install like any other plugin.
The marketplace catalog (`marketplace.json`) is named
`fxtr-workstation` and exposes one plugin, also named
`fxtr_workstation`.

```bash
# inside a Claude Code session, anywhere:
/plugin marketplace add /abs/path/to/projects/fxtr_workstation_agent_plugin/claude_plugin
/plugin install fxtr_workstation@fxtr-workstation
```

Then start a session with the channel enabled. The canonical
`plugin:<plugin-name>@<marketplace-name>` allowlist token works
cleanly with the marketplace install:

```bash
claude --dangerously-load-development-channels plugin:fxtr_workstation@fxtr-workstation
```

To pick up edits to the plugin source after install, refresh
the marketplace and update the plugin:

```
/plugin marketplace update fxtr-workstation
/plugin update fxtr_workstation@fxtr-workstation
```

### Option A: ad-hoc with `--plugin-dir`

No install — Claude Code loads the plugin directly from the
directory for the session only. Faster iteration loop since
each launch reads the current files on disk.

```bash
claude \
  --plugin-dir /abs/path/to/projects/fxtr_workstation_agent_plugin/claude_plugin \
  --dangerously-load-development-channels plugin:fxtr_workstation:workstation_mcp
```

Note the allowlist token shape differs from Option B: it's
`plugin:<plugin-name>:<server-name>` (the MCP server name as
declared in `.mcp.json` is `workstation_mcp`).

You may see a startup warning like
``server:plugin:fxtr_workstation:workstation_mcp · no MCP server
configured with that name`` — this is a known display-only bug
with `--plugin-dir` + the fully-qualified token. Notifications
are still routed correctly; you can confirm by grepping
`~/.claude/debug/<session-id>.txt` for the absence of
``Channel notifications skipped``. Option B avoids the warning
entirely.

## Running the tests

```bash
cd fxtr_workstation_mcp
uv run pytest
```

The suite includes unit tests for the connection / event / mirror
logic plus an end-to-end subprocess test
(`test_mcp_subprocess.py`) that drives the MCP via stdio against
the `examples/basic_test_demo/` sample.

## Debug log

The `.mcp.json` launcher appends the MCP's stderr to
`/tmp/fxtr_workstation_mcp_debug.log` with a session marker each
time Claude Code spawns the server. If the MCP crashes mid-session
(Claude Code reports `MCP error -32000: Connection closed`), the
real traceback lands there. Tail it during a session to watch
warnings and reconnect attempts live.

## Smoke recipe (without Claude Code)

For development without launching a full Claude Code session:

1. Start the demo workstation as above.
2. Spawn the MCP via `uv run --directory fxtr_workstation_mcp fxtr-workstation-mcp`
   and drive it with an `mcp.client.stdio` script (or just run the
   `test_mcp_subprocess.py` test, which does exactly this).
