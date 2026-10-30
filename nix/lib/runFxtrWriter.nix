# runFxtrWriter — run a fxtr_nix_writer entry point inside a derivation to
# produce a layered file store at $out.
#
# The shared scaffold the no-venv writer helpers build on. It builds the
# lib-internal fxtr2-bearing venv (mkVenv over nix/fxtr_nix_writer)
# and runs the `fxtr-nix-writer` console script. The writer spec is handed
# over via `__structuredAttrs` as the `writerSpec` attr (the script reads it
# from $NIX_ATTRS_JSON_FILE); the script reads $out for the output directory
# and writes a layered file store there. See
# design_notes/10_nix_infrastructure.md.
{
  canonicalSystem,
  pkgsFor,
  mkVenv,
}:
{
  # Derivation name.
  name,
  # Writer spec: an attrset with a `kind` discriminator (e.g. "hello_world")
  # plus kind-specific fields. Handed to the script as the `writerSpec`
  # structured attr.
  spec,
  # Single-arch pin; override to write on a different builder.
  system ? canonicalSystem,
  # Fixed-output-derivation pinning. When `outputHash` is non-null the result
  # is a FOD (recursive sha256 over `$out`) allowed network access — for
  # writers that fetch from the build-time rehost (`impureEnvVars` lists the
  # env vars to pass through, e.g. the `FXTR_REMOTE_*` contract). Pure writers
  # (the default) leave both unset. v0 hash workflow: `lib.fakeHash` + re-pin
  # from the mismatch error.
  outputHash ? null,
  impureEnvVars ? [ ],
}:
let
  pkgs = pkgsFor system;
  venv = mkVenv {
    name = "fxtr-nix-writer-venv";
    workspaceRoot = ../fxtr_nix_writer;
    inherit system;
  };
  fodAttrs =
    if outputHash == null then
      { }
    else
      {
        inherit outputHash impureEnvVars;
        outputHashAlgo = "sha256";
        outputHashMode = "recursive";
        # cacert provides SSL_CERT_FILE so the writer's httpx client can build
        # its SSL context in the sandbox (httpx does so eagerly even for the
        # rehost's plain-HTTP URL); also lets a future HTTPS remote work.
        nativeBuildInputs = [ pkgs.cacert ];
      };
in
pkgs.runCommand name (
  {
    __structuredAttrs = true;
    writerSpec = spec;
  }
  // fodAttrs
) ''
  ${venv}/bin/fxtr-nix-writer
''
