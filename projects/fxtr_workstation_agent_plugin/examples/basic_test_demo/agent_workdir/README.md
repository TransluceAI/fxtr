# basic_test_demo agent_workdir

You're in the agent's working directory for the
``basic_test_demo`` example. **Setup is already done**: the
demo workstation is running, the MCP plugin is installed and
pointed here, and the toml is configured. Your job is to drive
the demo through the plugin's tools and verify the end-to-end
flow works.

(If something looks like it isn't set up, point the user at
[`../README.md`](../README.md).)

## What's in this directory

- ``preamble.py`` — re-executed in the sandbox before every
  ``run_script`` call. Already imports ``Note``, ``scenarios``,
  and the stable workstation names from ``demo_pkg``, so
  scripts you write can use them directly without import
  boilerplate.
- ``packages/demo_pkg/`` — shared package, src layout
  (``packages/demo_pkg/src/demo_pkg/``):
  - ``Note`` — a tiny ``DataclassEntity`` used in the
    entity-arg scenario.
  - ``scenarios`` — async helpers, one per scenario step.
    Each function is a self-contained body that uses the
    ``_fxtr_mcp_builtin`` closures (``schedule_invoke``,
    ``retrieve_result``, ``lookup_*``); you call them from
    inside ``run_script`` one-liners.
- ``packages/hello_service/`` — a sandboxed-service sample,
  src layout. Defines ``HelloConfig(name)`` +
  ``HelloProto.greet()``. The flake builds it as
  ``fxtr.services.hello_service``; you spawn it **by git
  reference** (see the spawn walkthrough below).
- ``packages/demo_data/`` — a host-side package exposing a
  ``DEMO_DATA`` (``DemoDataEntity``) object. The flake
  materializes it as the DATA entity ``fxtr.entities.demo_data``
  (via ``mkEntityFromPython``); ``retrieve_nix_entity("demo_data")``
  pulls it into a script.
- ``renderers/demo_data/`` — a TSX renderer module for
  ``DemoDataEntity`` (``index.tsx``, the four shell dispatch keys).
  The flake compiles it via ``esbuildRenderer`` and routes it from
  the UI preset below — assembled by ``lib.mkUiPreset`` (the
  per-renderer-declaration transpose). No ``fxtr_renderer.json``: the
  declaration is the ``mkUiPreset`` entry in the flake.
- ``flake.nix`` — declares ``fxtr.services.hello_service`` (CODE,
  spawned by committed git ref), ``fxtr.entities.demo_data`` (DATA,
  built on demand from the live workdir), and
  ``fxtr.uiPresets.demo_preset`` (CODE — a renderer preset
  pushed to a nix dispatcher by committed git ref; see "UI presets"
  below).
- ``.fxtr_workstation.toml`` — workstation URL + (managed by
  the plugin) active session UUID.

Both packages above use **src layout** (a ``pyproject.toml`` at
``packages/<name>/`` and the code under ``packages/<name>/src/<name>/``);
that's the layout the workstation MCP's sandbox bridge expects.
When you add your own packages here, follow the same shape —
otherwise you'll get a warning at the top of each ``run_script``
response and the package won't be importable. The same src
layout is what the flake's ``mkWasmService`` builds a
``fxtr.services.<name>`` from. See the workstation skill for the
full convention.

## Workstation registrations available to you

The demo workstation has registered several stable things you can
discover by name:

| External name | Kind | Notes |
|---|---|---|
| ``delayed-echo`` | service | ``async echo(value, delay) -> value`` — echoes any wire-shape value back after sleeping ``delay`` seconds. |
| ``demo-note`` | entity | A ``Note`` registered at workstation startup. |
| ``callback-tester`` | service | ``register_callback(cb)`` + ``fire_notify(payload, delay)`` — stashes a callback ref and fires fire-and-forget notifications back to it after a delay. |
| ``sandbox-spawner`` | service | A ``SandboxSpawnerProto`` registered via the workstation's ``external_services`` (i.e. as a fundamental builtin, not a peer registration). Call ``spawn(defn)`` with a ``NixWasmServiceDefn(git_commit=current_commit(), flake_attr="fxtr.services.<name>")`` to build + launch a sandboxed service from your committed flake. |
| ``internet-authorizer`` | service | A ``NetworkHandleAuthorizerProto`` (HTTPS passthrough, no host whitelist). Bridge into a sandboxed service's ``connect`` config so the sandboxed code can call ``fxtr_sandbox_rpc_adapter.get_client(authorizer)`` for HTTP access to arbitrary public hosts. |

