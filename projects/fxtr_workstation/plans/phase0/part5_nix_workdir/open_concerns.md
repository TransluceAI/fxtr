# Open concerns

Things in the Part 5 design that feel awkward, underspecified, or
worth a second look before we start implementation. Not blocking —
just flagged so we can decide consciously whether to act now,
fold into followups, or accept as-is.

Organized roughly by "act before starting" → "decide as we go" →
"considered but probably fine."

---

## Probably worth acting on before implementation

### 1. fxtr2 / sandbox-RPC-adapter version coupling

**What.** The agent's venv (built by `mkVenv` from the agent's
`pyproject.toml`) provides `fxtr2` + `fxtr-sandbox-rpc-adapter`.
The MCP injects `_fxtr_mcp_builtin` at runtime against whatever
fxtr2 the venv has. The workstation server speaks a specific
RPC wire protocol that the sandbox-side adapter has to agree
with. **All three** (MCP, workstation, adapter-in-venv) need to
agree on the wire shapes, but they can drift independently.

**Why it might matter.** Agents edit their `pyproject.toml` and
inadvertently bump the fxtr git-rev to one that's incompatible
with the MCP they're using. Failure mode is silent
serialization drift, not a clear error. The flagged Q2.D
"version check at startup" only catches it after the fact.

**Options.**

- **(a)** Accept for v0; document loudly that the agent's
  flake-input pinning of the fxtr repo must match the MCP's
  installation. Workdir template + docs explain this.
- **(b)** Pin tighter: the MCP exposes its own fxtr-input
  flake-ref (via env var or config), and `mkVenv` /
  `mkSandbox` consume it as a flake input override. This means
  changing the MCP installation also pins the agent's venv to
  the corresponding fxtr commit. Agents only override
  consciously.
- **(c)** Some form of explicit version-check handshake at
  connect time. Catches mismatches but doesn't prevent them.

**Decision.** Just accept for v0. Eventually, add runtime version
checks to detect version skew and fail. No attempt to automatically
align versions, the user is responsible for ensuring they match.

**Folded into:** `design_notes/02_prelude_and_venv.md` (Q2.D) +
`followups.md`. Flagged: the eventual runtime check needs a
queryable fxtr2 version over the adapter protocol, which doesn't
exist yet.

---

### 2. Validate `unsafeDiscardOutputDependency` + sidecar symlink convention EARLY — ❌ OBSOLETE (2026-05-30)

**Obsolete under the code/entity decoupling.** Bundles no longer carry
a `.drv` and are no longer entities, so there's no closure-reachability
problem to solve — git transports the code. The whole
`unsafeDiscardOutputDependency` / closure-scan / sidecar-symlink
mechanic is removed (Area 12), and the synthetic validation Stage 1
built for it is orphaned (removed in Stage 3). The original concern is
retained below for the record only.

**What (original).** Area 12's bundle-pinning story relied on two
mechanics we hadn't independently verified:

- `builtins.unsafeDiscardOutputDependency` keeping the bundle's
  output out of the entity-build's input closure.
- The closure scanner picking up the `.drv` substring inside
  CBOR-encoded entity bytes (or, failing that, the sidecar
  `out/_drv_refs/<name>.drv` symlink doing the job instead).

**Why it might matter.** Both mechanics are load-bearing for
Stage 3 onward. If either doesn't behave as expected, the
bundle pinning collapses and we have to redesign mid-stage.
"Sidecar symlink as belt-and-suspenders" is a convention we
invented; we don't have prior art that confirms nix's closure
scanner detects symlink targets the way we're assuming.

**What to do.**

- Promote Q12.D from a "verification during implementation" to
  an **explicit early task in Stage 1 or early Stage 3** —
  write a tiny test that constructs a layered file store
  containing a CBOR string referencing a `.drv` path, runs
  `nix-store --query --references` over it, and asserts the
  scanner sees the drv. Same for the symlink fallback.
- If neither works the way we expect, we need to know before
  building `mkWasmPackageBundle` on top of the assumption.

**Decision.** Pull into Stage 1 (after the writer
scaffolding lands) as a "validate the closure-scan mechanic"
task. Cheap to do, expensive to defer. Can be folded into the
"hello world" task.

**Folded into:** `stage1_nix_foundations.md` (scope + end-of-stage
check); `followups.md` Q12.D entry marked moved. The Stage 1 check
is synthetic (embeds a fake `.drv`-shaped string) since the real
bundle consumer doesn't exist until Stage 3.

---

### 3. uv2nix → eryx compatibility sanity check

**What.** `mkVenv` uses uv2nix to build a Python environment.
That environment runs not on the host but inside eryx
(CPython 3.14 WASM). Pure-python wheels should work either way,
but uv2nix's typical use case is "build a venv for the host";
we're using it for "build a venv that runs under a different
Python interpreter."

