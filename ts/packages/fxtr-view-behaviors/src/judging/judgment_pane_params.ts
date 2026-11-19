// The param a judgment's panel reads to show one of its parts in place of the whole, and the
// params of the links that open each part. A link opens the judgment alone with one of them, so
// the part opens in a pane of its own.

/** The param naming the part: `judge_conversation` (the judge's whole conversation with the model,
 *  and the arguments `judge` was given) or `output_schema`. */
export const JUDGMENT_PART = "judgment_part";

/** The params that open a judgment's conversation. */
export const JUDGE_CONVERSATION_PARAMS = Object.freeze({ [JUDGMENT_PART]: "judge_conversation" });

/** The params that open a judgment's output schema. */
export const SCHEMA_PARAMS = Object.freeze({ [JUDGMENT_PART]: "output_schema" });
