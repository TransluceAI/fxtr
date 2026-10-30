# Stage 5 post-2 — Simplify the nix-CLI wrappers (no shared lib)

A follow-up to Stage 5 that **reverses** the `followups.md` "Shared nix wrapper
library" plan. The investigation that motivated a shared lib instead showed the
duplication is small, mechanical, and split across three *different* domains
(DATA/non-frozen vs CODE/frozen; MCP vs sandbox vs dispatcher), and that the
typed-error apparatus the wrappers carry is used by **nobody but their own
tests**. So rather than extract a shared library (premature: it needs a knob to
absorb the frozen/non-frozen variation, and couples three independent packages
to save ~50 lines), we **delete the dead surface and simplify in place**.

## What the audit found (the basis for this)

- `build_flake_ref` is identical in only **two** places (sandboxing +
  dispatcher); the MCP uses a different bare-workdir form. Its `"://"`-sniff +
  remote (`git+ssh`/`git+https`) passthrough has **no live caller** — only unit
  tests — and is forward-looking for the deferred remote-deployment followup.
- The `Nix{Service,Preset}{Ref,Eval,Build}Error` hierarchies: **zero runtime
  consumers**. Imported only by the two wrapper test files + the dispatcher
  `__init__.py` re-exports. The eval-vs-build distinction is asserted only in
  tests; `.ref` / `.context` / `.returncode` are read only in tests.
- The base type is caught in exactly **one** place — the MCP's
  `except nix_util.NixError` in `host_impl` (→ `NixEntityBuildFailed`, reads
  `.stderr`). The spawner doesn't catch its nix errors (they propagate as the
  invoke_error per `11_failure_and_ux.md`); the dispatcher catches broad
  `Exception` for auto-load and just `%r`s it.
- Genuinely-used distinctions (keep): `NoFlakeNixError` (MCP pre-flight),
  `WorkingTreeDirty` / `NotAGitRepo` (`git_util`, friendly `current_commit()`
  messages).

## Decisions

1. **No shared lib.** The followup's `fxtr_nix_util` / `fxtr_nix_cli` plan is
   withdrawn. ~50 lines that rhyme across three domain-specific wrappers is the
   right amount of duplication; extract later only if a 4th consumer with the
   *same* shape appears.
2. **`build_flake_ref` → local-only + private.** `git+file://{resolve(repo)}?rev={c}[&dir=…]`,
   ~4 lines, renamed `_build_flake_ref` (no external caller). Drop the `"://"`
   sniff, the remote passthrough, and `_STEP_ERRORS`. A remote constructor gets
   written when remote deployment lands and its shape is real.
3. **Errors — a named type only where something catches it.**
   - **sandboxing + dispatcher**: raise plain `RuntimeError` with the full
     `nix <step> failed for <ref>:\n<stderr>` message. Delete all three custom
     classes + the unused `.ref`/`.context`/`.returncode` attrs. Nothing catches
     them; the message is the whole contract.
   - **MCP**: keep one `NixError(Exception)` (collapse `NixEvalError` +
     `NixBuildError` into it; it carries `.stderr`, which `host_impl` reads) +
     `NoFlakeNixError`. Can't drop to `RuntimeError` without `host_impl`'s
     `except` over-catching.
4. **Drop the two wrapper test files** (`test_nix_git_ref.py`,
   `test_nix_preset_ref.py`). They test the nix CLI + thin wrapping with a
   fragile repo-root coupling; the real git-ref path is covered end-to-end by
   the spawner integration tests + the chunk-4 dispatch test, and a 4-line
   `_build_flake_ref` is correct-by-inspection. **No fixture flake /
   `prepare_isolated_flake_repo` needed** — that whole sub-effort evaporates.

## Implementation steps

1. [x] **`fxtr_sandboxing/nix.py`** — `_build_flake_ref` local-only; deleted
   `NixServiceRefError`/`Eval`/`Build` + `_STEP_ERRORS`; `_run_nix` raises
   `RuntimeError`. Trimmed `__all__`. (Spawner unchanged — it doesn't catch.)
2. [x] **`nix_configurable_dispatch/nix.py`** — same: `_build_flake_ref`
   local-only; deleted `NixPreset*`; `eval_preset_table` / `build_drv_outputs`
   raise `RuntimeError`. Trimmed the package `__init__.py` re-exports.
3. [x] **MCP `nix_util.py`** — collapsed `NixEvalError` + `NixBuildError` into
   `NixError` (stderr-carrying init); the three eval/build functions raise
   `NixError`; kept `NoFlakeNixError`. `host_impl` `except` clauses unchanged.
4. [x] **Deleted** `fxtr_sandboxing/.../tests/test_nix_git_ref.py` +
   `fxtr_workstation/tests/test_nix_preset_ref.py`.
   **Note (mid-impl):** the MCP's own `tests/test_nix_util.py` is a *keeper*
   (own tmp fixtures + real good-path coverage — eval-structure → dataclasses,
   the layer-not-built guard, build → store path), **not** one of the
   repo-root-coupled files. It was *updated* (its `NixEvalError`/`NixBuildError`
   `pytest.raises` collapsed to `NixError`; the `.attr_path` assertion moved to
   a message substring check), not deleted.
5. [x] **Updated `followups.md`** — the "Shared nix wrapper library" entry now
   records this reversal; the stage5 carry-forward note points at it too.
6. [x] **Updated CODE_OVERVIEWs** — `fxtr_sandboxing`, `fxtr_workstation`, the
   agent-plugin MCP.

## Verification ✅

- `fxtr_workstation` non-slow: **362 passed, 1 skipped**.
- `fxtr_sandboxing` non-slow: **148 passed, 9 skipped**.
- MCP `test_nix_util.py`: **8 passed** (collapsed error type).
- `test_nix_dispatch_preset.py` (chunk-4, slow): **1 passed** — happy path
  through the simplified `RuntimeError` wrapper is unaffected. (The unhappy
  path isn't exercised by any test now, by design — nothing catches it.)

## Out of scope / unchanged

- `git_util.py` (MCP) — untouched; only the MCP uses git directly.
- The `--apply` expressions, `FlakeStructure`, `build_drv_outputs`'s batch
  shape, the agent-surface mapping — all stay where they are.
- Remote (`git+ssh`/`git+https`) ref support — re-added as its own constructor
  when remote deployment lands.
