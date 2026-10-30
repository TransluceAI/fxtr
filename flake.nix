{
  description = "fxtr nix infrastructure — lib.fxtr helpers + build-time tooling (Part 5)";

  # The uv2nix stack (pyproject-nix, uv2nix, pyproject-build-systems)
  # backs `lib.fxtr.mkVenv`; `follows` pins a single nixpkgs across the
  # closure. Pattern follows the spike at
  # /Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/.
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

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

    # OCI images for `lib.fxtr.mkContainerService` (stage 7): builds a
    # small JSON manifest referencing store paths as layers — no image
    # tarball in the store. The spawner-side skopeo (the nix2container-
    # patched build that understands the `nix:` transport) is pinned
    # separately in `fxtr_sandboxing/nix.py`.
    nix2container = {
      url = "github:nlewo/nix2container";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{ self, nixpkgs, ... }:
    let
      # The fxtr helper namespace. Deliberately NOT indexed by system —
      # see overview.md "A principled single architecture".
      libFxtr = import ./nix/lib { inherit inputs; };

      # Shared by the fn/script builder-check targets: the py_testing venv and a
      # builder *input* layer (an InputNote pinned via mkEntityFromPython). Same
      # venv name/workspace as the other py_testing targets, so the derivation
      # is shared.
      pyTestingVenv = libFxtr.mkVenv {
        name = "py-testing-venv";
        workspaceRoot = ./nix/py_testing;
      };
      builderInputLayer =
        (libFxtr.mkEntityFromPython {
          venv = pyTestingVenv;
          fromPython = "py_testing.builder_fixture:input_note";
          name = "fxtr-builder-input-layer";
        }).layer;
      # The fn-builder layer (pins "wrapped" = a WrappedNote over builderInputLayer).
      # Shared by the fn-builder check + the report step (which reads it as input).
      fnBuilderLayer = libFxtr.mkFxtrFnBuilder {
        venv = pyTestingVenv;
        entry = "py_testing.builder_fixture:build";
        parents.base = builderInputLayer;
        spec.label = "fn";
        name = "fxtr-fn-builder-check";
      };
    in
    {
      lib.fxtr = libFxtr;

      packages.${libFxtr.canonicalSystem} = {
        # Smoke target: a trivial canonical-system derivation with no fxtr
        # code. Building it from a macOS host forces an offload to the
        # linux builder, confirming the builder is reachable. Useful as a
        # dev/CI reachability check.
        smoketest = libFxtr.pkgs.runCommand "fxtr-nix-smoketest" { } ''
          echo "fxtr nix smoke: built on $(uname -srm)" > "$out"
        '';

        # Probe that mkVenv produces a working fxtr2-bearing venv from the
        # fxtr_nix_writer workspace (fxtr2 reached via its `../../../v2`
        # path-dep). Building it exercises the whole uv2nix path.
        fxtr2-import-check =
          let
            venv = libFxtr.mkVenv {
              name = "fxtr-nix-writer-venv";
              workspaceRoot = ./nix/fxtr_nix_writer;
            };
          in
          libFxtr.pkgs.runCommand "fxtr2-import-check" { } ''
            ${venv}/bin/python -c \
              "import fxtr2, fxtr_nix_writer; print('fxtr2 ok:', fxtr2.__file__)" > "$out"
          '';

        # write_hello_world through the shared writer-builder: a layered
        # file store containing a single HelloWorldEntity. The py_testing
        # round-trip test reads the message back.
        hello-world-layer = libFxtr.runFxtrWriter {
          name = "hello-world-layer";
          spec = {
            kind = "hello_world";
            message = "hello world";
          };
        };

        # mkEntityFromPython over the py_testing venv: writes py_testing's
        # `sample_type_id` type entity into a layer. `.layer` is the
        # buildable derivation; the test reads the pinned entity back.
        sample-type-entity =
          (libFxtr.mkEntityFromPython {
            venv = libFxtr.mkVenv {
              name = "py-testing-venv";
              workspaceRoot = ./nix/py_testing;
            };
            fromPython = "py_testing.sample_type:sample_type_id";
          }).layer;

        # tsvNamespaceExport over an inline fixture namespace: a marker
        # (reserved_prefix="root") + one reserved-prefix TSV pinning a
        # content-stable id. Building `.export` runs the pure namespace_export
        # writer; the py_testing test reads `fxtr_namespace_export.json` back.
        # The fixture is generated inline so no source dir is committed.
        namespace-export-check =
          let
            fixture = libFxtr.pkgs.runCommand "fxtr-namespace-fixture" { } ''
              mkdir -p "$out"
              cat > "$out/fxtr_namespace.toml" <<'EOF'
              reserved_prefix = "root"
              name = "demo"
              EOF
              printf 'greeting\tdu7aezbzspqg4fsl3x5txk6q2vl3zkow6xc3yqofygkz2h5u4zwua\n' \
                > "$out/root.fxtr.tsv"
            '';
          in
          (libFxtr.tsvNamespaceExport {
            namespaceRoot = fixture;
            name = "demo";
          }).export;

        # mkFxtrFnBuilder over the py_testing venv: read the `builderInputLayer`
        # parent's `InputNote` and pin a derived `WrappedNote` (the `build`
        # entrypoint in `py_testing.builder_fixture`). Pure (no remote). The
        # py_testing test reads the wrapped note back + resolves its inner ref
        # through the parent layer.
        fxtr-fn-builder-check = fnBuilderLayer;

        # The report step is just a part-3 builder whose venv has
        # fxtr_report_builder and whose generic `env` wires the nix-built CLI.
        # Reads two parent-layer inputs (a plain entity + a part-3-derived one),
        # embeds them as RenderFigure refs via build_root_report, pins the
        # RootReport. Pure (no FOD); the CLI store path in `env` is the build dep.
        report-step-check = libFxtr.mkFxtrFnBuilder {
          venv = pyTestingVenv;
          entry = "py_testing.report_fixture:build";
          parents = {
            note = builderInputLayer;
            wrapped = fnBuilderLayer;
          };
          spec = {
            title = "Demo Report";
            summary = "A demo report over two layer inputs.";
          };
          env.FXTR_REPORT_BUILDER_CLI = "${libFxtr.reportBuilderCli}/bin/fxtr-report-builder";
          name = "report-step-check";
        };

        # Determinism gate for build_root_report (a JS toolchain is in the loop):
        # an independent build (different `name` → different drv) with identical
        # inputs. Content addressing means a byte-stable report → the SAME pinned
        # RootReport id; the test asserts report-step-check's `report` pin equals
        # this one's. (`nix build --rebuild` can't offload an x86_64-linux build
        # from a mac host, so two normal builds are the portable check.)
        report-step-check-alt = libFxtr.mkFxtrFnBuilder {
          venv = pyTestingVenv;
          entry = "py_testing.report_fixture:build";
          parents = {
            note = builderInputLayer;
            wrapped = fnBuilderLayer;
          };
          spec = {
            title = "Demo Report";
            summary = "A demo report over two layer inputs.";
          };
          env.FXTR_REPORT_BUILDER_CLI = "${libFxtr.reportBuilderCli}/bin/fxtr-report-builder";
          name = "report-step-check-alt";
        };

        # Same builder via the script form: a loose `.py` (owns `main`, calls
        # `nix_builder.main(build)`) over the same venv + parent.
        fxtr-script-builder-check = libFxtr.mkFxtrScriptBuilder {
          venv = pyTestingVenv;
          script = ./nix/py_testing/sample_build_script.py;
          parents.base = builderInputLayer;
          spec.label = "script";
          name = "fxtr-script-builder-check";
        };

        # Provenanced (pure / input-addressed) fn builder: pins the same derived
        # WrappedNote, but with a default build-step provenance_context minted on
        # ctx.scope. The py_testing test reads "wrapped" back as a *layered* id
        # and confirms the build c-node is a NixInputAddressedBuildProvenance
        # anchored on this layer's $out + carrying the declared provenanceInfo.
        fxtr-provenanced-fn-builder-check = libFxtr.mkFxtrFnBuilder {
          venv = pyTestingVenv;
          entry = "py_testing.builder_fixture:build";
          parents.base = builderInputLayer;
          spec.label = "provenanced";
          provenanced = true;
          provenanceInfo.desc = "py_testing provenanced builder check";
          name = "fxtr-provenanced-fn-builder-check";
        };

        # Throw path: provenanceInfo without provenanced is a static config error
        # (eval throw). The py_testing test asserts `nix build` fails with the
        # message. (The venv is never forced — the throw fires first.)
        provenance-info-requires-provenanced-check = libFxtr.mkFxtrFnBuilder {
          venv = pyTestingVenv;
          entry = "py_testing.builder_fixture:build";
          provenanceInfo.desc = "illegal without provenanced";
          name = "fxtr-provenance-info-requires-provenanced-check";
        };

        # Throw path: a provenanced fixed-output (FOD) builder with no
        # provenanceInfo is a static config error — a FOD has no safe $out anchor.
        provenanced-fod-requires-info-check = libFxtr.mkFxtrFnBuilder {
          venv = pyTestingVenv;
          entry = "py_testing.builder_fixture:build";
          provenanced = true;
          outputHash = libFxtr.pkgs.lib.fakeHash;
          name = "fxtr-provenanced-fod-requires-info-check";
        };

        # The report-builder CLI bundle (pure pnpm-in-nix). Building it exercises
        # pnpm.fetchDeps + esbuild-in-derivation + makeWrapper.
        report-builder-cli = libFxtr.reportBuilderCli;

        # Smoke check: run the CLI against the README envelope and assert
        # `{ok:true}` — IN the derivation (the bundle is x86_64-linux, so it runs
        # on the build sandbox, not a mac dev host). The py_testing test just
        # builds this target; a clean build means the CLI ran + responded ok.
        report-builder-cli-smoke =
          libFxtr.pkgs.runCommand "fxtr-report-builder-cli-smoke" { } ''
            envelope='{"source":"# H\n","inputs":{},"passthroughComponents":["RenderFigure","RenderLink"]}'
            result=$(printf '%s' "$envelope" | ${libFxtr.reportBuilderCli}/bin/fxtr-report-builder)
            echo "cli response: $result"
            echo "$result" | grep -qE '"ok": *true' || {
              echo "report-builder-cli smoke failed (no {ok:true}): $result" >&2
              exit 1
            }
            printf '%s' "$result" > "$out"
          '';

        # NOTE: `mkWasmService` (a consumer of `_mkLockedSitePackages`)
        # is exercised by `nix/sandbox_smoke/`'s own workdir flake, built
        # through `prepare_workdir_repo` in
        # `nix/py_testing/tests/test_sandbox_import.py` — the git-ref code
        # path. There is intentionally no in-repo guest-site-packages target
        # here. See projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage3_bundle_and_spawner.md.

        # esbuildRenderer smoke: compile a trivial TSX renderer (imports an
        # external + uses JSX) into an ESM `.mjs`. Confirms esbuild runs in
        # the build sandbox, the automatic-JSX transform works, and the
        # externals stay bare-specifier imports (no `node_modules` needed).
        # The fixture is generated inline so no source file is committed.
        renderer-esm-check =
          let
            fixture = libFxtr.pkgs.runCommand "fxtr-renderer-fixture" { } ''
              mkdir -p "$out"
              cat > "$out/index.tsx" <<'EOF'
              import { useMemo } from "react";
              export default function Demo() {
                const x = useMemo(() => 42, []);
                return <div>{x}</div>;
              }
              EOF
            '';
          in
          libFxtr.esbuildRenderer {
            name = "fxtr-renderer-esm-check";
            src = fixture;
          };

        # mkUiPreset transpose smoke: feed the helper plain-attrset stubs
        # (mimicking the renderer-drv / mkEntityFromPython shapes it passes
        # through untouched — no build) and emit `toJSON` of the transposed
        # table. Exercises multiple subtypes in one entry + sharding one type
        # across two entries with non-overlapping keys. The py_testing test
        # reads the JSON back and checks the structure.
        ui-preset-transpose-check =
          let
            typeA = {
              idString = "type-a";
              entity = {
                type = "fxtr_layer_entity";
                layer.drvPath = "/drv/ea.drv";
                layerKey = "ka";
              };
            };
            typeB = {
              idString = "type-b";
              entity = {
                type = "fxtr_layer_entity";
                layer.drvPath = "/drv/eb.drv";
                layerKey = "kb";
              };
            };
            table = libFxtr.mkUiPreset {
              # One module, two subtypes for two different types.
              multi = {
                renderer.drvPath = "/drv/r-multi.drv";
                subtypes = {
                  sa = {
                    forType = typeA;
                    forDispatchKeys = [
                      "u1"
                      "u2"
                    ];
                  };
                  sb = {
                    forType = typeB;
                    forDispatchKeys = [ "u3" ];
                  };
                };
              };
              # A second module shards typeA with a non-overlapping key.
              shard = {
                renderer.drvPath = "/drv/r-shard.drv";
                subtypes.sc = {
                  forType = typeA;
                  forDispatchKeys = [ "u4" ];
                };
              };
            };
          in
          libFxtr.pkgs.runCommand "fxtr-ui-preset-transpose-check"
            { json = builtins.toJSON table; }
            ''printf '%s' "$json" > "$out"'';

        # mkUiPreset conflict smoke: two entries claim the same
        # (type-id, dispatch-key) pair, so the helper `throw`s when `toJSON`
        # forces the table — `nix build` fails at eval. The test asserts the
        # non-zero exit + the conflict message.
        ui-preset-conflict-check =
          let
            typeA = {
              idString = "type-a";
              entity = {
                type = "fxtr_layer_entity";
                layer.drvPath = "/drv/ea.drv";
                layerKey = "ka";
              };
            };
            table = libFxtr.mkUiPreset {
              e1 = {
                renderer.drvPath = "/drv/r1.drv";
                subtypes.s1 = {
                  forType = typeA;
                  forDispatchKeys = [ "dup" ];
                };
              };
              e2 = {
                renderer.drvPath = "/drv/r2.drv";
                subtypes.s2 = {
                  forType = typeA;
                  forDispatchKeys = [ "dup" ];
                };
              };
            };
          in
          libFxtr.pkgs.runCommand "fxtr-ui-preset-conflict-check"
            { json = builtins.toJSON table; }
            ''printf '%s' "$json" > "$out"'';
      };
    };
}