The exact short_ids change between workstation runs; resolve
them by name via ``lookup_service_by_name`` /
``lookup_entity_by_name``.

You can try the spawner end-to-end with the bundled
``packages/hello_service/`` package — see the spawn walkthrough
below. To give a spawned service internet access, look up
``internet-authorizer`` and pass it as part of the spawned
service's ``connect`` config.

## How to try the demo

You'll want to use ``connect``, ``open_session``, and a few
``run_script`` calls. ``set_working_dir`` should already point
here.

### 1. Open the session

```
connect()
open_session()
```

### 2. Run each scenario

Each scenario is split into a ``schedule_*`` and a
``retrieve_*`` half (the result is async, so two scripts with a
wait in between). Watch for the
``<channel source="workstation_mcp" slot_key="..." kind="invoke_result">…</channel>``
frame between the two halves — that's how you know the result
landed.

**Scenario 1: scalar echo.**

```python
# run_script
await scenarios.schedule_basic_echo()
```

Wait for ``<channel source="workstation_mcp" slot_key="basic_echo" kind="invoke_result">…</channel>``.

```python
# run_script
result = await scenarios.retrieve_basic_echo()
assert result == "hello", f"got {result!r}"
```

**Scenario 2: round-trip an entity arg.**

```python
# run_script
await scenarios.schedule_entity_arg_echo(title="passed-in", body="round-trip")
```

Wait for ``<channel ... slot_key="entity_arg_echo" kind="invoke_result">…</channel>``.

```python
# run_script
note = await scenarios.retrieve_entity_arg_echo()
assert note.title == "passed-in" and note.body == "round-trip", note
```

**Scenario 3: name-based entity lookup.** No scheduling — just
a read-side path.

```python
# run_script
note = await scenarios.fetch_demo_note()
assert note.title == "demo-note", note
```

**Scenario 4: retrieve a nix DATA entity.** The flake declares
``fxtr.entities.demo_data``; ``retrieve_nix_entity`` builds its
layer on demand and bridges the entity into the script. (First
call cold-builds the layer — it can take a few seconds.)

```python
# run_script
from _fxtr_mcp_builtin import retrieve_nix_entity
from demo_data import DemoDataEntity
from fxtr2.data.entity_ref_scope import EntityRef

fid = await retrieve_nix_entity("demo_data")
ent = await EntityRef(fid, DemoDataEntity).load()
assert ent.title == "demo data", ent
```

`retrieve_nix_entity` returns a bare `FxtrID` (like
`lookup_entity`); wrap it in an `EntityRef` to load. If you've
added untracked files to the workdir, the result includes a
one-line notice that nix can't see them until `git add`.

**Scenario 5: spawn a sandboxed service by git reference.** No
bundling — the spawner builds ``fxtr.services.hello_service``
from your committed flake at the commit you pin. The workdir
must be a **clean** git repo (``current_commit()`` errors on a
dirty tree, so commit first).

```python
# run_script — schedule the spawn
defn = NixWasmServiceDefn(
    git_commit=current_commit(),
    flake_attr="fxtr.services.hello_service",
).store()
spawner = await lookup_service_by_name("sandbox-spawner")
await schedule_invoke("running", spawner, lambda s: s.spawn(defn))
```

Wait for ``<channel ... slot_key="running" kind="invoke_result">…</channel>``
(the build offloads to nix, so it can take a while), then
connect with a ``HelloConfig`` and call ``greet()``:

