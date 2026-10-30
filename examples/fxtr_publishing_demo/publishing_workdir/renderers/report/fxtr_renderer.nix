# Report renderer slice for the publishing demo's `demo` UI preset. The index.tsx
# re-exports `@fxtr-ui/reports` for the RootReport / SubReport type ids, so its
# `forType`s come from `lib.fxtr.entities.*` (the library type entities built from
# the fxtr_lib_type_ids stub venv — no fxtr_report_builder venv needed here). It
# imports beyond the base externals, so it compiles with an extended list (kept in
# sync with the host's installFxtrExternals by hand).
{ lib, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
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
  report = {
    renderer = lib.esbuildRenderer {
      src = ./.;
      externals = lib.workstationExternals ++ [
        "@fxtr-ui/reports"
        "@fxtr-ui/workstation"
      ];
    };
    subtypes = {
      root_report = {
        forType = lib.entities.rootReport;
        forDispatchKeys = shellKeys;
      };
      sub_report = {
        forType = lib.entities.subReport;
        forDispatchKeys = shellKeys ++ [ figure ];
      };
    };
  };
}
