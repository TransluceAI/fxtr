import {
  EntityHoverPreview,
  EntityInlineLink,
  InvocationPanel,
  EntityPanel,
  renderer,
  type AnyRenderer,
} from "fxtr-view";
import * as invocations from "./invocations";
import { REFINE_BIO_WORKFLOW } from "./invocation_names";
import * as refinement from "./refinement";
import * as rollout from "./rollout";
import { GRADES, ROLLOUTS } from "./views";
import { BIO, MODEL_CONFIG, REWRITE_INSTRUCTIONS, RUBRIC, SCENARIO, SIMULATED_TRANSCRIPT, TRANSCRIPT } from "./types";

export * from "./types";
export * from "./invocation_names";
export * from "./rollout";
export * from "./refinement";
export { GradePanel, RefineBioRoundsPanel, RolloutPanel } from "./invocations";

/** The example experiments' renderers, for a registry to concatenate: a panel, an inline link,
 *  and a hover preview per entity type, then the invocation panels, which show any number of
 *  invocations. */
export const demoRenderers: readonly AnyRenderer[] = [
  renderer(EntityPanel, MODEL_CONFIG, "basic-project-views.ModelConfigPanel", rollout.ModelConfigPanel),
  renderer(EntityInlineLink, MODEL_CONFIG, "basic-project-views.ModelConfigInlineLink", rollout.ModelConfigInlineLink),
  renderer(EntityHoverPreview, MODEL_CONFIG, "basic-project-views.ModelConfigPreview", rollout.ModelConfigPreview),

  renderer(EntityPanel, SCENARIO, "basic-project-views.ScenarioPanel", rollout.ScenarioPanel),
  renderer(EntityInlineLink, SCENARIO, "basic-project-views.ScenarioInlineLink", rollout.ScenarioInlineLink),
  renderer(EntityHoverPreview, SCENARIO, "basic-project-views.ScenarioPreview", rollout.ScenarioPreview),

  renderer(EntityPanel, RUBRIC, "basic-project-views.RubricPanel", rollout.RubricPanel),
  renderer(EntityInlineLink, RUBRIC, "basic-project-views.RubricInlineLink", rollout.RubricInlineLink),
  renderer(EntityHoverPreview, RUBRIC, "basic-project-views.RubricPreview", rollout.RubricPreview),

  renderer(EntityPanel, TRANSCRIPT, "basic-project-views.TranscriptPanel", rollout.TranscriptPanel),
  renderer(EntityInlineLink, TRANSCRIPT, "basic-project-views.TranscriptInlineLink", rollout.TranscriptInlineLink),
  renderer(EntityHoverPreview, TRANSCRIPT, "basic-project-views.TranscriptPreview", rollout.TranscriptPreview),

  renderer(EntityPanel, BIO, "basic-project-views.BioPanel", refinement.BioPanel),
  renderer(EntityInlineLink, BIO, "basic-project-views.BioInlineLink", refinement.BioInlineLink),
  renderer(EntityHoverPreview, BIO, "basic-project-views.BioPreview", refinement.BioPreview),

  renderer(EntityPanel, REWRITE_INSTRUCTIONS, "basic-project-views.RewriteInstructionsPanel", refinement.RewriteInstructionsPanel),
  renderer(EntityInlineLink, REWRITE_INSTRUCTIONS, "basic-project-views.RewriteInstructionsInlineLink", refinement.RewriteInstructionsInlineLink),
  renderer(EntityHoverPreview, REWRITE_INSTRUCTIONS, "basic-project-views.RewriteInstructionsPreview", refinement.RewriteInstructionsPreview),

  renderer(EntityPanel, SIMULATED_TRANSCRIPT, "basic-project-views.SimulatedTranscriptPanel", refinement.SimulatedTranscriptPanel),
  renderer(EntityInlineLink, SIMULATED_TRANSCRIPT, "basic-project-views.SimulatedTranscriptInlineLink", refinement.SimulatedTranscriptInlineLink),
  renderer(EntityHoverPreview, SIMULATED_TRANSCRIPT, "basic-project-views.SimulatedTranscriptPreview", refinement.SimulatedTranscriptPreview),

  renderer(ROLLOUTS, invocations.RolloutPanel),
  renderer(GRADES, invocations.GradePanel),
  renderer(InvocationPanel, REFINE_BIO_WORKFLOW, "basic-project-views.RefineBioRoundsPanel", invocations.RefineBioRoundsPanel),
];
