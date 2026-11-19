// Every view of this package another view links to, declared apart from its component so a view's
// module links to others by these without importing them. `index.ts` registers the components.

import { InvocationPanel, declareView } from "fxtr-view";

/** The registered names of each experiment's workflow and of its conversation step. */
export const INSTRUCTIONS_IN_FORCE_WORKFLOW = "instructions_in_force.experiment";
export const INSTRUCTIONS_IN_FORCE_CONVERSATION_STEP = "instructions_in_force.conversation";
export const BELIEF_REINFORCEMENT_WORKFLOW = "belief_reinforcement.experiment";
export const BELIEF_REINFORCEMENT_CONVERSATION_STEP = "belief_reinforcement.conversation";

/** The instructions-in-force conversation step's panel, which a row among several conversations
 *  opens at its keys, to show that conversation alone. */
export const CONVERSATIONS = declareView(InvocationPanel, INSTRUCTIONS_IN_FORCE_CONVERSATION_STEP, "conversation");
