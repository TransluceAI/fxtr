{
  description = "fxtr_experiment_demo workdir — the `default` UI preset for the experiment dashboard demo.";

  # The fxtr library flake. Placeholder in the committed template;
  # `prepare_workdir_repo` (run_experiment_demo.py --auto-workdir / tests)
  # rewrites it to a pinned git ref in the runnable workdir copy.
  inputs.fxtr.url = "@FXTR_LIB_FLAKE@"; # fxtr-lib-sub: flake-input

  outputs =
    { fxtr, ... }:
    let
      lib = fxtr.lib.fxtr;

      # ONE aggregator venv (packages/{experiment_notes,experiment_demo_helper}
      # made importable; fxtr2-only, light) — the single mkVenv root for the
      # demo-owned typeEntities. See type_entities/pyproject.toml.
      typeEntitiesVenv = lib.mkVenv {
        name = "fxtr-experiment-demo-type-entities-venv";
        workspaceRoot = ./type_entities;
      };

      # forType = { idString; entity }. The hand-pasted idString is the preset
      # key + what the dispatcher's eager validation checks the built
      # typeEntity against (print via `<TYPE_ID>.id_string()`).
      mkType = idString: fromPython: {
        inherit idString;
        entity = lib.mkEntityFromPython {
          venv = typeEntitiesVenv;
          inherit fromPython;
        };
      };
      expNoteType = mkType "dozuzg2yishung364mrbkkqv57efm5mglelxg42tzxqu3uf6niewa" "experiment_notes:EXP_NOTE_TYPE_ID";
      # The host-side demo-driver SERVICE type id (packages/experiment_demo_helper).
      experimentDriverType = mkType "djzunc4dwqcj7s2gldq47xpn4nmu6oyxw7pyt2vgdyztx3dskj44q" "experiment_demo_helper:EXPERIMENT_DEMO_HELPER_TYPE_ID";

      # The behavior scenario's entity types (packages/behavior_research_types).
      behaviorTypes = {
        transcript = mkType "d5ggmuq4plvq5mhpijaxq2f6ks6dw4mvqm6ooyw6uyq6w3urd6x2q" "behavior_research_types.entities:TRANSCRIPT_TYPE_ID";
        subjectModelConfig = mkType "dhjjybycc4hcl7g3pyaio7szdd6u4qay7xsk4mw6gbl6aigun74zq" "behavior_research_types.entities:SUBJECT_MODEL_CONFIG_TYPE_ID";
        userSimConfig = mkType "dco7qtsseyjjzk3m5z5heunqfb7aq7jwl3opqierzlr3frvozdssa" "behavior_research_types.entities:USER_SIM_CONFIG_TYPE_ID";
        judgeConfig = mkType "d4eqcezrrc73o7q3zu6ujx4o7n24gkus4qdqmw2hkxg65472c5nya" "behavior_research_types.entities:JUDGE_CONFIG_TYPE_ID";
        rolloutConfig = mkType "d4klckpm32wossaj6xpzggzw33hyy6ckyxz25sh242xtoyf3c3smq" "behavior_research_types.entities:ROLLOUT_CONFIG_TYPE_ID";
        sweepConfig = mkType "db6enzafttkzv2gnts2hv3bc2grj2bqfudi36kqpi3hdmcr355d3q" "behavior_research_types.entities:SWEEP_CONFIG_TYPE_ID";
        judgeResult = mkType "dd67zym3jk7bwjgnw44qcnutvaekvecgjnntrhb24fkq4ftel33xq" "behavior_research_types.entities:JUDGE_RESULT_TYPE_ID";
        analysisTable = mkType "dtr2t34pxibynh2ymvi35pozhhakfiteosu7p3wfjbpnbzpp3kn7a" "behavior_research_types.entities:ANALYSIS_TABLE_TYPE_ID";
        serviceKey = mkType "dpukhxkoiyartkiy2ka4h72ybhr3svogmj7gwehkzijyqre7b2loq" "behavior_research_types.entities:SERVICE_KEY_TYPE_ID";
        userSimImprovementRequest = mkType "dpd32rjrr7puhs7zpk25fqwfszjvawub2e5io52mx5hn4ew7ownpq" "behavior_research_types.entities:USER_SIM_IMPROVEMENT_REQUEST_TYPE_ID";
        improvementEvent = mkType "dkktspkdqk5i3puc5gitojgqanzusfssljsct64vbznujwf4isk6q" "behavior_research_types.entities:IMPROVEMENT_EVENT_TYPE_ID";
        userSimImprovementResult = mkType "do7q7ixk5ll2xmuhb2pbebcbicvacyzgyskfafn5m2bwiw4gi4beq" "behavior_research_types.entities:USER_SIM_IMPROVEMENT_RESULT_TYPE_ID";
      };
      # The behavior scenario's driver SERVICE type id (packages/behavior_demo_helper).
      behaviorDriverType = mkType "di6nchc2pxd7t3ro3f55lejwgzddcimxbian6x6niogexhbyfptyq" "behavior_demo_helper:BEHAVIOR_DEMO_HELPER_TYPE_ID";

      # Dispatch-key UUIDs shared across the renderer slices (one place,
      # `inherit`ed into each slice).
      keyIds = {
        fullPanel = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234";
        blockLink = "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901";
        inlineLink = "c4d5e6f7-8901-4a12-9b34-5d6e7f890123";
        hoverPreview = "d5e6f7a8-9012-4a23-9b45-6e7f89012345";
        streamEntry = "4b2e1a3c-7d68-4e91-a25f-9c3d8b0e1f7a";
        serviceInlineLink = "f1e2d3c4-b5a6-4798-8c1d-2e3f405162a7";
        figure = "f4a3e2c1-9d0b-4d8a-9e2c-5f1b3a8e0d12";
      };

      # Each renderer dir carries its own `fxtr_renderer.nix` slice. Merge
      # them into one mkUiPreset call — entry names are unique so `//` is the
      # right merge; mkUiPreset still throws on a duplicate
      # (type-id, dispatch-key) across slices. The fxtr_experiment renderers
      # (dashboard + frame + stored-config entities) come from the repo lib
      # by reference — their source lives in the fxtr repo
      # (projects/fxtr_experiment/frontend), not in this workdir.
      rendererEntries =
        (import ./renderers/builtin/fxtr_renderer.nix { inherit lib keyIds; })
        // (import ./renderers/exp_note/fxtr_renderer.nix { inherit lib expNoteType keyIds; })
        // (import ./renderers/experiment_driver/fxtr_renderer.nix {
          inherit lib experimentDriverType keyIds;
        })
        // (import ./renderers/behavior_entities/fxtr_renderer.nix {
          inherit lib behaviorTypes keyIds;
        })
        // (import ./renderers/behavior_driver/fxtr_renderer.nix {
          inherit lib behaviorDriverType keyIds;
        })
        // (lib.experimentRenderers { inherit keyIds; });
    in
    {
      # UI preset (CODE, referenced by git commit). Auto-loaded on boot by the
      # demo's NixConfigurableDispatchImpl; re-push after editing a renderer /
      # the flake via set_preset(current_commit(), "fxtr.uiPresets.default").
      fxtr.uiPresets.default = lib.mkUiPreset rendererEntries;
    };
}
