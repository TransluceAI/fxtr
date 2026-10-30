# mkContainerService — build a container-kind `fxtr.services.<name>` attr
# from a guest package.
#
# Produces the attr shape the spawner's container arm consumes —
#
#   { kind  = "container";   # discriminator the spawner checks
#     image = <derivation>;  # nix2container JSON manifest (no tarball)
#   }
#
# `image` is a nix2container image description: a small JSON file whose
# layers reference store paths directly. The spawner builds it lazily,
# then delivers it into the container runtime with the nix2container-
# patched skopeo (`nix:` transport) — the full image tarball is never
# materialized in the nix store.
#
# Unlike the wasm attr there is NO `entryPoint` leaf: the entry point is
# baked into the image's `Cmd` (`python -m fxtr_service_runner <ep>`), so
# the built artifact fully controls what runs. The broader contract is
# "any container image that launches into a fxtr service runner" — a
# hand-built `{ kind = "container"; image = <nix2container drv>; }` whose
# Cmd speaks the service-runner UDS protocol works just as well; this
# helper is a convenience, not a constraint.
#
# The guest venv is a full-Linux venv (mkVenv, no wasi marker override);
# the guest package's pyproject is expected to depend on
# `fxtr-service-runner` + `fxtr-sandbox-rpc-adapter[fxtr2]` (git sources).
# The image is self-contained nix-only — no base distro.
#
# Design:
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md
{
  canonicalSystem,
  lib, # nixpkgs lib (for toLower)
  pkgsFor,
  mkVenv,
  nix2containerFor,
}:
{
  # Path to the guest package source (pyproject.toml + uv.lock + src/).
  src,
  # Image name; defaults to "fxtr-container-service-<project.name>".
  # (Informational: the spawner re-tags on delivery with a
  # content-addressed reference.)
  name ? null,
  # Single-arch pin; override to build on a different builder. The image
  # self-describes its architecture; the spawner reads it from the built
  # JSON and passes `--platform` accordingly.
  system ? canonicalSystem,
  # Wheel-first; threaded through to uv2nix.
  sourcePreference ? "wheel",
}:
let
  pyproject = builtins.fromTOML (builtins.readFile (src + "/pyproject.toml"));
  projectName = pyproject.project.name or "service";
  effectiveName = if name != null then name else "fxtr-container-service-${projectName}";

  entryPoint = import ./_readServiceEntryPoint.nix {
    inherit src;
    helper = "mkContainerService";
  };

  # Mirror mkVenv's interpreter default (python312), but pin it explicitly
  # here: the image Cmd references the base interpreter directly (see
  # below), so the venv and the Cmd must agree on which python that is.
  basePython = (pkgsFor system).python312;

  venv = mkVenv {
    workspaceRoot = src;
    name = "${effectiveName}-venv";
    inherit system sourcePreference;
    python = basePython;
  };

  n2c = nix2containerFor system;
in
{
  kind = "container";
  image = n2c.buildImage {
    # OCI image names must be lowercase; underscores from python project
    # names are fine, but normalize case defensively.
    name = lib.toLower effectiveName;
    # Run the BASE interpreter with PYTHONPATH pointing into the venv's
    # site-packages rather than `${venv}/bin/python`: pyproject-nix venvs
    # activate via an argv[0]-carrying exec shim, and argv[0] does not
    # reliably survive binfmt_misc-based emulation (translators are often
    # registered without the preserve-argv0 flag — e.g. Rosetta under
    # Apple Container and similar VM-backed runtimes), so the venv python
    # silently degrades to the bare interpreter there. The venv is thus a
    # packaging intermediate; the runtime contract is PYTHONPATH, which
    # works identically under emulation and native. importlib.metadata
    # entry-point discovery (fxtr_service_runner) sees sys.path dists.
    config = {
      Env = [ "PYTHONPATH=${venv}/lib/python${basePython.pythonVersion}/site-packages" ];
      Cmd = [
        "${basePython}/bin/python"
        "-m"
        "fxtr_service_runner"
        entryPoint
      ];
    };
  };
}
