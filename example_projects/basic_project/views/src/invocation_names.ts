// The registered names of the example experiments' steps and workflows (the `name=` each
// `@step` / `@workflow` in `example_projects/basic_project/` declares). Invocation renderers are
// keyed on these, so they are spelled once here, the way `types.ts` spells the `$type` strings.

export const ROLLOUT_STEP = "rollout";
export const GRADE_STEP = "grade";
export const ROLLOUT_EXPERIMENT_WORKFLOW = "rollout_experiment";
export const SIMULATE_STEP = "simulate";
export const REWRITE_STEP = "rewrite";
export const REFINE_BIO_WORKFLOW = "refine_bio";
export const REFINE_BIOS_WORKFLOW = "refine_bios";
