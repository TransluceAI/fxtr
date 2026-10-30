# reportBuilderCli — the fxtr_report_builder MDX→FxtrCbor CLI, built pure
# pnpm-in-nix as a self-contained `node` executable.
#
# A `stdenv.mkDerivation` that fetches the cli's locked npm deps via
# `pnpm.fetchDeps` (a lockfile FOD — network only there), bundles `src/main.ts`
# → `dist/main.cjs` with the package's own `esbuild.config.mjs` (cf.
# `esbuildRenderer.nix` for the esbuild-in-derivation precedent), then
# `makeWrapper`s a self-contained launcher:
#
#   $out/bin/fxtr-report-builder  ==  ${nodejs}/bin/node $out/libexec/main.cjs "$@"
#
# Node is baked into the wrapper, so a consumer (a part-3 builder whose venv has
# fxtr_report_builder) wires ONE thing — the executable path, via the builder's
# generic `env` (FXTR_REPORT_BUILDER_CLI) — and needs nothing else in the build.
#
# Exposed as a **derivation attr** (not a function): one pinned build shared by
# all consumers. The cli is deliberately outside the repo pnpm workspace (its own
# pnpm-lock.yaml) so fetchDeps sees a self-contained package, not the whole-repo
# TS closure — see projects/fxtr_report_builder/CODE_OVERVIEW.md.
{
  canonicalSystem,
  pkgsFor,
}:
let
  pkgs = pkgsFor canonicalSystem;
  inherit (pkgs) lib;

  pname = "fxtr-report-builder-cli";
  version = "0.0.1";

  # The cli package dir, narrowed to the inputs the build reads (drop node_modules
  # / dist build junk + VCS). fetchDeps hashes only package.json + the lock, so
  # src edits never perturb the deps FOD; the bundle rebuilds on src changes.
  src = lib.cleanSourceWith {
    src = ../../projects/fxtr_report_builder/ts/packages/cli;
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      base != "node_modules" && base != "dist";
    name = "${pname}-src";
  };
in
pkgs.stdenv.mkDerivation (finalAttrs: {
  inherit pname version src;

  nativeBuildInputs = [
    pkgs.nodejs
    pkgs.pnpm # the pnpmConfigHook drives this binary (not bundled with the hook)
    pkgs.pnpmConfigHook # populates node_modules from pnpmDeps (offline)
    pkgs.makeWrapper
  ];

  # The locked-deps FOD (the only networked step). v0 hash workflow: lib.fakeHash
  # then re-pin from the mismatch (`nix build` prints the real `got:` hash).
  pnpmDeps = pkgs.fetchPnpmDeps {
    inherit (finalAttrs) pname version src;
    fetcherVersion = 3;
    hash = "sha256-SFcoHcHOWvZ1urxKlRLB+0K5NvPb4VGqQUk6l65cZKQ=";
  };

  buildPhase = ''
    runHook preBuild
    node esbuild.config.mjs
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/libexec" "$out/bin"
    cp dist/main.cjs "$out/libexec/main.cjs"
    makeWrapper ${pkgs.nodejs}/bin/node "$out/bin/fxtr-report-builder" \
      --add-flags "$out/libexec/main.cjs"
    runHook postInstall
  '';
})
