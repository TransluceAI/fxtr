{
  description = "fxtr_nix_runner rehost + entities E2E fixture.";

  # Placeholder; prepare_workdir_repo rewrites it to a pinned git ref in the
  # workdir copy. The other @...@ markers are substituted by the test itself
  # (a templated copy) before prepare_workdir_repo runs.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;
      pkgs = lib.pkgs;
    in
    {
      # The networked fetch FOD — the only build step allowed network access.
      # Proves the manager's build span delivers FXTR_REMOTE_* into the build
      # and the FOD reaches the bearer-authed rehost (env delivery + networking
      # + auth). The name is templated UNIQUE per test run so the FOD is never
      # served from the nix store cache (a FOD is keyed by name+outputHash) —
      # forcing a real fetch each run; the output is a fixed sentinel so its
      # outputHash is stable and known up front.
      fodFetch = pkgs.runCommand "@FOD_NAME@"
        {
          nativeBuildInputs = [ pkgs.curl pkgs.cacert ];
          outputHashMode = "flat";
          outputHashAlgo = "sha256";
          outputHash = "@FOD_OUTPUT_HASH@";
          impureEnvVars = [
            "FXTR_REMOTE_BASE_URL"
            "FXTR_REMOTE_BEARER_TOKEN"
          ];
        }
        ''
          set -e
          curl -sf -H "Authorization: Bearer $FXTR_REMOTE_BEARER_TOKEN" \
            "$FXTR_REMOTE_BASE_URL/data/@FOD_ENTITY_ID@" -o fetched
          test -s fetched
          printf 'fod-ok' > "$out"
        '';

      # A builtin entity — exercises the entities adapter's eval + builtin
      # short-circuit through real nix (no build/materialize). The full
      # layer-build path is not exercised by this fixture.
      fxtr.entities.demo = lib.builtinEntity { idString = "!basic_type"; };
    };
}
