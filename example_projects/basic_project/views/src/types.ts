// The entity types of two of the example project's experiments (`rollout_experiment.py` and
// `bio_refinement.py` in `example_projects/basic_project/src/basic_project/`), as TypeScript: each
// mirrors the serialized form, with `EntityID<...>` in reference positions so a renderer that has
// cast its own id once gets typed ids for everything it references. Nothing here is checked at
// runtime; these are the assertions the renderers make about data tagged with these type names.

import type { EntityData, EntityID } from "fxtr-view";

export const MODEL_CONFIG = "org.example.rollout.ModelConfig";
export const SCENARIO = "org.example.rollout.Scenario";
export const RUBRIC = "org.example.rollout.Rubric";
export const MESSAGE = "org.example.rollout.Message";
export const TRANSCRIPT = "org.example.rollout.Transcript";
export const BIO = "org.example.refinement.Bio";
export const REWRITE_INSTRUCTIONS = "org.example.refinement.RewriteInstructions";
export const SIMULATED_TRANSCRIPT = "org.example.refinement.SimulatedTranscript";

/** How to sample from a model. */
export interface ModelConfigData extends EntityData {
  readonly $type: typeof MODEL_CONFIG;
  readonly name: string;
  readonly temperature: number;
}

/** A situation to put the model in. */
export interface ScenarioData extends EntityData {
  readonly $type: typeof SCENARIO;
  readonly title: string;
  readonly prompt: string;
}

/** What a judge looks for. */
export interface RubricData extends EntityData {
  readonly $type: typeof RUBRIC;
  readonly name: string;
  readonly criteria: readonly string[];
}

/** One turn of a transcript. Inline (a tagged dataclass, not a stored entity). */
export interface MessageData extends EntityData {
  readonly $type: typeof MESSAGE;
  readonly role: string;
  readonly content: string;
}

/** A rollout: the config and scenario it came from, by reference, and the turns. */
export interface TranscriptData extends EntityData {
  readonly $type: typeof TRANSCRIPT;
  readonly model: EntityID<ModelConfigData>;
  readonly scenario: EntityID<ScenarioData>;
  readonly messages: readonly MessageData[];
}

/** A simulated user: who they are and what they want, at some level of detail. */
export interface BioData extends EntityData {
  readonly $type: typeof BIO;
  readonly name: string;
  readonly persona: string;
  readonly detail: number;
}

/** What the rewriter is asked to do to a bio. */
export interface RewriteInstructionsData extends EntityData {
  readonly $type: typeof REWRITE_INSTRUCTIONS;
  readonly style: string;
  readonly target_detail: number;
}

/** A conversation between the simulated user of `bio` and `model`. */
export interface SimulatedTranscriptData extends EntityData {
  readonly $type: typeof SIMULATED_TRANSCRIPT;
  readonly model: EntityID<ModelConfigData>;
  readonly bio: EntityID<BioData>;
  readonly messages: readonly MessageData[];
}
