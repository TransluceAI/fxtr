// Every view of this package another view links to, declared apart from its component so a view's
// module links to others by these without importing them. `index.ts` registers the components.

import { InvocationPanel, declareView } from "fxtr-view";

export const OPENAI_CONVERSATION = "openai_rollout.openai_conversation";
export const OPENAI_EXPERIMENT = "openai_rollout.openai_experiment";

/** The conversation step's panel, which a row among several conversations opens at its keys, to
 *  show that conversation alone. */
export const CONVERSATIONS = declareView(InvocationPanel, OPENAI_CONVERSATION, "conversation");
