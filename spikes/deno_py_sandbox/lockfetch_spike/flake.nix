# Spike: build a pyodide guest site-packages dir straight from uv.lock,
# bypassing uv2nix's mkVirtualEnv.
#
# Why: pyproject-nix's wheel selection (`pypa.selectWheels` →
# `isPlatformTagCompatible`) has no arm for `pyemscripten_*_wasm32` platform
# tags, so the mkVenv path can never pick a PEP 783 binary wheel — it would
# silently fall back to compiling the sdist for linux (the wrong artifact
# entirely). Instead of building a venv against a host interpreter and
# extracting site-packages (what `_mkGuestSitePackages.nix` does today), this
# reuses uv2nix's *lock parsing + PEP 508 marker resolution* only, then does
# its own wheel pick per resolved package:
#
#   1. `pyemscripten_<ABI>_wasm32` wheel if the lock has one (binary, exact
#      ABI pin — flake-controlled via `emscriptenAbiTag`),
#   2. else the pure `py3-none-any` wheel,
#   3. else fail loudly.
#
# Every wheel carries url+sha256 in uv.lock, so fetchurl is pure — no FOD
# relocking. Local/virtual workspace members are skipped here (the real
# implementation file-walks them like the venv path already does).
{
  description = "uv.lock -> pyodide guest site-packages, no mkVirtualEnv";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      lib = nixpkgs.lib;
      forAllSystems = lib.genAttrs [
        "aarch64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];

      # The pyodide ABI generation to accept for binary wheels. One value per
      # yearly pyodide ABI (PEP 783): pyodide 314.x = CPython 3.14 =
      # pyemscripten_2026_0. In the real helper this comes from the flake /
      # service attr, not a constant.
      emscriptenAbiTag = "pyemscripten_2026_0_wasm32";
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          lock1 = uv2nix.lib.lock1;
          pep508 = pyproject-nix.lib.pep508;

          lock = lock1.parseLock (builtins.fromTOML (builtins.readFile ./uv.lock));

          # Marker environment for the pyodide guest: a real interpreter's
          # environ (for python_version etc. — pyodide 314.x is CPython 3.14)
          # with the platform keys retargeted to emscripten. This drives which
          # deps are needed, not which wheel file is picked.
          guestEnviron = pep508.setEnviron (pep508.mkEnviron (pkgs.python314 or pkgs.python313)) {
            os_name = "posix";
            sys_platform = "emscripten";
            platform_system = "Emscripten";
            platform_machine = "wasm32";
          };

          resolved = lock1.resolveDependencies {
            inherit lock;
            environ = guestEnviron;
            dependencies = [ "lockfetch-spike" ]; # the workspace root package
          };

          remotePackages = lib.filter (pkg: !(lock1.isLocalPackage pkg)) (lib.attrValues resolved);

          # Local packages resolved for the guest, minus the virtual workspace
          # root. Their wheels are built through the ordinary pyproject-nix
          # pythonSet under a NEUTRAL (build-host) environ — pure-python wheel
          # content is environ-independent, and forcing these drvs under the
          # emscripten environ would force their propagated dep closure through
          # emscripten wheel selection (where a binary dep would try to build
          # its sdist for linux). Only each local package's own wheel is taken;
          # the dep closure it was built inside is discarded.
          localPackages = lib.filter (
            pkg: lock1.isLocalPackage pkg && !(pkg.source ? virtual)
          ) (lib.attrValues resolved);

          workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
          # The overlay must carry the emscripten environ: guest workspaces
          # constrain `[tool.uv] environments` to emscripten, and uv2nix
          # asserts the lock's supported-markers are satisfiable by the
          # overlay environ. That makes the set's *dependency* drvs pick
          # wheels under emscripten selection (where a binary dep has no
          # compatible wheel and would sdist-build for linux) — so plucked
          # local drvs get their dep propagation stripped below; building a
          # pure-python local's installed tree doesn't need its runtime deps.
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              python = pkgs.python314 or pkgs.python313;
            }).overrideScope
              (lib.composeManyExtensions [
                pyproject-build-systems.overlays.wheel
                (workspace.mkPyprojectOverlay {
                  sourcePreference = "wheel";
                  environ = {
                    sys_platform = "emscripten";
                    platform_machine = "wasm32";
                  };
                })
              ]);

          # Each local package's drv `out` is an installed tree
          # (lib/python3.X/site-packages/...) with build-backend semantics
          # (hatch force-include etc.) already applied — copy that subtree
          # rather than extracting a wheel. NOTE: uv2nix stages only the
          # member directory when building a *local* package, so out-of-dir
          # force-include (fxtr2-portable's `../fxtr2`) is unsupported for
          # local members; it works for *git*-sourced deps (full repo
          # fetched, built from subdir), which is how fxtr2-portable
          # actually reaches guest workspaces.
          localInstalls = map (
            pkg:
            pythonSet.${pkg.name}.overrideAttrs (_: {
              propagatedBuildInputs = [ ];
            })
          ) localPackages;

          # Wheel pick: exact-ABI emscripten binary wheel, else pure-python.
          pickWheel =
            pkg:
            let
              emsc = lib.filter (w: lib.elem emscriptenAbiTag w.file'.platformTags) pkg.wheels;
              pure = lib.filter (
                # py3-none-any and friends (abiTags/platformTags are raw strings;
                # languageTags are parsed structs, so match on abi+platform).
                w: lib.elem "any" w.file'.platformTags && lib.elem "none" w.file'.abiTags
              ) pkg.wheels;
            in
            if emsc != [ ] then
              lib.head emsc
            else if pure != [ ] then
              lib.head pure
            else
              throw "lockfetch: no ${emscriptenAbiTag} or py3-none-any wheel for ${pkg.name} ${pkg.version}";

          fetchWheel =
            wheel:
            pkgs.fetchurl {
              url = wheel.url;
              sha256 = lib.removePrefix "sha256:" wheel.hash;
            };

          selectedWheels = map (pkg: fetchWheel (pickWheel pkg)) remotePackages;
        in
        {
          default = pkgs.runCommand "lockfetch-guest-site-packages" {
            nativeBuildInputs = [ pkgs.unzip ];
          } (
            "mkdir -p $out\n"
            + lib.concatMapStrings (whl: "unzip -qo ${whl} -d $out\n") selectedWheels
            + lib.concatMapStrings (drv: ''
              cp -RL ${drv}/lib/python*/site-packages/. $out/
            '') localInstalls
            # Strip __pycache__ (same rationale as _mkGuestSitePackages.nix:
            # .pyc embeds store paths, dragging the build python's closure
            # into $out; the guest recompiles from .py anyway).
            + ''
              chmod -R u+w "$out"
              find "$out" -type d -name __pycache__ -prune -exec rm -rf {} +
            ''
          );
        }
      );
    };
}