**Why it might matter.** Subtle issues might surface: arch
tags baked into wheel filenames, `.dist-info` directory layout
quirks, scripts directories that don't apply in WASM, etc. The
spike (`2026_05_21_nix_fxtr_prototype`) demonstrates uv2nix +
fxtr works, but that's a "Python derivation runs on the host"
case, not "Python venv runs in eryx-WASM."

**What to do.** Run a tiny end-to-end test early in Stage 2 —
build a minimal venv with `fxtr-sandbox-rpc-adapter[fxtr2-portable]`
via `mkVenv`, bind-mount into a freshly-built eryx sandbox,
verify `import fxtr2` works. This is essentially Stage 2's
end-of-stage check, but done as the *first* thing in Stage 2
rather than the last. If it doesn't work, we learn before
having gutted `sandbox_runtime`.

**Decision.** Sequence Stage 2 as: smoke-test mkVenv-in-
eryx FIRST, then do the broader MCP cutover. This is also a
forcing function for getting `mkVenv`'s post-processing right.
We may be able to tell uv to target a different platform by
passing `--python-platform`.

**Folded into:** `stage2_sandbox_runtime.md` (sequencing note).

---

## Decide as we go

### 4. Multi-tenant / per-agent repo assumption — ✅ RESOLVED (2026-05-30): not a real problem

**What (original concern).** The dispatcher/spawner hold a single
configured git repo; we worried two agents wanting *different* repos on
one workstation wouldn't be supported.

**Resolution.** It's a non-issue under the git-ref model. The
spawner/dispatcher resolve `?rev=<commit>` against their configured
repo, and git holds disconnected histories in one bare repo fine — so
multiple agents work by all **pushing to one shared bare repo** and
each referencing its own commit by hash. No per-agent repo machinery,
no out-of-band derivation push (that whole worry was an artifact of the
old `.drv`-pushing model; git is the transport now). And the trust
model is **connection-level** (like a nix remote builder), not a
per-repo allowlist — see `12_nix_drv_reference.md` "Trust model &
topology". The only genuinely workstation-wide thing is *which preset
the shared browser UI shows*, which is replace-semantics / UI ownership
(Area 06), not a repo limitation.

**Folded into:** `design_notes/12_nix_drv_reference.md` "Trust model &
topology", `design_notes/06_ui_presets.md`.

---

### 15. Inner-loop commit churn from `current_commit()` (accepted)

**What.** The clean-tree guard means every spawn / `set_preset`
iteration is edit → commit (→ push, remote) → reference. On a tight
loop that's a lot of commits (and pushes).

**Decision.** Accepted — it's the price of "shipped code is pinned,"
the workdir is a private repo, and the standard iteration pattern is to
**squash changes into a WIP commit** (amend) per iteration. Not a
blocker; flagged so it's not a surprise.

### 16. `flakePath` carried but unused by the demos (tracked → tested)

**What.** The recommended flow puts the flake at the workdir-repo root,
so `flakePath` (`&dir=`) is exercised by no demo.

**Decision.** Keep the field (cheap, real subpath case exists), and add
a dedicated Stage-3 unit/integration test that builds a service from a
subpath flake — so the path isn't untested. Demos stay root-flake.
**Folded into:** `stage3_bundle_and_spawner.md` end-of-stage check.

### 17. `set_preset` / `spawn` are long-blocking invokes on a cold cache (fine for now)

**What.** Eager renderer builds + a cold nix store can make `set_preset`
a multi-second-to-minute synchronous RPC; same for `spawn`.

**Decision.** Fine for v0 — the existing `spawn` path already does slow
nix work, so the invoke layer tolerates it. Sanity-check there's no
invoke timeout a cold build would trip; revisit (lazy builds, progress
notifications) if it bites.

---

### 5. Empty-table dispatcher startup is silent to the agent

**What.** If the agent forgets to declare `fxtr.workstation.
uiPresets.default`, the workstation comes up with an empty
table + a log line the agent never sees. Agent observes "all
entities render as NoMatchFallback" without context.

**Why it might matter.** First-run UX. Likely to happen if
agents copy the template, forget the `default` preset, and
wonder why nothing's rendering.

**Options.**

- **(a)** Surface "currently-loaded preset: none; flake
  declares: …" in the dispatcher's debug view (which Area 06
  references).
- **(b)** Make the auto-load fail loudly if the workstation
  knows the flake has a `default` slot but it's malformed,
  while silently no-oping only on "key absent."
- **(c)** Both.

**Decision.** (a) is enough as polish; pull into Stage 5
as part of the dispatcher impl.

**Folded into:** `design_notes/06_ui_presets.md` +
`stage5_ui_presets.md`.

---

### 6. Stage 2 bundles a lot of independent changes

**What.** Stage 2 lands `mkVenv` + `mkSandbox`, restructures
the demo workdir, removes `sandbox_runtime`, simplifies
`SandboxEnvSupervisor`, adds the MCP build trigger, adds the
setup header + `.tmp_outputs/` + friendly errors. The
end-of-stage check is essentially "`import fxtr2` works."

