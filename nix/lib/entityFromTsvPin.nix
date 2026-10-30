# entityFromTsvPin — a single tsv-namespace pin as a `fxtr.entities.<name>`.
#
# Sugar over `runFxtrWriter` with the `entity_from_tsv_pin` writer kind: a FOD
# that resolves one pin key → id from the committed `*.fxtr.tsv` files (pure)
# and fetches that id's bytes via the build-time rehost (`FXTR_REMOTE_*`) into a
# one-pin layered store. Drops straight in wherever a `fxtr_layer_entity` is
# expected. Content deps of the entity are left as remote precomp markers — they
# resolve through whatever remote the reader is opened with (the manager's repo
# store).
#
# Because it fetches, it is a fixed-output derivation: pass the pinned
# `outputHash` (v0: `lib.fakeHash` then re-pin from the mismatch error; the
# part-2b lock tool automates this). The layer output is byte-stable for a fixed
# id, so the hash is reproducible.
{
  canonicalSystem,
  runFxtrWriter,
  lib,
}:
{
  # Store path of the dir containing fxtr_namespace.toml + the *.fxtr.tsv files.
  namespaceRoot,
  # The full pin key (e.g. "greeting" or "data/models:encoder").
  key,
  # Pinned recursive-sha256 of the produced layer (the FOD hash).
  outputHash,
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
  # Derivation name.
  name ? "fxtr-entity-from-tsv-pin",
}:
let
  # Narrow the source to exactly the tsv namespace (the marker + every
  # `*.fxtr.tsv`), dropping `flake.nix`, `fod_lock.toml`, `flake.lock`, VCS
  # files, etc. Hygiene: the FOD's inputs are precisely the files the writer
  # reads, so unrelated flake edits don't perturb its `.drv`, and the produced
  # layer never depends on the lock file it's pinned by.
  src = builtins.path {
    path = namespaceRoot;
    name = "fxtr-tsv-namespace-src";
    filter =
      p: type:
      type == "directory"
      || baseNameOf p == "fxtr_namespace.toml"
      || lib.hasSuffix ".fxtr.tsv" (baseNameOf p);
  };
in
{
  type = "fxtr_layer_entity";
  layer = runFxtrWriter {
    inherit name system outputHash;
    spec = {
      kind = "entity_from_tsv_pin";
      namespaceRoot = src;
      inherit key;
    };
    impureEnvVars = [
      "FXTR_REMOTE_BASE_URL"
      "FXTR_REMOTE_BEARER_TOKEN"
    ];
  };
  layerKey = key;
}
