# Stage 9 post-1 — Pyright pre-script checking

Goal: run pyright on `<preamble> + <script>` *before* dispatching
`run_script` to the sandbox. Strict mode — type errors block
execution and the agent gets the report back as the tool result.
This makes the typed-service surface from stage 9 (`get_service[T]`,
`schedule_invoke`, `result_type_of`, `retrieve_result`) actually
load-bearing: today an agent who passes the wrong arg type just
gets a runtime error from the sandbox; with this stage, pyright
catches it before any side effects are committed.

**Scope.** We type-check only `combined.py` (script body + preamble).
The agent's `packages/*/src` are reachable via `extraPaths` so the
script can `from my_pkg import Whatever` and get typed imports, but
diagnostics *inside* those packages are not surfaced — the MCP
doesn't try to maintain a per-workdir venv that resolves third-party
deps. If an agent's package has third-party deps it wants checked
deeply, that's the agent's own pyright workflow to run; the MCP's
job is the typed-service surface plus pure-package types.

This is post-stage9 because every piece of the typed-service
surface (sentinels, generic ServiceRefs, owned-callback
`ServiceRef[EventListenerProto]`, `result_type_of`'s
`Callable[Concatenate[_T_proto, _P], Awaitable[_R]]` signature)
was designed to be pyright-friendly. We now lean on that.

## What pyright needs to see

```
combined script (preamble + script body)
  │
  ├─ _fxtr_mcp_builtin   ← static stub from the bridge's stubs/ dir
  ├─ demo_pkg / etc.     ← agent's own packages (via packages/<name>/src)
  └─ fxtr2 / fxtr_ws_mcp_sandbox_bridge
                         ← reached as extraPaths into the source
                           dirs that the sandbox env exposes at
                           runtime (no host venv, no sandbox venv)
```

The combined-script trick: pyright sees one logical module made
by concatenating `preamble.py` and the script body. This matches
the runtime semantic (preamble globals are in scope for the
script) and avoids needing a multi-file project.