**Why it might matter.** If something breaks, narrowing it
down is annoying. Several of these changes are independent.

**Options.**

- **(a)** Leave as one stage; rely on tests catching
  regressions piecewise.
- **(b)** Split into two sub-stages: "venv build mechanics +
  isolation tests" then "MCP cutover."
- **(c)** No formal split, but order implementation tasks so
  the venv mechanics land + are validated first (per concern 3
  above).

**Decision.** (c) — same content as Stage 2, sequenced
internally so venv mechanics get green before the MCP cutover.

**Folded into:** `stage2_sandbox_runtime.md` (sequencing note,
shared with #3).

---

### 7. Stage 6 sizing

**What.** `fxtr_workstation_demo` migration is one bullet but
the demo has: a custom dispatcher impl, an agent workdir with
multiple packages, renderer subdirs with `fxtr_renderer.json`s,
integration tests, the demo's own README. Substantial migration.

**Why it might matter.** Stage 6 might be 2-3 substantial PRs
in practice rather than a single cleanup pass.

**Decision.** Leave as one nominal stage; just be
prepared for it to be larger than the other stages. If during
implementation it grows beyond reason, split.

**Folded into:** `stage6_workstation_demo.md` (sizing note).

---

### 14. Manual commit→spawn loop UX (new, 2026-05-30)

**What.** For v0 the agent's loop for shipping code is fully manual:
edit flake → `git commit` (→ `git push` for a remote consumer) →
`git rev-parse HEAD` → hand-write `NixWasmServiceDefn(commit=…, attr=…)`
/ `set_preset(commit=…, attr=…)`. No MCP builtin captures HEAD or
auto-commits.

**Why it might matter.** It's several manual steps, and a stale
copy-pasted commit hash (forgot to re-commit, or referenced the wrong
hash) fails at the spawner/dispatcher rather than at edit time. The
remote case adds a `push` the agent can forget (surfaces as a
git-fetch failure — see `11_failure_and_ux.md`).

**Decision.** Manual commit/push stays for v0, **plus** one cheap
correctness guard: a `current_commit()` MCP builtin (Stage 4) that
returns the workdir's git HEAD and **errors if the working tree has any
uncommitted changes — including untracked files** (nix git flakes only
see *tracked* files, so an uncommitted edit would silently build the
old code). The common path becomes correct-by-default:

```python
svc = NixWasmServiceDefn(git_commit=current_commit(), flake_attr="fxtr.services.foo")
```

It's read-only (`git rev-parse HEAD` + `git status --porcelain`), does
**not** auto-commit (the agent stays in control of *what* gets
committed), and does **not** cover the remote case — it can't verify
the commit was pushed to a remote consumer's repo, so a forgotten
`git push` still surfaces as a git-fetch failure at spawn
(`11_failure_and_ux.md`). A richer helper (auto-commit / construct the
whole defn) remains a later option (Q5.F).

**Folded into:** `05_sandbox_bundle.md` (Q5.F + flow), `09_migration.md`,
`stage4_retrieve_nix_entity.md` (builtin), `stage5_ui_presets.md`
(used for `set_preset`), `overview.md`.

---

## Considered but probably fine

**Disposition for all of #8–#13:** no folding needed — accepted
as-is for v0. Left here as a record of what was weighed. (#8 and
#12 are already tracked elsewhere: the shared-nix-wrapper followup
and the `WORKSPACE_TRUST_ROOTS` removal in Stage 4 / migration.)

### 8. Three nix-CLI wrappers across three packages
(MCP / spawner / dispatcher). Already captured as a followup.
Each is at a different layer of abstraction (workdir-relative
flake eval, raw `.drv` realize, workstation-relative flake
build), so the duplication isn't egregious. Extract a shared
lib if the surface stabilizes across all three.

### 9. `fxtr_nix_writer` vs `mkEntityFromPython` split
Two writer scaffolds (lib-internal-fxtr2 vs user-venv-fxtr2).
Forced by the venv-vs-no-venv distinction. Defensible.

### 10. `<entity attr>` shape has no nix-level static check
`{ type, layer, layerKey }` is just an attrset. Passing the
wrong shape gives runtime errors in the assembly script. Nix
doesn't have a great solution here.

### 11. `fxtr.workstation.*` vs `fxtr.entities` asymmetry
Stated rationale in Area 01: `fxtr.entities` is "general fxtr
stuff"; `fxtr.workstation` is "workstation-specific stuff."
Holds up.

### 12. `WORKSPACE_TRUST_ROOTS` going away
Was always best-effort sandboxing of one specific code path
(`prepare_package_bundle`). Removing it doesn't worsen the
agent's actual sandbox-breakout surface — runtime scripts can
already `open()` arbitrary paths in their venv / workdir.

### 13. Eryx version pinning
Comes through fxtr_sandboxing's existing setup, unchanged by
this work. Worth a sanity check during Stage 2 but not a
redesign concern.
