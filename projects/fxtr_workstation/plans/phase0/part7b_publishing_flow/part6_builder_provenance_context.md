# Part 6 — builder-minted default `provenance_context`

> Status: SCOPED (2026-06-22), not yet implemented. Split out of 7b part 5
> (`part5_layer_provenance.md`) as a separable follow-up. The 7b.5 core
> (preserve-provenance through the layer reader/writer + the `provenanced` builder
> flag) has **landed**; this part is independent of that mechanism and only adds
> *what* provenance a built entity carries by default.

## Why this is separate

7b.5 made the layered file store **preserve the provenance an entity already
carries** (the `provenanced` layer mode) and threaded a `provenanced` flag through
`mkFxtrFnBuilder` / `mkFxtrScriptBuilder` → `builderSpec` → `nix_builder._run` →
the `$out` writer. But it does **not** decide what provenance a freshly-built
entity gets. Today `nix_builder._run` opens `ctx.scope` as a plain `FxtrScope`
with **no** `provenance_context`, so a freshly-built entity (one whose children
don't already carry provenance) either stores bare, or — in a provenanced layer,
via `store_cbor`'s `"auto"` path — gets a `FallbackContextMarker` c-node keyed on
`scope.name` (`"nix_builder"`). Content-stable, but meaningless: it identifies the
*code path*, not *this build*.

## Goal

Have a provenanced builder set a **meaningful default build-step
`provenance_context`** on `ctx.scope`, so every entity the entrypoint stores
carries it automatically — *without per-script work*. For the common
(input-addressed) case the marker references the **nix output store path**
(`$out`), so a receiver who has that path in their nix store can ask nix which
derivation built the entity (`nix-store --query --deriver $out` → the `.drv`).
This gives the demo *meaningful* provenance (trace a served entity back to its
build). A user entrypoint can still override by opening a child scope with its own
`provenance_context`.

## Key constraint — FODs can't self-reference `$out`

The `$out` anchor is only safe for **input-addressed (pure)** builders:

- **Input-addressed build:** `$out` is computed from the derivation's *inputs*
  and is known before the build runs. Embedding it in the output content is fine
  and deterministic — the content hash doesn't feed back into `$out`.
- **FOD build** (`outputHash` set — the `remote is not None` case in
  `nix_builder`): `$out` is derived from `outputHash`, which *is* the content
  hash. Embedding `$out` in the content makes the content hash depend on `$out`
  which depends on the content hash — an unsolvable fixpoint; re-pinning never
  converges. So an FOD **cannot** embed its own `$out`.

So provenance splits by build kind. Rather than leave FODs meaningless, the author
**declares** their provenance up front:

- A new optional nix attr **`provenanceInfo`** on both builder helpers — arbitrary
  JSON (recommended shape `{ desc = "..."; }`), byte-stable, carried verbatim into
  the minted entity. Only legal when `provenanced = true`.
- **Two nix-specific c-node entity types** (defined in fxtr2), one per build kind:
  - **input-addressed** → carries `output_store_path` (`$out`, always) **plus**
    the optional `provenanceInfo`.
  - **FOD** → carries `provenanceInfo` **only** (no `$out`); `provenanceInfo` is
    **required** in provenanced mode (there is no safe machine anchor to fall back
    to).

This makes `$out` the **machine anchor** (input-addressed only) and
`provenanceInfo` the **declared human supplement** (everywhere). It also dissolves
7b.5's old determinism worry: input-addressed `$out` is already deterministic with
no `outputHash` to keep stable, and FOD `provenanceInfo` is static JSON — both
byte-stable by construction.

## Always-on (no extra knob)

The default `provenance_context` is **always set when `provenanced = true`**, off
when `false` (DECIDED — not a separate opt-in flag). `provenanced` is the existing
gate; a provenanced builder gets meaningful build provenance for free, and a
non-provenanced builder strips provenance on store anyway so the marker would be
pointless. A script wanting a *different* trace opens a child scope with its own
`provenance_context` (the override path).

## The two entity types (new, fxtr2)

Defined in a new fxtr2 module, **`fxtr2/data/layered_file_store/nix_build_provenance.py`**
(tagged nix-specific; adjacent to `nix_builder.py`). Both are `DataclassEntity`s
under a new nix-build `define_type` namespace UUID, mirroring how
`fxtr_sandboxing` declares `SandboxProvenanceContext`. Both carry the c-node
`value = FxtrID(".")` default (the c-node must unwrap to ground, same as
`SandboxProvenanceContext`).

```python
# input-addressed (pure) builds — the common case
class NixInputAddressedBuildProvenance(DataclassEntity, fxtr_type=...):
    output_store_path: str                 # $out — the machine anchor
    provenance_info: FrozenFxtrCbor | None = None   # optional declared supplement
    value: FxtrID = FxtrID(".")            # c-node unwrap target (ground)

# fixed-output (FOD) builds — $out is unsafe, so the author must declare
class NixFixedOutputBuildProvenance(DataclassEntity, fxtr_type=...):
    provenance_info: FrozenFxtrCbor        # REQUIRED — no machine anchor
    value: FxtrID = FxtrID(".")
```

`provenance_info` is `FrozenFxtrCbor` because the nix `provenanceInfo` JSON maps
directly onto the FxtrCbor shape (dicts / lists / strings / numbers). The input
must be **frozen** into `frozendict`s / tuples before it lands in the field (the
storable immutable form); no interpretation — it's opaque metadata.

(Naming/exact field types to be finalized in implementation; the asymmetry —
input-addressed has the optional anchor, FOD has the required info — is the load-
bearing distinction.)

## Plumbing the flag (the path `provenanceInfo` travels)

Mirrors the `provenanced` plumb from 7b.5, one layer at a time:

1. **`mkFxtrFnBuilder.nix` / `mkFxtrScriptBuilder.nix`** — add `provenanceInfo ? null`,
   pass through to `_runFxtrBuilder`.
2. **`_runFxtrBuilder.nix`** — add `provenanceInfo ? null`. Two **eval-time
   validations** (clear `throw`s, since these are static config errors):
   - `provenanceInfo != null && !provenanced` → throw (info only legal when
     provenanced).
   - `outputHash != null && provenanced && provenanceInfo == null` → throw
     ("a provenanced FOD has no safe `$out` anchor — declare `provenanceInfo`").
   Add `provenanceInfo` to `builderSpec` **only when non-null** (so existing
   builders' `builderSpec` — and hence their `.drv` / `$out` — are byte-unchanged).
3. **`nix_builder.amain`** — read `builderSpec.get("provenanceInfo")` (default
   `None`) and thread into `_run(provenance_info=…)`.
4. **`nix_builder._run`** — the actual minting (below). `_run` is unit-tested
   without nix, so it **re-asserts** validation (b) (provenanced FOD ⇒
   `provenance_info` present) as a defensive `ValueError`.

## Minting in `_run` (the nix-agnostic core)

`_run` already knows everything it needs: `out_dir` (== `$out`), `remote`
(`is None` ⇔ pure / input-addressed; `is not None` ⇔ FOD), `provenanced`, and the
new `provenance_info`. Behavior:

```
if not provenanced:
    ctx.scope = FxtrScope(include=[*reader_scopes])          # today's behavior
else:
    # mint the build-step c-node in a small BARE minting scope
    prov_scope = stack.enter(FxtrScope(isolated=True, include=[GLOBAL_SCOPE]))
    if remote is None:                                       # input-addressed
        entity = NixInputAddressedBuildProvenance(
            output_store_path=str(out_dir), provenance_info=provenance_info)
    else:                                                    # fixed-output (FOD)
        assert provenance_info is not None                   # defended (validation b)
        entity = NixFixedOutputBuildProvenance(provenance_info=provenance_info)
    c_node = prov_scope.create_c_node(entity._fxtr_convert_to_cbor())
    ctx.scope = FxtrScope(
        include=[*reader_scopes, prov_scope],
        provenance_context=c_node,
        provenance_context_from_scope=prov_scope)
```

Notes that fall out of the existing machinery (verified against the code):

- **Minting scope must be bare.** `prov_scope` has no `provenance_context`, so its
  `$fxtr.ctx` marker is the ground `.` — exactly what `create_c_node`'s c-node-body
  validation wants (mirrors `_get_or_mint_fallback_or_own_marker`, which mints in a
  bare `include=[GLOBAL_SCOPE]` scope). The `define_type` type entity resolves
  through `GLOBAL_SCOPE`.
- **`provenance_context_from_scope` lifetime.** The c-node is *copied* into
  `ctx.scope`'s arena at entry (`FxtrScope` docstring: "synchronously copied … at
  entry, so … without holding a lifetime reference back to the source"). So
  `prov_scope` only needs to be alive when `ctx.scope` enters — keeping it in the
  same `AsyncExitStack` (entered first) satisfies that; it closes after `ctx.scope`.
- **The entrypoint stays mode-agnostic.** It just `ctx.scope.store(...)`s; the
  scope's `provenance_context` makes `store_cbor`'s `"auto"` path layer the result
  with the build c-node. The 7b.5 provenanced *writer* then preserves it on disk.
- **Type entity rides into the layer.** The pin walk pulls each pinned entity's
  closure (incl. its provenance c-node → its `NixInputAddressedBuildProvenance` /
  `NixFixedOutputBuildProvenance` d-node → that type entity) into `data/`, so a reader
  reconstructs the layered id without the type pre-installed. (Loading it as a
  typed Python object needs the class imported — it lives in fxtr2 — but the demo
  read-side is 7c; here we only mint + attach.)

## Determinism

Both forms are byte-stable for fixed inputs, so the c-node id (and any entity that
inherits it) is reproducible across rebuilds of the same derivation:

- input-addressed: `$out` is input-addressed (stable) + `provenanceInfo` is static
  nix JSON.
- FOD: `provenanceInfo` only, static nix JSON (no `$out`).

Worth a regression assertion (same gate as the existing FOD/layer determinism
tests), but it holds by construction. Note that two *different* derivations
producing the same logical entity get *different* provenance traces (different
`$out`) but the **same content-addressed data id** — provenance is never in the
CBOR, so the 7b.5 determinism gate (on the bare data id) is unaffected.

## Out of scope

- **Read-side / serving.** Consuming this provenance over HTTP + the frontend, and
  resolving `$out` → `.drv` for display, is 7a parts 6/7 + the 7c demo.
- **`.drv`-hash inside the build.** Not available in the build env; the `$out` →
  `--query --deriver` route is the only one, and it's a read-side concern.

## Implementation steps

- [x] **New entity types** (`fxtr2`): add
      `fxtr2/data/layered_file_store/nix_build_provenance.py` with the nix-build
      `define_type` namespace UUID + `NixInputAddressedBuildProvenance` /
      `NixFixedOutputBuildProvenance` + a `mint_build_provenance_c_node(scope, …)`
      helper (selects the entity by `output_store_path is None`, supplies an
      explicit ground `$fxtr.ctx` so minting never depends on an active scope,
      raises if a fixed-output build has no `provenance_info`).
- [x] **`_run` minting** (`fxtr2`): add `provenance_info` param; when `provenanced`,
      mint the build c-node (input-addressed vs FOD by `remote is None`) and open
      `ctx.scope` with it as `provenance_context`. Re-assert the FOD-requires-info
      rule. Tests (no nix): pure provenanced builder → stored entity carries a
      `NixInputAddressedBuildProvenance` c-node referencing `out_dir`; with
      `provenance_info` set → present on the entity; FOD provenanced builder with
      info → `NixFixedOutputBuildProvenance` c-node, no `$out`; FOD provenanced without info
      → raises; `provenanced=False` → no provenance_context (unchanged).
      (Also hardened `_run` to discard the master remote lease via a stack
      callback, so a mid-setup raise — e.g. the FOD-no-info case — can't leak it
      and hang a remote store's teardown.)
- [x] **`amain`** (`fxtr2`): read `builderSpec.provenanceInfo` → `_run`. Test
      mirrors `test_amain_reads_provenanced_from_builder_spec` with a faked contract
      carrying `provenanceInfo`.
- [x] **nix helpers** (`nix/`): `provenanceInfo ? null` on `mkFxtrFnBuilder` /
      `mkFxtrScriptBuilder` → `_runFxtrBuilder` `builderSpec` (only when non-null) +
      the two eval-time `throw`s (forced via `builtins.seq`). py_testing checks
      (`test_nix_builder.py` + three new `flake.nix` targets):
      `fxtr-provenanced-fn-builder-check` builds + reads back a layered id whose
      c-node anchors on the built `$out` + carries `provenanceInfo`; the two throw
      targets fail the build with the expected messages. (Positive E2E passed via a
      real nix build; throw paths verified by `nix eval` + the py_testing tests.)
- [x] **Update CODE_OVERVIEW.md / LOCAL_CODE_OVERVIEW.md**: `v2`
      layered_file_store `LOCAL_CODE_OVERVIEW.md` (new `nix_build_provenance`
      module + `_run` minting), `nix/CODE_OVERVIEW.md` (the `provenanceInfo`
      builder attr + the two validations + the new test targets).
      `v2/CODE_OVERVIEW.md` needs no change — it's conceptual and the
      layered_file_store submodule detail lives in the LOCAL overview (same as
      7b.5).
