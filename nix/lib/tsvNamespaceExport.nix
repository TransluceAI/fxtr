# tsvNamespaceExport — publish a committed tsv namespace as a PURE manifest.
#
# Wraps `runFxtrWriter` with the `namespace_export` writer kind: parses +
# validates the committed `fxtr_namespace.toml` + `*.fxtr.tsv` files under
# `namespaceRoot` and emits `$out/fxtr_namespace_export.json` — the publication
# manifest the runner-manager's namespace read view consumes. No FOD, no
# network, no layer: only the server reads it, so it never needs hash
# re-pinning and stays automatically in sync with the TSVs.
#
# Declared in user flakes as `fxtr.namespaces.<name>`. Keeps part5's two
# invariants: `export` is the buildable leaf; `type` is cheap eval metadata.
{
  canonicalSystem,
  runFxtrWriter,
}:
{
  # Store path of the dir containing fxtr_namespace.toml + the *.fxtr.tsv files
  # (reaches the build as flake source).
  namespaceRoot,
  # The published namespace name recorded in the manifest.
  name,
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
}:
{
  type = "fxtr_namespace_export";
  export = runFxtrWriter {
    name = "fxtr-namespace-export-${name}";
    inherit system;
    spec = {
      kind = "namespace_export";
      inherit namespaceRoot;
      namespaceName = name;
    };
  };
}
