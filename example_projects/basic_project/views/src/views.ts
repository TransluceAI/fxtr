// Every view of this package another view links to, declared apart from its component so a view's
// module links to others by these without importing them. `index.ts` registers the components.

import { InvocationPanel, declareView } from "fxtr-view";
import { GRADE_STEP, ROLLOUT_STEP } from "./invocation_names";

/** The rollout step's panel, which a row among several rollouts opens at its keys, to show that
 *  rollout alone. */
export const ROLLOUTS = declareView(InvocationPanel, ROLLOUT_STEP, "basic-project-views.RolloutPanel");

/** The grade step's panel, which a row among several grades opens at its keys, to show that grade
 *  alone. */
export const GRADES = declareView(InvocationPanel, GRADE_STEP, "basic-project-views.GradePanel");
