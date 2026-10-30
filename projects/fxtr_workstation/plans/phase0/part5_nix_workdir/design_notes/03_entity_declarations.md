# Entity declarations + layered file stores

How the workstation flake declares "entities" — named, manifest-level
pointers to (layer, key) pairs. **This note is about the DATA slice
(`fxtr.entities`) only.**

> **Revised (2026-05-30):** entities are now strictly DATA. The CODE
> slices — `fxtr.services` (Area 05) and `fxtr.workstation.uiPresets`
> (Area 06) — are **not** entities; they're plain attrsets referencing
> derivations, resolved by git commit. An earlier draft of this note
> said `uiPresets` was an entity of identical shape; that is no longer
> true.

## Shape

Each entity is a plain nix attrset with two fields (+ a type discriminator):

```nix
entities.<name> = {
  type = "fxtr_layer_entity";
  layer = <derivation producing a layered-file-store directory>;
  layerKey = "<key in that layer>";
};
```

This shape is for `fxtr.entities.<name>` (DATA). UI presets and
services do **not** use it — see Areas 05/06 for their (non-entity)
shapes.

The `type` discriminator is not an "entity type" discriminator, but
rather identifies the encoding (as a layered file store entity).
No `entityType` discriminator. The kind of entity is encoded inside
the entity itself (via `$fxtr.type`), not in the manifest.

Names within each folder are flake-author chosen and unconstrained
(except by nix attr-name rules). They function as "git-branch"-like
mutable pointers to immutable layered storage — the same key in the
same layer is always the same content, but the name `entities.foo`
can be rewired to point at a different (layer, layerKey) pair
without anything else noticing.

## Hybrid layer / entity model

The flake-author expresses ~tens of *layers* (each a derivation with
its own build logic) and ~hundreds of *entities* (each a manifest
entry). One layer commonly backs many entities — and that's
expressed naturally via attribute sharing:

```nix
let
  buildResultsLayer = pkgs.callPackage ./build_results_layer.nix { ... };
in {
  entities = {
    foo = { layer = buildResultsLayer; layerKey = "key-for-foo"; type = "fxtr_layer_entity"; };
    bar = { layer = buildResultsLayer; layerKey = "key-for-bar"; type = "fxtr_layer_entity"; };
    baz = { layer = someOtherLayer; layerKey = "key-for-baz"; type = "fxtr_layer_entity"; };
    # ...
  };
}
```

Because the `buildResultsLayer` derivation is the same nix value in
both entries, the MCP's "build foo's layer" and "build bar's
layer" produce the same store path. Nix's content-addressing handles
dedup for free; we just declare honestly.

## Why this shape (vs alternatives)

Two alternatives considered (see chat thread for the full
discussion):

- **Option B** — `entities.<name>` IS the layer derivation, with
  `layerKey` attached via `passthru`. Tempting because `nix build
  .#entities.foo` directly returns the layer path, but mixes
  building and metadata: `nix build .#entities` would build *all*
  of them. Option A keeps a clean separation — derivations under
  `.layer`, metadata as plain strings.
- **Option C** — one monolithic `everything` derivation that
  produces a manifest JSON + all layers in one shot. Gives up
  per-leaf materialization (can't build just one) and forces the MCP
  to learn a custom manifest format. Not in scope for v0.

Option A is also the closest fit to how nix flakes typically express
structured outputs (think `lib`, `nixosConfigurations` — attrsets
holding derivations + plain data side-by-side). Standard `packages`
outputs are closer to Option B *because* a package is inherently a
single-derivation thing; our case isn't.

## "Build everything" convenience — dropped for v0

An earlier design shipped a `mkAll` helper assembling a
`fxtr.workstation.all` linkFarm so agents/CI could force a full build.
**Removed for v0.** Two reasons:

- **Weak benefit.** Consumers now build lazily from *committed git
  refs* (the spawner/dispatcher) or `path:` (the MCP, data). A
  `path:`-built linkFarm wouldn't even reliably pre-warm the git-ref
  builds — `path:` copies the whole working tree and yields a different
  flake-source store path than the tracked-tree git fetch, so the store
  paths can differ. The reliable pre-warm / fail-fast is just building
  the specific committed attr the consumer will use
  (`nix build "<repo>?rev=<c>#fxtr.services.foo.sitePackages"`).
- **Real cost.** Enumerating "everything" is now fiddly — renderer ESMs
  are nested inside preset attrsets keyed by id_string.

The one residual use — a CI "does every declared object still build?"
rot-check — is better served by a CI target (or `nix flake check`) than
an agent-facing flake helper. Tracked as a follow-up.

## Open questions

- Q3.A (resolved): no `lib.fxtr.mkEntity` helper. Entity attrs are
  constructed by the helper that *produces* them (`mkEntityFromPython`
  — the only entity-producing helper now that code helpers don't write
  entities), or written inline as the three-field attrset
  (`{ type = "fxtr_layer_entity"; layer = ...; layerKey = "..."; }`).
  A future `meta` slot, if wanted, would live on the individual
  producing helpers, not on a generic constructor.
- Q3.B: what other DATA layer-writer helpers do we ship? `mkEntityFromPython`
  (Python-resolved object → layer) is the v0 one. More generic helpers
  (e.g. "store these literal bytes as an entity with type T") come
  later. (The old "package source → bundle entity" helper is gone —
  services aren't entities; see `05_sandbox_bundle.md`.)
- Q3.C: Is there a maximum-entities-per-layer convention or limit?
  The layered file store has no architectural limit, but ergonomics
  of having one layer with thousands of keys vs many smaller layers
  isn't obvious yet.
