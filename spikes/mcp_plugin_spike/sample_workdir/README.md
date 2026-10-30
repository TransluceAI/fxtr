# Sample working directory

A minimal `<working_dir>` for the `set_working_dir` + `run_script` MCP
tools. Used by the subprocess test (`tests/test_mcp_subprocess.py`)
and as the suggested target for manual smoke-testing the plugin from
a real Claude Code session.

## Layout

```
sample_workdir/
  preamble.py                  re-exec'd before each script
  packages/
    demo_pkg/__init__.py       importable as `demo_pkg`
```

`preamble.py` imports `GREETING` and `doubler` from `demo_pkg` and
defines `ANSWER = 42`. Anything bound there is in scope for the
script.

## Sample scripts

The script body is what you'd pass as the `script` argument to
`run_script`. It can `from _fxtr_mcp_builtin import declare_value,
schedule_message, host_info` (top-level await is allowed).

`run_script` returns a plain-text summary of the form:

```
Declared values:
  <key> = <python-repr>
  ...
Scheduled N follow-up message(s):
  +<delay>s: '<message preview>'
```

(or `Declared values: (none)` / `Scheduled follow-ups: (none)` for the
empty cases).

### 1. Round-trip a value

```python
from _fxtr_mcp_builtin import declare_value
declare_value("answer", ANSWER)
declare_value("doubled", doubler(ANSWER))
```

Expected:

```
Declared values:
  answer = 42
  doubled = 84
Scheduled follow-ups: (none)
```

### 2. Schedule a message

```python
from _fxtr_mcp_builtin import schedule_message
schedule_message(5.0, "remember to follow up on the ANSWER")
```

Expected:

```
Declared values: (none)
Scheduled 1 follow-up message(s):
  +5.0s: 'remember to follow up on the ANSWER'
```

Roughly 5 seconds later, a `<channel source="scheduler">` notification
fires and Claude sees it as a self-directed follow-up.

### 3. Call back to the host

```python
from _fxtr_mcp_builtin import declare_value, host_info
info = await host_info()
declare_value("host", info)
```

Expected `host` value contains "mcp_plugin_spike, scripts_dir=...".

### 4. Use a local package

```python
from _fxtr_mcp_builtin import declare_value
declare_value("greeting", GREETING)
```

Expected: `greeting = 'hello from demo_pkg'`.

## Manual smoke test

After the subprocess test passes, drive the plugin from a real Claude
Code session:

```bash
claude \
  --plugin-dir ./plugin \
  --dangerously-load-development-channels plugin:mcp-plugin-spike:scheduler
```

Then in the session:

> Use `set_working_dir` to point at
> `/Users/.../spikes/mcp_plugin_spike/sample_workdir`, then use
> `run_script` to declare answer=42 and schedule a 5-second reminder.

Verify (a) the tool returns the expected JSON, (b) ~5 s later the
channel notification fires and Claude follows up.