We deliberately do **not** point pyright at the MCP host's venv
(it would let the agent `import httpx` etc. at script scope and
pass typing while crashing the sandbox), and we don't build a
synthetic sandbox-shaped venv (managing dependencies across
potentially-conflicting workdir packages isn't worth it for v1).
extraPaths alone is enough to resolve the surface the typed-service
work actually targets. We grow the extraPath list if it turns out
some `fxtr2` import doesn't resolve through `v2/portable/` alone
(hatch force-includes — see "Risks" below).

## Pieces to build

### 1. `_fxtr_mcp_builtin.pyi` stub

The module is constructed at runtime by `build_builtins(host,
queued_ops)`, so static checkers can't see it. We ship a
hand-written `.pyi` next to `builtins.py` and bundle it into the
sandbox bridge package. The stub is the *contract* of the
builtins module — drift between it and the runtime closures is a
maintenance hazard, but the surface is small (~15 functions) and
changes rarely.

Critical overloads (so the chain `retrieve_result(slot,
result_type_of(P, P.m))` infers end-to-end):

```python
class _ResultTypeSentinel[T]: ...

def result_type_of[_T_proto, _R, **_P](
    target_type: type[_T_proto],
    method: Callable[Concatenate[_T_proto, _P], Awaitable[_R]],
) -> _ResultTypeSentinel[_R]: ...

# retrieve_result has three overloads:
@overload
async def retrieve_result(slot_key: str) -> Any: ...
@overload
async def retrieve_result[T](
    slot_key: str, result_type: _ResultTypeSentinel[T]
) -> T: ...
@overload
async def retrieve_result[T](
    slot_key: str, result_type: type[T]
) -> T: ...

# schedule_invoke is the headline: typed dispatch on the action.
async def schedule_invoke[T](
    slot_key: str,
    service: ServiceRef[T],
    action: Callable[[T], Awaitable[Any] | Any],
) -> None: ...
```

The `schedule_invoke` signature is what unlocks the killer
feature: `lambda s: s.method(...)` gets `s` typed as `T`, so
calling a non-existent method or passing a wrong arg is caught
statically.

**Auto-gen vs. hand-write?** Hand-write for v1. The surface is
small and stable. Auto-gen from `inspect.signature` over the
returned mapping would need a build step and would lose the
overload information (pyright wants overloads explicitly). If the
surface grows, revisit.

### 2. Cache-dir `pyrightconfig.json`

Lives in `~/.cache/fxtr-workstation-mcp/<sha256(workdir-abspath)>/`
alongside the generated `combined.py`. **Not in the agent's workdir**
— the workdir is agent-editable and an agent-editable
`extraPaths`/`venvPath` would let it reach outside the trust boundary
the rest of the MCP enforces.

Roughly:

```json
{
  "extraPaths": [
    "<workdir>/packages/<pkg1>/src",
    "<workdir>/packages/<pkg2>/src",
    ...
    "<repo>/v2/portable",
    "<bridge-install-dir>",
    "<bridge-install-dir>/stubs"
  ],
  "pythonVersion": "3.12",
  "typeCheckingMode": "strict",
  "reportMissingTypeStubs": "none",
  "reportMissingImports": "warning"
}
```

- Each `packages/<name>/src` is expanded explicitly — pyright's
  `extraPaths` is a literal list and doesn't glob. Same set the
  sandbox bridge already enumerates in `build_runner_volumes`;
  reuse that helper.
- `<repo>/v2/portable` (and any sibling dirs needed; see "Risks" on
  hatch force-includes) makes `fxtr2` resolvable. We grow the list
  if some import doesn't resolve through `v2/portable/` alone, and
  only revisit a more sophisticated layout if growing-the-list
  becomes unwieldy.
- `<bridge-install-dir>` exposes `fxtr_ws_mcp_sandbox_bridge.*`.
  Located at MCP startup via the installed bridge package
  (`Path(fxtr_ws_mcp_sandbox_bridge.__file__).parent.parent` or
  `importlib.resources`).
- `<bridge-install-dir>/stubs` lets `_fxtr_mcp_builtin.pyi` resolve
  as a top-level module — pyright finds `.pyi` files in extraPaths
  the same way it finds `.py` files, so no separate `stubPath`
  is needed.
- No `venvPath` and no `venv`. Standard library comes from pyright's
  bundled typeshed; everything else is in extraPaths.
- `typeCheckingMode: strict` plus tightened individual settings.
  Default-strict catches a lot; we may still need to dial individual
  rules.
- `reportMissingImports: "warning"` rather than `"error"`. Diagnostics
  inside `packages/*` (where the agent may have unresolved third-party
  deps that the MCP isn't responsible for resolving) get dropped by the
  file-path filter (see section 5) regardless, but `warning` better
  expresses pyright's actual confidence — the imports may or may not
  exist at sandbox runtime, the MCP can't tell.

Regenerated *before each `run_script`* — the package list can change
between calls and pyright's `extraPaths` doesn't glob, so we
recompute and write-if-different every time. Writes are cheap and
idempotent; pyright's mtime cache shrugs off no-op rewrites.

### 3. Pre-script invocation in `_do_script_job`

Insertion point: in `host_impl.py` (or `ws_connection.py`'s
worker) before the sandbox script runs but after we have
`(preamble_src, script_src)` in hand.

The script body uses top-level `await` (the runtime exec's it
inside a coroutine), which is not legal at module scope and
would trip pyright. The preamble runs at module scope without
top-level await, so it stays at module scope. The combined file
synthesizes the runtime's structure:

```
combined = (
    preamble_src
    + "\n"
    + "async def _fxtr_mcp_script_main():\n"
    + indent(script_src, "    ")
)
write to <cache-dir>/combined.py     # always same path, overwrite each call
run pyright --project <cache-dir> --outputjson <cache-dir>/combined.py
parse JSON, filter diagnostics by file
if errors: return tool-error string, skip sandbox dispatch
else: continue to sandbox
```

Preamble globals are at module scope, so they're in scope for
the script's coroutine body — same visibility as the runtime
semantics. The async-fn wrapping is the load-bearing trick: it
makes `await` legal and matches the runtime's exec-inside-a-
coroutine model exactly.

**Line back-translation via source map.** When we build
`combined.py`, we also build a `list[tuple[str, int]]` mapping
each combined line to `(source_file, source_line)`:

```
line_map[0..N-1]   = ("preamble.py", 1..N)   # preamble lines, 1-indexed
line_map[N]        = ("<script>", 0)         # the `async def …:` shell
line_map[N+1..]    = ("<script>", 1..M)      # script body lines, 1-indexed
```

Pyright error at `combined.py` line K → `line_map[K-1]`. No
arithmetic, no off-by-one — just an array lookup. The user-visible
error references read as `preamble.py:L: …` or `<script>:L: …`.
We don't try to fix column numbers (the script body is indented by
4 spaces in the wrapper; columns pass through unchanged for v1 —
agents care primarily about line numbers).

**Diagnostic file-path filter.** After pyright's JSON output is
parsed, we drop every diagnostic whose `file` isn't
`<cache-dir>/combined.py`. This filters out:
- Errors inside the bridge / `fxtr2` source (any internal type
  issues there aren't the agent's bug to fix and could only be
  resolved by pulling in third-party deps we explicitly aren't
  carrying).
- Errors inside `<workdir>/packages/<name>/` (those are the agent's
  own pyright workflow to run if they want them; we just need the
  packages' public types to be reachable for the script's imports).

Combined with the line back-translation, the agent sees errors only
in the source they directly wrote (preamble + script).

**`combined.py` location.** Lives at `<cache-dir>/combined.py` —
always same path, overwritten on each `run_script` call. This keeps
pyright's mtime-based cache effective for everything *else* in
extraPaths (workdir packages, bridge, fxtr2) while invalidating just
the synthesized file. Cache dir is keyed on the absolute workdir
path so distinct workdirs don't collide.

The error path returns a formatted block to the agent — same
fence-marker convention as `_format_script_error` so the agent
sees consistent output structure. Each error becomes one line:
`<source>:<line>: error: <message>`. Maybe limited to the
first N errors to keep the channel frame manageable.

**Bypass mechanism: inline pyright comments only.** Agents that
need to run code pyright doesn't like add `# pyright: ignore` /
`# type: ignore` to the offending lines. No tool-level kwarg,
no toml flag, no per-workdir disable. Keeps the typed contract
load-bearing — if an agent disables checks, the locality of the
comment makes the exception visible at the bug site.

### 4. Latency

Cold `pyright` invocation is 1.5–3s. For interactive use that's
a noticeable per-tool-call tax, but it's the per-call cost we
accept — no daemon, no LSP. Pyright's own cache helps for
subsequent calls in the same workdir (it tracks file mtimes
and avoids re-parsing unchanged sources), so steady-state
latency is closer to the bottom of that range than the top.

If the cold cost ever becomes intolerable we can revisit a
`pyright-langserver` daemon, but the implementation cost (LSP
framing, didOpen/didChange, capability negotiation, shutdown
hooks) is real and out of scope for now.

### 5. Error filtering / surface

Two layers of filtering, in order:

1. **File-path filter (load-bearing).** Drop any diagnostic whose
   `file` isn't `<cache-dir>/combined.py`. This is what makes the
   "no venv, just extraPaths" design work: packages and fxtr2 source
   may legitimately fail to fully resolve, and we don't want that
   noise polluting the agent's report. The agent only sees errors in
   code they wrote.

2. **Rule-level filter (taste).** Even within `combined.py`, some
   strict-mode diagnostics will be noisy:
   - `reportUnusedImport` — agents often write exploratory code with
     unused imports. Probably disable.
   - `reportMissingTypeStubs` — disabled per the config above
     (third-party packages often lack stubs).
   - `reportPrivateUsage` — uncertain; agents shouldn't reach into
     underscored fxtr2 internals but the rule false-positives on
     things like `mirror._data_by_key` reads.
   - Style-ish warnings (`reportUnnecessaryComparison` etc.) —
     probably leave enabled; if agents trip them, that's a real
     smell.

   Configured in `pyrightconfig.json`. Keep the disabled list minimal
   — every silenced rule is a missed opportunity.

## The schedule_invoke recording-proxy story

This is the magic moment for the typed surface:

```python
svc = get_service("s5", DelayedEchoProto)  # ServiceRef[DelayedEchoProto]
await schedule_invoke(
    "r", svc,
    lambda s: s.echo("hi", 0.1),  # ← pyright sees s: DelayedEchoProto
)
```

Pyright unifies:
- `service: ServiceRef[T]` with `svc: ServiceRef[DelayedEchoProto]`
  ⇒ `T = DelayedEchoProto`.
- `action: Callable[[T], ...]` ⇒ the lambda's `s` param is
  `DelayedEchoProto`.
- `s.echo("hi", 0.1)` type-checks against
  `DelayedEchoProto.echo(self, value: Any, delay: float)`. A
  wrong-arity or wrong-type call is a static error.

The `_RecordingProxy` runtime mechanics are invisible to pyright
— the typing presents `s` as a clean Protocol-typed value. If
the user does something the proxy can't actually record at
runtime (e.g. `s.method.__name__`, `s.method.bind(x)`), pyright
won't catch it and the recorder will fail — that's expected.
The static check's job is catching obvious method-call /
arg-type errors, not auditing every possible misuse of the
recording API.

This is the single biggest payoff of stage 9 + this stage.

## Generic Protocol substitution edge

Pyright should handle `get_service[MyProto[int]](...)` correctly
because the `_T_proto` TypeVar in `result_type_of` (and similar)
binds via standard generic inference. Worth a smoke-test once
the stubs are in place — the `Concatenate[_T_proto, _P]` trick
relies on pyright's TypeVar unification through a nested
generic, which is correct in principle but occasionally
surprising at the implementation level.

## Things to spike before committing

1. **Pyright cold-start latency on a realistic workdir.**
   Measured on a workdir with the typical demo packages, the
   bridge, fxtr2 — what's the actual per-call cost? Inform
   whether we need to revisit daemoning sooner than planned.
2. **The `result_type_of` + `retrieve_result` inference chain.**
   Hand-write a minimal stub, write a test script that
   exercises the chain, run pyright in a sandbox. Confirm `R`
   flows through to a typed return without manual annotation.
3. **Generic Protocol parameterization through schedule_invoke.**
   If a Protocol is generic (`MyProto[T]`), does
   `schedule_invoke[T](slot, service: ServiceRef[T], action:
   Callable[[T], Any])` substitute through correctly when the
   user passes `service: ServiceRef[MyProto[int]]`? Should work
   via standard TypeVar inference but worth a smoke test.
4. **`fxtr2` resolution via extraPaths.** Hatch force-includes mean
   `v2/portable/` alone may not cover the fxtr2 surface the sandbox
   actually exposes. Try a small combined.py that exercises a
   reasonable import surface (entities, refs, scopes), see which
   imports fail to resolve, grow the extraPath list. Settles
   empirically what the final `pyrightconfig.json` extraPaths look
   like. May surface a missing-stubs gap that needs a small bundle
   of typing-only shims; defer to implementation.
5. **Top-level-await wrapping survives strict mode.** The
   async-fn wrapper turns the script into a coroutine body —
   confirm pyright doesn't flag "coroutine return value not
   awaited" or similar style nits inside the wrapper.

## Files touched (sketch)

```
projects/fxtr_workstation_agent_plugin/
  sandbox/fxtr_ws_mcp_sandbox_bridge/
    stubs/
      _fxtr_mcp_builtin.pyi    new — hand-written stub
    pyproject.toml             include stubs/ in the wheel

  fxtr_workstation_mcp/
    pyproject.toml             pin `pyright` (PyPI package) as a dep
    fxtr_workstation_mcp/
      pyright_check.py         new — cache-dir resolution
                               (~/.cache/fxtr-workstation-mcp/<hash>),
                               combined.py synthesis
                               (preamble + async-fn-wrapped script),
                               source-map line back-translation,
                               pyrightconfig.json gen (re-written
                               per run_script), subprocess pyright
                               + JSON parse + file-path filter +
                               error formatting
      host_impl.py / ws_connection.py
                               hook pre-script check into the
                               script-job pipeline; on type error
                               return formatted output and skip
                               sandbox dispatch
```

No files in the agent's workdir — `pyrightconfig.json` and
`combined.py` both live in the OS cache dir, keyed by workdir hash.

## Implementation steps (rough)

- [ ] **Pin `pyright` in `fxtr_workstation_mcp/pyproject.toml`.**
      Depend on the `pyright` PyPI package (which bundles a specific
      node binary) rather than relying on a system install. Pinned
      version so behavior is reproducible across agent machines.
- [ ] **Hand-write `_fxtr_mcp_builtin.pyi`** in
      `sandbox/fxtr_ws_mcp_sandbox_bridge/stubs/`, covering every
      builtin returned by `build_builtins`. Match the runtime
      signatures exactly. Includes the three `retrieve_result`
      overloads, `schedule_invoke`'s generic signature, and
      `result_type_of`'s `Concatenate` form. Update the bridge's
      `pyproject.toml` to ship `stubs/` in the wheel.
- [ ] **Verify the stub** by hand-running pyright on a sample
      combined script against it. Confirm
      `result_type_of` → `retrieve_result` inference flows
      end-to-end. Spike #2.
- [ ] **`pyright_check.py` module.** Owns:
      - Cache-dir resolution: `~/.cache/fxtr-workstation-mcp/<sha256(workdir-abspath)>/`,
        created on demand.
      - Bridge-install-dir resolution (via `Path(fxtr_ws_mcp_sandbox_bridge.__file__)`
        or `importlib.resources`) so we know where `stubs/` lives.
      - `combined.py` synthesis (preamble at module scope + script body
        wrapped in `async def _fxtr_mcp_script_main(): ...`) plus the
        parallel `line_map: list[tuple[str, int]]`.
      - `pyrightconfig.json` regeneration: enumerate `packages/*/src`
        explicitly (no glob), add bridge dir, bridge `stubs/`,
        `v2/portable/` (and any additional dirs needed for `fxtr2`
        to resolve — settle empirically during spike #4).
        Write-if-different.
      - Run pyright subprocess with `--project <cache-dir> --outputjson <cache-dir>/combined.py`,
        parse JSON, drop diagnostics whose `file != <cache-dir>/combined.py`,
        back-map remaining diagnostics' line numbers via `line_map`,
        format as `<source>:<line>: <severity>: <message>`, limit to first N.
      - Skip on non-zero pyright exit only if errors are present —
        warnings might still yield non-zero in some configurations
        (check empirically).
- [ ] **Hook into the script job.** In whatever function builds
      the `_ScriptJob` request (today: `run_script` tool calling
      into `_handle_script_job`), invoke the check before
      submitting. On error, return the formatted error string as
      the tool result. No per-call bypass — agents disable via
      `# pyright: ignore` / `# type: ignore` comments on the
      offending lines.
- [ ] **Tests.** Add `test_pyright_check.py` with a
      type-correct script (passes) and a wrong-arg-type script
      (fails with the expected pyright error). Both via the
      MCP's tool callable path.
- [ ] **Update `CODE_OVERVIEW.md`** with a "Pre-script type
      check" subsection under "Sandbox interaction".
- [ ] **Latency profiling.** Time cold pyright on a realistic
      workdir. Spike #1.
- [ ] **(Stretch) `pyright-langserver` daemon mode.** Hold the
      langserver open on the MCP supervisor, dispatch via LSP.
      Avoids cold-start cost AND lets us hand the combined source
      to pyright without writing it to disk. Defer unless v1
      cold-start is unacceptable.

## Things that intentionally don't change

- Sandbox runtime / wire shape. Type-checking is purely host-side.
- The `_fxtr_mcp_builtin` runtime closures. The stub is a
  separate file that mirrors them but isn't consumed at runtime.
- The typed-service dialect / `result_type_of` signature.

## Risks / things to revisit

- **Stub drift.** The stub and `builtins.py` can fall out of
  sync silently — a new builtin gets added but the stub doesn't
  list it; a runtime signature changes but the stub keeps the
  old form; a builtin gets removed from runtime but the stub
  keeps the symbol. Punted for v1: we just keep both in the
  same package and rely on reviewer attention. If drift becomes
  a real problem in practice, a name-coverage assert (parse the
  `.pyi` via `ast`, compare to `build_builtins`'s mapping keys)
  is the cheap fallback.
- **Pyright version pinning.** Different pyright versions
  surface different diagnostics. Handled by step 1 (pin in
  `fxtr_workstation_mcp/pyproject.toml`). Worth flagging here for
  drift: if pyright is bumped, we should rerun the test suite to
  see what diagnostics change shape.
- **`fxtr2` force-include gap.** `v2/portable/` may not cover the
  whole fxtr2 surface the sandbox exposes (hatch force-includes
  pull in code from outside `v2/portable/` into the wheel).
  Mitigation is just "grow `extraPaths` until imports resolve",
  but if the layout is genuinely tangled we may need a small
  typing-only shim bundle or a more sophisticated cache-dir
  construction. Revisit only if extraPaths-growth becomes
  unwieldy. Spike #4.
- **Network filesystem latency.** Pyright reads many files;
  workdirs on slow filesystems (e.g. NFS, Docker bind-mounts on
  macOS) could amplify cold-start latency. Worth measuring.
- **Diagnostic noise.** Strict mode will probably surface lots
  of nits in real agent code that don't actually matter (e.g.
  inferring `Any` from dynamic constructs). The filter rules
  will need a few rounds of tuning. Plan to start strict and
  loosen specific rules as they prove noisy, rather than the
  reverse.
- **Stub generation for generic protocols.** If a Protocol's
  generic substitution needs `Concatenate` or similar, the stub
  for the corresponding builtin signature might need careful
  hand-tuning. Most cases should "just work" but generic edges
  can be tricky.

## Why strict by default

The typed-service surface from stage 9 is only valuable if
agents actually get static feedback. Warn-only mode means the
agent ships type-erroneous code and the typed annotations become
documentation rather than enforcement. Strict-by-default + an
explicit opt-out for genuine exploratory cases keeps the typed
contract load-bearing.

The cost is that strict mode will need some tuning — see
"Diagnostic noise" above — but that tuning is finite and
once-only. Warn-only mode would defer that work indefinitely
and accept permanent typing drift.
