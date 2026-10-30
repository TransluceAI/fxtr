# _runFxtrBuilder — shared scaffold beneath mkFxtrFnBuilder / mkFxtrScriptBuilder.
#
# Runs a user-supplied command (`runCmd`, e.g. `${venv}/bin/python …`) in a
# derivation that produces a layered file store at $out. The command runs user
# Python that imports `fxtr2.data.layered_file_store.nix_builder` to parse the
# build contract and get an open writer + readers (see that module). The build
# contract is handed over via `__structuredAttrs` as the `builderSpec` attr
# (`{ parents; spec; entry?; }`); the script reads $out for the output dir.
#
# Generalizes `runFxtrWriter`'s FOD branch: purity is the `outputHash` flag —
# null = an ordinary pure derivation (writer opened with no remote); set = a
# fixed-output derivation (recursive sha256, network allowed) that reads the
# `FXTR_REMOTE_*` rehost env (`impureEnvVars`) and builds a remote. Internal
# (not exposed in `lib.fxtr.*`); the two public helpers wrap it.
{
  canonicalSystem,
  pkgsFor,
}:
{
  # Derivation name.
  name,
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
  # The command run inside the derivation (the helper bakes in `${venv}/bin/…`).
  runCmd,
  # name -> layer derivation. Under `__structuredAttrs` each derivation
  # serializes to its out-path, so this reaches the build (and nix_builder) as a
  # name->path map, and each is a build dependency automatically.
  parents ? { },
  # Arbitrary JSON passthrough (the entrypoint's `ctx.spec`).
  spec ? { },
  # fn form only: "module:fn" the `-m` launcher resolves. null for the script
  # form (the script owns `main`).
  entry ? null,
  # Open the $out writer in provenanced mode: its pins round-trip provenance
  # (stored as bare provenance-node ids; see LayeredFileStoreIsolatedWriter).
  # The entrypoint is mode-agnostic. Default false = bare layer.
  provenanced ? false,
  # Author-declared default build provenance (arbitrary JSON; recommended shape
  # `{ desc = "..."; }`). Only legal when `provenanced`. Rides into the minted
  # build-step provenance entity verbatim. For an input-addressed build it is an
  # optional supplement to the `$out` anchor; for a fixed-output (FOD) build —
  # which has no safe `$out` anchor — it is REQUIRED. null = not declared.
  provenanceInfo ? null,
  # Generic "I need bare-name tools / setup hooks on PATH" knob -> nativeBuildInputs.
  extraBuildInputs ? [ ],
  # Generic extra derivation env (e.g. paths to CLI binaries). A
  # store path interpolated here is already a build dependency (no PATH needed).
  env ? { },
  # FOD pinning: null = pure; set = fixed-output derivation (recursive sha256).
  outputHash ? null,
  # FOD: env vars passed through to the (otherwise pure) sandbox — the
  # FXTR_REMOTE_* rehost contract.
  impureEnvVars ? [ ],
}:
let
  pkgs = pkgsFor system;
  inherit (pkgs) lib;
  # provenanceInfo is only meaningful in provenanced mode, and a fixed-output
  # (FOD = outputHash set) build has no safe `$out` anchor, so it MUST declare
  # provenanceInfo. Both are static config errors → fail at eval with a clear
  # message (the Python `_run` re-asserts the FOD rule as a defensive backstop).
  _ =
    if provenanceInfo != null && !provenanced then
      throw "provenanceInfo is only allowed when provenanced = true"
    else if outputHash != null && provenanced && provenanceInfo == null then
      throw (
        "a provenanced fixed-output (FOD) builder has no safe $out anchor; "
        + "declare provenanceInfo (e.g. { desc = \"...\"; })"
      )
    else
      null;
  builderSpec =
    { inherit parents spec provenanced; }
    // (if entry == null then { } else { inherit entry; })
    # Emit the key only when set, so existing builders' builderSpec (and hence
    # their .drv / $out) stay byte-unchanged.
    // (if provenanceInfo == null then { } else { provenanceInfo = provenanceInfo; });
  fodAttrs =
    if outputHash == null then
      { }
    else
      {
        inherit outputHash impureEnvVars;
        outputHashAlgo = "sha256";
        outputHashMode = "recursive";
      };
  # cacert gives the writer's httpx client an SSL_CERT_FILE in the sandbox (it
  # builds its SSL context eagerly even for the rehost's plain-HTTP URL); FOD only.
  fodBuildInputs = if outputHash == null then [ ] else [ pkgs.cacert ];
  # `env` is exported in the build command (not merged as top-level derivation
  # attrs): under __structuredAttrs scalar attrs become bash vars but aren't
  # exported to child processes (the python builder reads them via os.environ).
  # A store path interpolated here is still a build dependency (string context).
  envExports = lib.concatStrings (
    lib.mapAttrsToList (k: v: "export ${k}=${lib.escapeShellArg v}\n") env
  );
in
# Force the `_` validation chain (lazy `let` bindings don't otherwise fire).
builtins.seq _ (
  pkgs.runCommand name (
    {
      __structuredAttrs = true;
      inherit builderSpec;
      nativeBuildInputs = extraBuildInputs ++ fodBuildInputs;
    }
    // fodAttrs
  ) (envExports + runCmd)
)
