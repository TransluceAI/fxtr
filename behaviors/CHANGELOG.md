# behaviors changelog

## 0.0.1a6

- Requires fxtr 0.2.0a1 (`fxtr>=0.2.0a1,<0.3`), for `Partial` and step previews.
- `ChatRolloutPreview` shows a chat rollout in the viewer while it runs, through a step's
  `ctx.preview`. `run_chat_rollout` reports how a rollout ended in a last event,
  `RolloutEndEvent` (#285).
- `behaviors.judging`: `judge` asks a model to assess entities and returns a `Judgment`, with
  checked citations into the entities it read (#299, #320).
- ModelAPI: dialect codecs translate requests and responses for each provider, with structured
  output, more consistent reasoning, and a log of what a translation could not keep. Requested
  cache TTLs are a lower bound on every provider (#313, #320, #326).

## 0.0.1a5

- Supports fxtr 0.1 and 0.2 (`fxtr>=0.1.0a0,<0.3`). With the fxtr 0.2 viewer, a link can open
  a rollout or transcript at a selected message (#288).
- The guides cover model-driven user simulators, checkpointing, model providers, and
  provider-specific sampling and caching options (#261).

## 0.0.1a4

- Tagged `DynamicData` metadata, initialization and session checkpoints, model providers,
  base-model and pilot-steered user simulators, and provider adapter improvements (#220).
- The unused mashumaro dependency is removed (#223).
- Changed rollout records are versioned as v2, the current stored shape is required, and the
  type-versioning policy is documented (#230, #235, #236).
- Requires fxtr 0.1, with content-only entity IDs (#259).
