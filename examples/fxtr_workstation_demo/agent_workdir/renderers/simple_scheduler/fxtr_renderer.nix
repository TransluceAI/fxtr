# simple_scheduler renderer slice for the demo's `default` UI preset.
# Two subtypes since the stage-13 channel migration: the thin scheduler
# panel keyed by the scheduler SERVICE's pinned type id
# (simple_tasks.SIMPLE_SCHEDULER_TYPE_ID), and the run-grid card keyed
# by the run-TAG entity type (simple_tasks.SchedulerRunTag) under the
# ChannelContentCard dispatch key — the per-stream override of
# @fxtr-ui/rd-types-channel's generic card (hence the extra external).
{
  lib,
  schedulerType,
  schedulerRunTagType,
  keyIds,
}:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    channelContentCard
    ;
in
{
  simple_scheduler = {
    renderer = lib.esbuildRenderer {
      src = ./.;
      externals = lib.workstationExternals ++ [ "@fxtr-ui/rd-types-channel" ];
    };
    subtypes = {
      simple_scheduler = {
        forType = schedulerType;
        forDispatchKeys = [
          fullPanel
          blockLink
          inlineLink
          hoverPreview
        ];
      };
      scheduler_run_tag = {
        forType = schedulerRunTagType;
        forDispatchKeys = [ channelContentCard ];
      };
    };
  };
}
