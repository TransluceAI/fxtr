# lib.fxtr.entities — ready-made `forType` values ({ idString; entity; }) for
# the library / builtin type ids the demo's `builtin/` renderer routes, so a
# consumer flake imports them instead of standing up its own venv over the
# library packages (the assembly demo's agent panel + log-event + report
# slots). Drops straight into a `mkUiPreset` entry's `forType`.
#
# Each `entity` is built by `mkEntityFromPython` over the **stub package**
# `nix/fxtr_lib_type_ids` — which re-declares each type id via
# `define_type(SAME_NS, SAME_LABEL)` and depends only on fxtr2, so it's
# content-identical to the canonical type yet builds from a light venv (no
# fxtr_workstation_mcp / fxtr_report_builder closure). The idStrings are
# content-stable literals (verified against the canonical define_types); the
# nix/git dispatcher's eager validation builds `entity` + asserts the literal
# matches, so a stale paste or a drifted stub is caught loudly.
#
# Design (under projects/fxtr_workstation/plans/phase0/part5_nix_workdir/):
#   stage6pre1_builtin_and_lib_entities.md + design_notes/06_ui_presets.md.
{
  mkVenv,
  mkEntityFromPython,
  builtinEntity,
}:
let
  # Light fxtr2-only venv over the stub package (its uv.lock pins fxtr2 + deps).
  typeIdsVenv = mkVenv {
    name = "fxtr-lib-type-ids-venv";
    workspaceRoot = ../fxtr_lib_type_ids;
  };

  # forType = { idString; entity = built typeEntity layer }. `fromPython` names
  # a stub constant; `layerKey` is the pin name inside the produced layer.
  forType = idString: pyConst: layerKey: {
    inherit idString;
    entity = mkEntityFromPython {
      venv = typeIdsVenv;
      fromPython = "fxtr_lib_type_ids:${pyConst}";
      inherit layerKey;
    };
  };
in
{
  # fxtr_workstation_mcp types (the agent panel + the typed event log + the
  # service-substitution placeholder).
  agentMcpRunningSession = forType "db7cfuzhcj6744exem23apyoyg3ayx5rs7uvjtkjnovs37nnvm4eq" "AGENT_MCP_RUNNING_SESSION_TYPE_ID" "agent_mcp_running_session";
  logScriptExecution = forType "dzrgduvojmh2le7frxjs6s66bza4rgrrte2weeprqtys4vaorjq6q" "LOG_SCRIPT_EXECUTION_TYPE_ID" "log_script_execution";
  logInvokeResult = forType "djhhw6diwxpj7xypu5rlomohathz7ksdn2tiocteebtcstmu4yaxq" "LOG_INVOKE_RESULT_TYPE_ID" "log_invoke_result";
  logInvokeError = forType "d5pv2hlapgb6bkegmdwun7rpprqyfhwwi2siitz6lta6w6pp7ohra" "LOG_INVOKE_ERROR_TYPE_ID" "log_invoke_error";
  logCallbackNotification = forType "dvguvnwsj4327k4fqv3jz46lan36bfgdygt2qsfffrvkd7g3fre7q" "LOG_CALLBACK_NOTIFICATION_TYPE_ID" "log_callback_notification";
  workstationServicePlaceholder = forType "duzfdis2ybod5jav4d3ktwvnuwr2qvkc7o7lddqhe5c72qcwniqca" "WORKSTATION_SERVICE_PLACEHOLDER_TYPE_ID" "workstation_service_placeholder";

  # fxtr_report_builder types (the RootReport / SubReport renderers).
  rootReport = forType "du7aezbzspqg4fsl3x5txk6q2vl3zkow6xc3yqofygkz2h5u4zwua" "ROOT_REPORT_TYPE_ID" "root_report";
  subReport = forType "d4nqychcnfoxwdou2ayifrx3w4rcjmmhgmsfmdkwr5zbe3rgezkdq" "SUB_REPORT_TYPE_ID" "sub_report";

  # fxtr2.common.channels: the fixed service type id shared by every output
  # channel's *reader* (the @fxtr-ui/rd-types-channel renderers route on it;
  # per-stream identity lives in the channel's *tag* entity type).
  channelReader = forType "dfnm2w6whc6p6qquontsj7uv6lfj5vmhrf4lzkr6x36ekmgzii4ba" "CHANNEL_READER_TYPE_ID" "channel_reader";

  # fxtr_experiment types (the experiment dashboard + frame + stored-config
  # renderers routed by lib.fxtr.experimentRenderers). user_capability is a
  # SERVICE type id; the other three are entity types.
  experimentUserCapability = forType "dpk4lldwrt6vws64rxlxqrscmnr6zwu2jkzjsyfshmuxpb4o6radq" "EXPERIMENT_USER_CAPABILITY_TYPE_ID" "experiment_user_capability";
  experimentFrame = forType "dgryl6ovr2364hrc4dgnbimcmexxkg7iniwydcdzqhj6n5365ik7a" "EXPERIMENT_FRAME_TYPE_ID" "experiment_frame";
  experimentGridConfig = forType "d4ogp2tmcknjyf7jgjxqnfcaw3bcwsun3dmmce6xlw45efji3xhbq" "EXPERIMENT_GRID_CONFIG_TYPE_ID" "experiment_grid_config";
  experimentSliceTemplate = forType "dzxosrmzceiu2i3bnxivfy5equooxbmnynrcgjjo2socixzhxzscq" "EXPERIMENT_SLICE_TEMPLATE_TYPE_ID" "experiment_slice_template";

  # The `!basic_type` fallback: a primitive FxtrID, no layer to build.
  basicType = {
    idString = "!basic_type";
    entity = builtinEntity { idString = "!basic_type"; };
  };
}
