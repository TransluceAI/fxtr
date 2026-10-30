{
  description = "fxtr publishing demo: a provenanced Note → Stats → SubReport → RootReport graph, built by nix and served (with provenance) over the stateless HTTP stack.";

  # The fxtr library flake. Placeholder in the committed template;
  # `prepare_workdir_repo` (scaffold_workdir.py / tests) rewrites it to a pinned
  # git ref in the scaffolded workdir copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;

      # One venv (fxtr2 + demo_types + fxtr_report_builder) backs every builder
      # step AND mkEntityFromPython for the Note/Stats `forType` entities. See
      # builders/pyproject.toml.
      venv = lib.mkVenv {
        name = "publishing-demo-builders-venv";
        workspaceRoot = ./builders;
      };

      # forType = { idString; entity }. The hand-pasted idString is the preset key
      # + what the dispatcher's eager validation checks the built typeEntity
      # against (print: `python -c "import demo_types; print(demo_types.Note.fxtr_type.id_string())"`).
      mkType = idString: fromPython: {
        inherit idString;
        entity = lib.mkEntityFromPython {
          inherit venv fromPython;
        };
      };
      noteType = mkType "dmxpiialaqbei2wigvc42c45ed2o2n4l4d7z4timst7bljlivat2a" "demo_types:Note.fxtr_type";
      statsType = mkType "dhwo4mucguvovfpqejfbdnv5tcghopjs5set7rkc6myzfknozrsdq" "demo_types:Stats.fxtr_type";

      # Dispatch-key UUIDs shared across the renderer slices (the workstation
      # shell keys + the @fxtr-ui/reports Figure key).
      keyIds = {
        fullPanel = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234";
        blockLink = "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901";
        inlineLink = "c4d5e6f7-8901-4a12-9b34-5d6e7f890123";
        hoverPreview = "d5e6f7a8-9012-4a23-9b45-6e7f89012345";
        figure = "f4a3e2c1-9d0b-4d8a-9e2c-5f1b3a8e0d12";
      };

      # The seeded notes enter the build as preload FODs (the only networked
      # build steps): each fetches one committed tsv pin's bytes via the build
      # span's rehost into a one-pin layer. Hashes come from `fod_lock.toml`
      # (managed by `fxtr_nix update-hashes`); slots are `fxtr.preloads.note_<k>`.
      fodLock = lib.fodLockReader { lockFile = ./fod_lock.toml; };
      notePreload = key: lib.entityFromTsvPin {
        namespaceRoot = ./.;
        inherit key;
        outputHash = fodLock key;
      };
      noteLayers = {
        note_0 = (notePreload "note_0").layer;
        note_1 = (notePreload "note_1").layer;
        note_2 = (notePreload "note_2").layer;
      };

      # The report CLI wired into the MDX builders via the generic `env`
      # (resolve_report_cli_command reads it). Nix has no report-builder-specific
      # code; the CLI is a pinned pnpm-in-nix bundle.
      reportCliEnv.FXTR_REPORT_BUILDER_CLI = "${lib.reportBuilderCli}/bin/fxtr-report-builder";

      # postprocess: notes → Stats. Pure (reads the note layers); provenanced, so
      # the Stats reads back as a layered (data, prov) id.
      statsLayer = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "publishing_builders.postprocess:build";
        parents = noteLayers;
        provenanced = true;
        provenanceInfo.desc = "publishing demo: aggregate stats over the seeded notes";
        name = "publishing-demo-stats";
      };

      # subreport: notes + stats → an MDX SubReport (figures: stats + two notes).
      subreportLayer = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "publishing_builders.subreport:build";
        parents = noteLayers // { stats = statsLayer; };
        spec.label = "Notes overview";
        env = reportCliEnv;
        provenanced = true;
        provenanceInfo.desc = "publishing demo: MDX subreport over the notes + stats";
        name = "publishing-demo-subreport";
      };

      # report: notes + stats + subreport → the served MDX RootReport. Its figure
      # refs carry provenance (the nested-provenance carrier).
      reportLayer = lib.mkFxtrFnBuilder {
        inherit venv;
        entry = "publishing_builders.report:build";
        parents = noteLayers // {
          stats = statsLayer;
          subreport = subreportLayer;
        };
        spec = {
          title = "Publishing demo report";
          summary = "A nix-built report over seeded notes, an aggregate, and a subreport.";
        };
        env = reportCliEnv;
        provenanced = true;
        provenanceInfo.desc = "publishing demo: MDX root report over the whole graph";
        name = "publishing-demo-report";
      };

      # The renderer slices (note + stats custom, report re-export), merged into
      # one mkUiPreset call — entry names are unique so `//` is the right merge;
      # mkUiPreset still throws on a duplicate (type-id, dispatch-key) claim.
      rendererEntries =
        (import ./renderers/note/fxtr_renderer.nix { inherit lib noteType keyIds; })
        // (import ./renderers/stats/fxtr_renderer.nix { inherit lib statsType keyIds; })
        // (import ./renderers/report/fxtr_renderer.nix { inherit lib keyIds; });
    in
    {
      # The served report — a provenanced layer entity (layered on the wire, with
      # provenance exposed when served with provenanced=True).
      fxtr.entities.report = {
        type = "fxtr_layer_entity";
        layer = reportLayer;
        layerKey = "report";
      };

      # The seeded notes published as a pure tsv-namespace export (bare
      # "demo:note_<k>" pins; the namespace flow is provenance-stripping).
      fxtr.namespaces.demo = lib.tsvNamespaceExport {
        namespaceRoot = ./.;
        name = "demo";
      };

      # Hash-locked preload slots for `fxtr_nix update-hashes` (lockKey = note_<k>).
      fxtr.preloads = noteLayers;

      # The flake-built renderer preset; `fxtr_nix serve` builds it into the
      # dynamic-dispatch service.
      fxtr.uiPresets.demo = lib.mkUiPreset rendererEntries;
    };
}