```python
# run_script — connect + greet
from hello_service import HelloConfig

wrapper = await retrieve_result("running")
cfg = HelloConfig(name="world").store()
await schedule_invoke("hello_svc", wrapper, lambda s: s.connect(cfg))
# (after hello_svc lands)
svc = await retrieve_result("hello_svc")
await schedule_invoke("greeting", svc, lambda s: s.greet())
```

### 3. Close out

```
close_session()
```

## What each scenario exercises

| Scenario | Path covered |
|---|---|
| ``schedule_basic_echo`` / ``retrieve_basic_echo`` | ``lookup_service_by_name`` → ``schedule_invoke`` with scalar args → channel notification → ``retrieve_result``. |
| ``schedule_entity_arg_echo`` / ``retrieve_entity_arg_echo`` | Same plus entity-typed invoke arg: a fresh ``Note`` is stored sandbox-side, passed into the service, and loaded back from the result. |
| ``fetch_demo_note`` | ``lookup_entity_by_name`` → ``EntityRef.load``. No invoke worker involvement. |
| Scenario 4 (retrieve nix entity) | ``retrieve_nix_entity`` → nix builds ``fxtr.entities.demo_data.layer`` → entity bridged into the script → ``EntityRef.load``. |
| Scenario 5 (git-ref spawn) | ``current_commit`` → ``NixWasmServiceDefn`` → spawner builds ``fxtr.services.hello_service`` from the pinned commit → ``connect`` → ``greet``. |

## UI presets (renderer dispatch)

The flake also declares ``fxtr.uiPresets.demo_preset`` — a
**renderer preset** (CODE, referenced by committed git ref, like a
service), assembled by ``lib.mkUiPreset``. It maps the ``DemoDataEntity``
type to the ``renderers/demo_data/`` module across the four shell dispatch
keys (FullPanel / BlockLink / InlineLink / HoverPreview). ``mkUiPreset``
takes one **entry per renderer** (here ``demo_data``: a ``renderer`` drv +
``subtypes`` declaring, per subtype, a ``forType = { idString; entity; }``
and its ``forDispatchKeys``) and transposes them into the type-id-keyed table
the dispatcher consumes, throwing on a duplicate ``(type, key)`` claim. A
nix/git dispatcher (``NixConfigurableDispatchImpl``) loads it by reference:

```python
# run_script (against a registered renderer dispatcher)
dispatcher = (await lookup_service_by_name("renderer-dispatcher")) \
    .replace_service_type(NixDispatcherProto)
await schedule_invoke("set_preset", dispatcher, lambda d: d.set_preset(
    git_commit=current_commit(),
    flake_attr="fxtr.uiPresets.demo_preset",
))
```

The dispatcher evals the preset, builds each renderer's ESM + the
attached ``typeEntity`` layer, validates that the preset key matches the
type, mints a renderer entity, and broadcasts a reload to every connected
frontend. The **preset key is the type's id_string** — obtain it from a
``run_script`` with ``print(DemoDataEntity.fxtr_type.id_string())`` and
paste it as the attribute name (there is no ``entityIdString`` nix
helper). Editing a renderer is the same loop as a service: edit
``renderers/demo_data/index.tsx``, commit, ``set_preset(current_commit(),
…)`` again.

> **Note:** this demo's workstation doesn't register a
> ``renderer-dispatcher`` service yet — that wiring (and a live,
> agent-driven ``set_preset`` walkthrough) lands with the demo migration.
> For now the preset path is exercised by the integration test
> (``fxtr_workstation_mcp/tests/test_nix_dispatch_preset.py``), which
> drives a ``NixConfigurableDispatchImpl`` against this workdir directly.

## Tips

- If you want to inspect what's actually in your session's
  state, the connection's mirror is accessible during testing
  but not from inside scripts — use ``retrieve_result`` to read
  individual slots, or watch the workstation's debug UI.
- ``retrieve_result`` raises ``LookupError`` if the slot is
  still ``invoke_pending``. Wait for the channel frame before
  retrieving; don't poll.
- Scenarios are intentionally tiny — feel free to read
  ``packages/demo_pkg/src/demo_pkg/scenarios.py`` to see exactly what each
  one does under the hood.
