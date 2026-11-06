# behaviors changelog

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
