# _mkLockedSitePackages — build a flat site-packages directory straight from a
# uv workspace's `uv.lock`, bypassing uv2nix's `mkVirtualEnv`.
#
# Internal helper for `mkPyodideService` (pyemscripten binary tags) and
# `mkWasmService` (`binaryPlatformTags = [ ]` — pure-python only, the eryx
# WASI guest). Deliberately NOT exposed via lib.fxtr.*.
#
# Why not mkVenv (the former `_mkGuestSitePackages` route)? pyproject-nix's wheel
# selection (`pypa.selectWheels` → `isPlatformTagCompatible`) has no arm for
# `pyemscripten_*_wasm32` platform tags, so a venv built against any host
# interpreter can never select a PEP 783 binary wheel — it silently falls back
# to compiling the sdist for the build platform (the wrong artifact entirely).
# This helper reuses uv2nix's *lock parsing + PEP 508 marker resolution* only,
# then handles each resolved package itself:
#
# - **registry packages**: pick a wheel whose platform tags match one of
#   `binaryPlatformTags` (in order), else the pure `py3-none-any` wheel, else
#   fail loudly naming the package. `fetchurl` by the lock's url+sha256 (pure —
#   no FOD relocking), unzip flat into $out.
# - **local workspace members + git-sourced packages**: build through the
#   ordinary pyproject-nix pythonSet (build-backend semantics — hatch
#   force-include etc. — for free) and copy each drv's installed
#   `lib/python*/site-packages` subtree. Two subtleties, both load-bearing:
#   - The uv2nix overlay must carry `guestEnviron` (guest workspaces constrain
#     `[tool.uv] environments`, and the overlay asserts the lock's
#     supported-markers are satisfiable by its environ).
#   - Plucked drvs get `propagatedBuildInputs = [ ]`: under the guest environ
#     the set's *dependency* drvs would pick wheels via emscripten-unaware
#     `selectWheels` (a binary dep has no compatible wheel and would
#     sdist-build for the build platform). Building a pure-python package's
#     installed tree doesn't need its runtime deps present.
#   Limitation: uv2nix stages only the member directory when building a
#   *local* package, so out-of-dir hatch force-include (fxtr2-portable's
#   `../fxtr2` pattern) is unsupported for local members. Git-sourced deps
#   build from the full fetched repo, so the same pattern works there — which
#   is how fxtr2-portable normally reaches guest workspaces. Local members
#   must be self-contained.
#
# `__pycache__` is stripped because locally-built .pyc embed store paths,
# dragging the build python's closure into $out; guests recompile from .py
# anyway. NOTE: raw-unzipped registry wheels skip installer normalization —
# `*.data/` dirs (scripts, purelib relocation) are left as-is. None of the
# guest-facing deps use them; a package that does needs an install step here.
{
  inputs,
  canonicalSystem,
  pkgsFor,
}:
{
  # Path to the uv workspace root (pyproject.toml + uv.lock).
  workspaceRoot,
  # Derivation name.
  name ? "fxtr-locked-site-packages",
  # Single-arch pin; override to build on a different builder. Note the
  # output is platform-independent content (fetched wheels + pure-python
  # trees); the system only picks where fetch/copy/build run.
  system ? canonicalSystem,
  # PEP 508 marker overrides describing the guest, as raw strings (the same
  # shape uv2nix's mkPyprojectOverlay takes) — e.g.
  # { sys_platform = "emscripten"; platform_machine = "wasm32"; }.
  # Drives dependency resolution from the lock and the overlay assert; does
  # NOT drive which wheel file is picked (binaryPlatformTags does).
  guestEnviron,
  # Wheel platform tags accepted for binary wheels, in preference order
  # (e.g. [ "pyemscripten_2026_0_wasm32" ]). [ ] = pure-python only.
  binaryPlatformTags ? [ ],
  # Interpreter whose version drives python_version markers and builds the
  # local/git packages. Should match the guest's python minor version.
  python ? null,
  # Wheel-first for the pythonSet building local/git packages.
  sourcePreference ? "wheel",
}:
let
  inherit (inputs)
    nixpkgs
    uv2nix
    pyproject-nix
    pyproject-build-systems
    ;
  lib = nixpkgs.lib;
  pkgs = pkgsFor system;
  py = if python != null then python else pkgs.python312;

  lock1 = uv2nix.lib.lock1;
  pep508 = pyproject-nix.lib.pep508;

  lock = lock1.parseLock (builtins.fromTOML (builtins.readFile (workspaceRoot + "/uv.lock")));

  # Full marker environment: the interpreter's own environ (python_version
  # etc.) with the caller's guest keys overriding.
  environ = pep508.setEnviron (pep508.mkEnviron py) guestEnviron;

  workspace = uv2nix.lib.workspace.loadWorkspace { inherit workspaceRoot; };

  resolved = lock1.resolveDependencies {
    inherit lock environ;
    dependencies = builtins.attrNames workspace.deps.default;
  };
  allPackages = lib.attrValues resolved;

  # Source-kind partition. A `virtual` root has no installable content.
  isBuildViaSet = pkg: (lock1.isLocalPackage pkg && !(pkg.source ? virtual)) || pkg.source ? git;
  isVirtual = pkg: pkg.source ? virtual;
  registryPackages = lib.filter (pkg: !(isBuildViaSet pkg) && !(isVirtual pkg)) allPackages;
  builtPackages = lib.filter isBuildViaSet allPackages;

  # --- registry arm ---------------------------------------------------------

  pickWheel =
    pkg:
    let
      matchesTag = tag: lib.filter (w: lib.elem tag (w.file'.platformTags or [ ])) pkg.wheels;
      binaryMatches = lib.concatMap matchesTag binaryPlatformTags;
      # py3-none-any and friends (abiTags/platformTags are raw strings;
      # languageTags are parsed structs, so match on abi+platform).
      pure = lib.filter (
        w: lib.elem "any" (w.file'.platformTags or [ ]) && lib.elem "none" (w.file'.abiTags or [ ])
      ) pkg.wheels;
    in
    if binaryMatches != [ ] then
      lib.head binaryMatches
    else if pure != [ ] then
      lib.head pure
    else
      throw (
        "_mkLockedSitePackages: no compatible wheel for ${pkg.name} ${pkg.version} — "
        + "need platform tags ${builtins.toJSON binaryPlatformTags} or a pure py3-none-any wheel. "
        + "Locked wheels: ${builtins.toJSON (map (w: w.file'.filename or w.url or "?") pkg.wheels)}"
      );

  fetchWheel =
    wheel:
    assert lib.assertMsg (wheel ? url && wheel ? hash && wheel.hash != null) (
      "_mkLockedSitePackages: locked wheel entry without url+hash: ${builtins.toJSON wheel}"
    );
    pkgs.fetchurl {
      url = wheel.url;
      sha256 = lib.removePrefix "sha256:" wheel.hash;
    };

  fetchedWheels = map (pkg: fetchWheel (pickWheel pkg)) registryPackages;

  # --- local/git arm --------------------------------------------------------

  pythonSet =
    (pkgs.callPackage pyproject-nix.build.packages { python = py; }).overrideScope
      (
        lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay {
            inherit sourcePreference;
            environ = guestEnviron;
          })
        ]
      );

  builtInstalls = map (
    pkg:
    pythonSet.${pkg.name}.overrideAttrs (_: {
      propagatedBuildInputs = [ ];
    })
  ) builtPackages;
in
pkgs.runCommand name { nativeBuildInputs = [ pkgs.unzip ]; } (
  ''
    set -eu
    mkdir -p "$out"
  ''
  + lib.concatMapStrings (whl: ''
    unzip -qo ${whl} -d "$out"
  '') fetchedWheels
  + lib.concatMapStrings (drv: ''
    cp -RL ${drv}/lib/python*/site-packages/. "$out/"
  '') builtInstalls
  + ''
    chmod -R u+w "$out"
    find "$out" -type d -name __pycache__ -prune -exec rm -rf {} +
  ''
)
