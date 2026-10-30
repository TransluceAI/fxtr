# Stage 5 — Claude plugin packaging + integration smoke

Goal: the agent can load this MCP as a Claude Code plugin, set a
working directory, and walk through the end-to-end flow described in
the part-2 sketch (open session → run scripts → close session). End
of stage 5 = agent-usable v1.

## Scope

- `projects/fxtr_workstation_agent_plugin/claude_plugin/` directory:
  - `.claude-plugin/plugin.json` — manifest; declares
    `channels: [{server: "workstation_mcp"}]` for
    `notifications/claude/channel` use.
  - `.claude-plugin/marketplace.json` — local marketplace catalog,
    same shape as the spike. Marketplace name `fxtr-workstation`,
    plugin name `fxtr-workstation`.
  - `.mcp.json` — `mcpServers.workstation_mcp.command = "uv"`,
    `args = ["run", "--directory",
    "${CLAUDE_PLUGIN_ROOT}/../fxtr_workstation_mcp",
    "fxtr-workstation-mcp"]`. (Console-script entry exposed in the
    package's `pyproject.toml`.)
  - `skills/use_fxtr_workstation/SKILL.md` — stub skill instructions
    explaining the working-dir contract, `connect`, `open_session`,
    `run_script`, `retrieve_result`, and the channel notification
    convention. Marked clearly as v1; deeper guidance defers to
    later.
- `fxtr_workstation_mcp/server.py` — add the `ChannelFastMCP`
  override (port from spike) and wire the
  `experimental_capabilities={"claude/channel": {}}` declaration.
- README.md (package root) covering:
  - What the plugin does.
  - The `.fxtr_workstation.toml` schema (`workstation_url`,
    `active_session`).
  - Working-dir layout (`preamble.py`, optional `packages/`).
  - Loading via the local marketplace install path (the spike
    documented two paths but for v1 we standardize on Option B —
    `/plugin marketplace add` + `/plugin install`. The
    `--plugin-dir` ad-hoc path can be a one-line "and you can also
    iterate this way" footnote without the channel-token caveat
    the spike had to spell out).
  - The agent-facing tool list and a brief sample workflow.
  - Smoke-test recipe (boot a `WorkstationServer` via the existing
    debug demo, then drive the MCP).
- `sample_workdir/` under the package root — minimal
  `preamble.py` (perhaps importing a `demo_pkg` similar to the
  spike's), `.fxtr_workstation.toml.example`, a README walking
  through 3-4 scripts (export, lookup, schedule_invoke + retrieve).

## Tests (stage 5)

- `test_mcp_subprocess.py` — spawns the MCP via stdio (using
  `mcp.client.stdio`) and walks through:
  - `set_working_dir(<sample_workdir>)`.
  - `connect()`.
  - `open_session()`.
  - `run_script("...")` for a couple of scripts (the same ones the
    sample_workdir README documents).
  - `close_session()`.
  - All against a real in-process `WorkstationServer` (subprocess
    or child task — pick whichever's least flaky; the spike uses
    in-process).
- `test_plugin_manifest_schema.py` — quick parse-test that
  `plugin.json` / `marketplace.json` / `.mcp.json` match the
  shapes Claude Code expects (mirror of the spike's
  marketplace-related sanity check).
- Verify the channel notification fires for an invoke result via
  the subprocess test (collect notifications from the MCP session
  during the test).

## Implementation steps

- [x] Channel capability wired into the lowlevel server's
      ``create_initialization_options`` via a one-line monkey-
      patch in ``server.py`` (replacing the original
      ``ChannelFastMCP`` subclass plan; covers all transports
      without per-transport overrides).
- [x] Console-script entry (`fxtr-workstation-mcp`) in
      `fxtr_workstation_mcp/pyproject.toml` (already present).
- [x] `claude_plugin/` directory tree, including the
      ``channels`` declaration in ``plugin.json``.
- [x] Flesh out the ``SKILL.md`` to cover the full stage-4 agent
      surface (name-based lookup, schedule_invoke / retrieve,
      channel notifications).
- [x] Build out ``examples/basic_test_demo/`` (renamed from
      ``sample_workdir/`` — see "Wrap-up notes"). Contains
      ``run_demo_workstation.py`` (standalone) + ``agent_workdir/``
      with preamble, demo_pkg (Note + scenarios), toml example,
      and READMEs.
- [x] Package-root ``README.md`` covering the toml schema,
      workdir layout, marketplace install path, and demo recipe.
- [x] ``test_mcp_subprocess.py`` — drives the MCP via
      ``mcp.client.stdio`` against the basic_test_demo
      agent_workdir + an in-process workstation. Covers
      schedule_invoke, channel-notification frame capture,
      retrieve_result.
- [x] ``test_plugin_manifest_schema.py`` — parse + shape-check
      ``plugin.json`` / ``marketplace.json`` / ``.mcp.json`` /
      ``SKILL.md``.
- [x] Update `CODE_OVERVIEW.md` (final).

## Open questions

- The `.mcp.json` `--directory` path resolution: Claude expands
  `${CLAUDE_PLUGIN_ROOT}` to the plugin's directory (i.e.
  `claude_plugin/`). The MCP server lives a sibling level up under
  `fxtr_workstation_mcp/`, so `${CLAUDE_PLUGIN_ROOT}/..` reaches
  the package root. Verify on first end-to-end load — the spike
  uses a similar pattern with `${CLAUDE_PLUGIN_ROOT}/scripts` for
  comparison.
- Skill content: v1 keeps it minimal. Later we'll likely include
  sample patterns for "schedule a couple of invokes and retrieve
  in a follow-up turn", "use the channel notifications", "use
  preamble for re-usable helpers".

Resolved: README focuses on Option B (marketplace install); ad-hoc
`--plugin-dir` loading is a one-line footnote rather than a
co-equal path.

## Wrap-up notes

After stage 5, the v1 deliverable is:

- A loadable Claude Code plugin.
- An MCP server that maintains a persistent workstation session.
- An agent affordance for running sandboxed Python that drives the
  workstation declaratively (export / invoke / retrieve / forget).
- Channel notifications when async results land.
- Integration with `fxtr_workstation`'s debug UI: anything the
  agent does is observable there.

Things explicitly deferred (per the part-2 sketch):

- Typed `ServiceRefStub[T]` + typed dispatch.
- Callback support.
- Bundling local packages as workstation-shipped sandbox images.

Each of these would be a self-contained follow-up part.

## Wrinkles that came up during implementation

- Stage 5 originally planned ``sample_workdir/`` under the plugin
  package root, with a single ``demo workstation`` snippet inline
  in the README. We landed on a slightly different layout that
  the user proposed mid-stream: a top-level ``examples/`` dir
  with one example per subdir
  (``examples/basic_test_demo/agent_workdir/`` + a sibling
  ``run_demo_workstation.py``). Keeping the demo workstation
  *outside* the agent_workdir matches the mental model that
  agent_workdir is what the MCP points at, not where the human
  operator's setup code lives.
- The agent needed name-based discovery of stable workstation
  services. We added a **prerequisite** workstation-side change
  before tackling stage 5 proper: a new
  ``WorkstationServiceProto.lookup_short_id_by_external_name``
  method + cross-namespace uniqueness enforcement between
  ``register_service`` and ``copy_entity``. With that landed,
  the MCP gained two thin sandbox builtins —
  ``lookup_service_by_name`` and ``lookup_entity_by_name`` —
  which the demo_pkg's scenarios use. See the workstation's
  CODE_OVERVIEW for the namespace invariant.
- ``ChannelFastMCP`` (the planned FastMCP subclass for
  declaring the channels capability) turned out to be
  unnecessary: a one-line monkey-patch of
  ``_mcp_server.create_initialization_options`` covers all
  transports without a per-transport ``run_*_async`` override.
- ``notifications/claude/channel`` isn't in the typed
  ``ServerNotification`` union, so ``mcp.ClientSession`` would
  silently drop the frames at validation time. The subprocess
  test uses a small raw JSON-RPC layer over ``stdio_client``'s
  streams to capture them. Worth being aware of if anything
  else ever wants to consume channel frames from a typed
  client.
