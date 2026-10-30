# Renderer slice for every `behavior_research_types` entity — one esbuild
# module, one subtype per entity type. Receives `behaviorTypes` (an attrset
# of forType values, built from the type_entities aggregator venv) from the
# root flake. Base externals suffice.
{ lib, behaviorTypes, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
  shellKeys = [
    fullPanel
    blockLink
    inlineLink
    hoverPreview
  ];
  mkSubtype = forType: {
    inherit forType;
    forDispatchKeys = shellKeys;
  };
in
{
  behavior_entities = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes = {
      transcript = mkSubtype behaviorTypes.transcript;
      subject_model_config = mkSubtype behaviorTypes.subjectModelConfig;
      user_sim_config = mkSubtype behaviorTypes.userSimConfig;
      judge_config = mkSubtype behaviorTypes.judgeConfig;
      rollout_config = mkSubtype behaviorTypes.rolloutConfig;
      sweep_config = mkSubtype behaviorTypes.sweepConfig;
      judge_result = mkSubtype behaviorTypes.judgeResult;
      analysis_table = mkSubtype behaviorTypes.analysisTable;
      service_key = mkSubtype behaviorTypes.serviceKey;
      user_sim_improvement_request = mkSubtype behaviorTypes.userSimImprovementRequest;
      improvement_event = mkSubtype behaviorTypes.improvementEvent;
      user_sim_improvement_result = mkSubtype behaviorTypes.userSimImprovementResult;
    };
  };
}
