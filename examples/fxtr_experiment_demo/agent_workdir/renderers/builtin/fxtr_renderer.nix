# Builtin renderer slice for the demo's `default` UI preset (the successor to
# this dir's old fxtr_renderer.json). Routes the workstation/agent-plugin +
# report + basic-type slots that the operator panel needs rendered. Its
# `forType`s all come from `lib.fxtr.entities.*` (the stage6pre1 library/builtin
# type entities), so this slice needs nothing but `lib`.
#
# The index.tsx here re-exports library frontend packages beyond the base
# externals set, so this renderer compiles with an extended `externals` list.
# TODO(daniel): figure out a better system for external workstation modules
# (so renderers don't each carry a divergent externals list — kept in sync with
# the frontend's installFxtrExternals by hand).
{ lib, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    streamEntry
    serviceInlineLink
    figure
    ;
  shellKeys = [
    fullPanel
    blockLink
    inlineLink
    hoverPreview
  ];
in
{
  builtin = {
    renderer = lib.esbuildRenderer {
      src = ./.;
      externals = lib.workstationExternals ++ [
        "@fxtr-ui/agent-plugin-renderer"
        "@fxtr-ui/rd-types-channel"
        "@fxtr-ui/rd-types-stream"
        "@fxtr-ui/reports"
        "@fxtr-ui/workstation"
      ];
    };
    subtypes = {
      agent_mcp_running_session = {
        forType = lib.entities.agentMcpRunningSession;
        forDispatchKeys = [ fullPanel ];
      };
      log_script_execution = {
        forType = lib.entities.logScriptExecution;
        forDispatchKeys = [ streamEntry ];
      };
      log_invoke_result = {
        forType = lib.entities.logInvokeResult;
        forDispatchKeys = [ streamEntry ];
      };
      log_invoke_error = {
        forType = lib.entities.logInvokeError;
        forDispatchKeys = [ streamEntry ];
      };
      log_callback_notification = {
        forType = lib.entities.logCallbackNotification;
        forDispatchKeys = [ streamEntry ];
      };
      workstation_service_placeholder = {
        forType = lib.entities.workstationServicePlaceholder;
        forDispatchKeys = [ serviceInlineLink ];
      };
      basic_type = {
        forType = lib.entities.basicType;
        forDispatchKeys = [
          blockLink
          inlineLink
        ];
      };
      root_report = {
        forType = lib.entities.rootReport;
        forDispatchKeys = shellKeys;
      };
      sub_report = {
        forType = lib.entities.subReport;
        forDispatchKeys = shellKeys ++ [ figure ];
      };
      channel_reader = {
        forType = lib.entities.channelReader;
        forDispatchKeys = shellKeys;
      };
    };
  };
}
