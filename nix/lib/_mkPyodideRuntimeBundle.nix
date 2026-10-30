# _mkPyodideRuntimeBundle — the pyodide runtime assets for the pyodide
# sandbox backend, as one derivation.
#
# Internal helper for `mkPyodideService`; NOT exposed via lib.fxtr.*.
#
# Contents:
# - `$out/pyodide/` — the unpacked pyodide npm tarball (pyodide.mjs,
#   pyodide.asm.wasm, python stdlib zip, ...). The host-trusted deno driver
#   imports `pyodide.mjs` from here BY FILE PATH and boots with
#   `indexURL = $out/pyodide` — no npm resolution, no network, and the deno
#   read grant stays scoped to this bundle + the site-packages + the sockets.
# - `$out/guest_bootstrap.py` — the guest bootstrap, copied from THIS fxtr
#   rev (the one the user flake pins). It is guest code (python inside
#   pyodide, untrusted) whose imports come from the flake-locked guest
#   site-packages, so it versions with the flake — unlike the driver, which
#   is host-owned and ships with the host fxtr install.
#
# The pin (version + tarball hash) arrives from the caller as one tuple —
# see `pyodideDefaultPin` in ./default.nix; `mkPyodideService` accepts
# overrides, so user flakes control their pyodide version. Everything in
# this bundle is untrusted at runtime: the deno permission flags (host-owned)
# are the security boundary, so an overridden pyodide has exactly the powers
# malicious guest python already has.
{
  canonicalSystem,
  pkgsFor,
}:
{
  # Pyodide npm release version, e.g. "314.0.2".
  pyodideVersion,
  # SRI hash of https://registry.npmjs.org/pyodide/-/pyodide-<version>.tgz.
  pyodideTarballHash,
  name ? "fxtr-pyodide-runtime-bundle-${pyodideVersion}",
  system ? canonicalSystem,
}:
let
  pkgs = pkgsFor system;
  tarball = pkgs.fetchurl {
    url = "https://registry.npmjs.org/pyodide/-/pyodide-${pyodideVersion}.tgz";
    hash = pyodideTarballHash;
  };
in
pkgs.runCommand name { } ''
  set -eu
  mkdir -p "$out/pyodide"
  tar xzf ${tarball} -C .
  cp -R package/. "$out/pyodide/"
  cp ${
    ../../projects/fxtr_sandboxing/fxtr_pyodide_runner/src/fxtr_pyodide_runner/guest_bootstrap.py
  } "$out/guest_bootstrap.py"
''
