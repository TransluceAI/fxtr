# basic_test_demo

Operator-facing setup for the smoke / regression demo. After
running through the steps below you can hand the agent over to
its own README (``agent_workdir/README.md``) and let it drive.

## Layout

```
basic_test_demo/
├── README.md                      this file (operator setup)
├── run_demo_workstation.py        standalone workstation process
└── agent_workdir/                 the dir the MCP plugin points at
    ├── README.md                  agent-facing (what to do once setup is done)
    ├── .fxtr_workstation.toml.example
    ├── preamble.py
    └── packages/
        ├── demo_pkg/              shared types + scenarios (src layout)
        │   ├── pyproject.toml
        │   └── src/demo_pkg/
        └── hello_service/         sandboxed-service sample (src layout)
            ├── pyproject.toml
            └── src/hello_service/
```

The workstation process and the agent workdir share
``demo_pkg`` (the workstation script adds
``packages/demo_pkg/src`` to its own ``sys.path``; the MCP
sandbox bridge adds the same dir to the script env's
``sys.path`` by walking ``packages/`` for ``pyproject.toml +
src/`` pairs). That single source of truth keeps the ``Note``
``fxtr_type`` registration identical on both ends so entities
round-trip cleanly.

## Setup

### 1. Install the plugin

The plugin lives at
``projects/fxtr_workstation_agent_plugin/claude_plugin/``. From
a Claude Code session, anywhere:

```
/plugin marketplace add /abs/path/to/projects/fxtr_workstation_agent_plugin/claude_plugin
/plugin install fxtr_workstation@fxtr-workstation
```

(For iteration during development you can also pass
``--plugin-dir`` instead; see the parent package's
[``README.md``](../../README.md) for that path and for any
plugin-install troubleshooting.)

### 2. Launch the demo workstation

In a dedicated terminal:

```bash
uv run python projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py
```

It prints the bound URL and the short_ids of the two
registrations (``delayed-echo`` service and ``demo-note``
entity), then parks until Ctrl-C. Keep it running for the
duration of your demo session.

### 3. Configure the agent workdir

Copy the toml example and update the URL to match what the
workstation printed in step 2:

```bash
cd projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir
cp .fxtr_workstation.toml.example .fxtr_workstation.toml
# edit workstation_url to match the line "workstation_url = ws://..." from step 2
```

### 4. Launch Claude Code with channels enabled

The plugin pushes ``notifications/claude/channel`` frames when
invoke results land in slots. The ``claude/channel`` capability
is gated behind a research-preview flag during install — start
Claude Code with the channel allowlist enabled so the agent
actually receives the frames (rather than having to poll for
results):

```bash
claude --dangerously-load-development-channels plugin:fxtr_workstation@fxtr-workstation
```

(If you used ``--plugin-dir`` in step 1, the allowlist token
is different — ``plugin:fxtr_workstation:workstation_mcp``.
See the parent [``README.md``](../../README.md) for both shapes
and the harmless display warning that comes with the
``--plugin-dir`` path.)

### 5. Hand over to the agent

``set_working_dir`` is an MCP tool, so you can't call it from
your prompt directly — instead, tell the agent to do it and
then read its own README. Something like:

> Use ``set_working_dir`` to point at
> ``/abs/path/to/projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/agent_workdir``,
> then read the README there and walk through the demo it
> describes.

The agent's README ([``agent_workdir/README.md``](agent_workdir/README.md))
covers connecting, opening a session, running each scenario,
and watching for channel notifications.

## Teardown

- ``close_session()`` from the agent (or just disconnect).
- Ctrl-C the workstation script.
- ``/plugin uninstall fxtr_workstation@fxtr-workstation`` if
  you want to remove the plugin afterwards.

## How it's also a test fixture

``test_mcp_subprocess.py`` (in the MCP plugin's ``tests/``)
drives the same ``agent_workdir/`` against an in-process
workstation. The scenarios are intentionally shared between
manual demos and the subprocess test so anything that breaks
the demo also breaks the test, and vice versa.
