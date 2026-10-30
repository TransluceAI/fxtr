# streaming_scheduler renderer slice for the demo's `default` UI preset
# (stage 13 part 4 — nested channels). Three subtypes: the thin
# scheduler panel keyed by the streaming scheduler SERVICE's type id,
# the run-grid card keyed by the run-TAG entity type, and the per-cell
# streaming view keyed by the progress-TAG entity type — the latter two
# under the ChannelContentCard dispatch key (the run grid embeds the
# cell cards recursively through the same key).
{
  lib,
  streamingSchedulerType,
  streamingRunTagType,
  taskProgressTagType,
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
  streaming_scheduler = {
    renderer = lib.esbuildRenderer {
      src = ./.;
      externals = lib.workstationExternals ++ [ "@fxtr-ui/rd-types-channel" ];
    };
    subtypes = {
      streaming_scheduler = {
        forType = streamingSchedulerType;
        forDispatchKeys = [
          fullPanel
          blockLink
          inlineLink
          hoverPreview
        ];
      };
      streaming_run_tag = {
        forType = streamingRunTagType;
        forDispatchKeys = [ channelContentCard ];
      };
      task_progress_tag = {
        forType = taskProgressTagType;
        forDispatchKeys = [ channelContentCard ];
      };
    };
  };
}
