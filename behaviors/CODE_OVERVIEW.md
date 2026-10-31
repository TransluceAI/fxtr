# Behaviors — code overview

`behaviors` is a Python 3.12.4+ package for running chat models against context policies. It depends
on `fxtr` for typed entity storage. Provider SDKs (`behaviors[models]`) and the Docent SDK
(`behaviors[docent]`) are optional.

`README.md` covers setup and usage and links to the documentation's model selection guide,
`../docs/behaviors/guides/choosing-models.mdx`, for listing and searching OpenAI, Anthropic,
and OpenRouter model IDs and selecting adapters. Model discovery uses provider APIs directly;
the library has no model catalog. How a project uses the library is documented on the docs site,
`docs/behaviors/` (its guides and the API overview, `reference/api.mdx`), whose pages the
behaviors skill (`.agents/skills/behaviors/SKILL.md`) links and ships copies of with fxtr.

behaviors depends on fxtr; fxtr has no dependency on behaviors. The shared conversation views in
the experiment viewer are `ts/packages/fxtr-view-behaviors` (see `ts/CODE_OVERVIEW.md`), and
`example_projects/behavior_rollouts/` is the maintained example project using both.

## Concepts

Structural protocols separate one provider call (`ModelAPI`), a stateful conversation
(`ChatSessionModel` / `ChatSession`), and the environment controlling each model-visible turn
(`ContextPolicy` / `ContextPolicySession`). `run_chat_rollout` alternates policy and model turns
and returns a storable `ChatRollout`, with streaming events, configurable failure recording, and
session cleanup. Scripted users, tool policies, and async generators compose into context policies.
Serializable sessions expose checkpoints. Data records are fxtr entities and tagged
dataclasses, so rollouts store in any fxtr storage and are content-addressed like any other value.
Producer-typed slots (turn and rollout metadata, the recorded initialization, session checkpoints)
are fxtr `DynamicData`: a tagged value held in serialized form until the reader names its type.
Provider implementations
(Anthropic Messages, OpenAI Chat Completions-compatible, and OpenAI Responses) require
`behaviors[models]`; core imports and rollout logic do not require SDKs.

## Source map

The importable package lives in `src/behaviors/`:

- `types/` defines frozen data records: content blocks, messages, tools and cache hints,
  model requests/responses, sampling and reasoning parameters, failures, retry settings,
  streaming events, rollout turns/status, session initializations, and transcript trees.
  `__init__.py` exposes the public vocabulary. `ChatRollout`, session initializations,
  `Transcript`, `TranscriptGroup`, and `AgentRun` are fxtr entities. Union members use
  `TaggedDataclass` tags with `org.transluce.behaviors.<Class>.v<N>` identifiers.
  `namespace.py` defines the type-versioning rules and qualifies versioned names with
  `behaviors_type`, also exported from `behaviors.types`. `json.py` re-exports
  `JSONValue` and `JSONObject` from `fxtr.core.json`; fxtr checks JSON on save and load, and
  field types on load. Cacheable content and tools are explicit `TextContent`, `Content`, and
  `ToolDefinition` union variants, defined in `content.py` and `tool.py` next to their base
  classes; `cache_control.py` holds the `CacheControl` breakpoint both use. `rollout.py` also defines `find_metadata`, which decodes the first
  metadata entry of a requested type. Transcript/group trees are
  embedded inside their parents and can also be stored independently.
- `interfaces/model_apis.py` defines `ModelAPI.generate` and `StreamingModelAPI.generate_with_events`.
  Calls return a `ModelResponse` or a categorized `ModelCallFailure`; implementation errors raise
  `ModelError`. Retry policy belongs to the caller.
- `interfaces/chat_session_models.py` defines model/session protocols, including streaming and
  checkpointable (serializable) variants. A model starts a session from an optional initialization; its session
  accumulates conversation state while generating model turns.
- `interfaces/context_policies.py` defines context policy sessions, user simulator sessions, and
  tool policy sessions. Each has a serializable variant. User simulators and context policies can
  return `EndRollout`; tool policies provide tool results. A user simulator session can be handed a
  `user_turn_override` to send in place of the turn it would have produced.
- `interfaces/model_apis.py` also defines `ModelProvider`, which looks up a `ModelAPI` by model id.
- `interfaces/projections.py` defines `TranscriptProjection` and `AgentRunProjection` protocols
  for views of ordinary objects. `ChatRolloutTranscriptProjection` in `rollouts/chat.py` flattens
  alternating turns while retaining the initial system message and unanswered context turns.
- `implementations/docent/` provides optional SDK integration (`behaviors[docent]`). `conversions.py`
  maps messages and transcript trees to public SDK models; `reading_step.py` replays readings
  locally through an injected `ModelAPI`, with prompt/citation helpers in `_reading_step_internals.py`.
- `interfaces/_sessions.py` provides cancellation-shielded session cleanup for rollout ownership.
  Combined policies also shield cleanup and close acquired sessions if startup fails.
- `implementations/models/from_api.py` adapts a ModelAPI to a stateful chat session. It accumulates
  context, records each call's `ModelResponse` as turn metadata, applies retry configuration, and
  captures/restores session state.
- `implementations/models/retries.py` retries configured failure categories, using AnyIO delays and
  stream-restart events. `overrides.py` wraps a model API with sampling overrides.
- `implementations/models/anthropic.py`, `openai_compatible.py`, and `openai_responses.py` implement
  Anthropic Messages, OpenAI Chat Completions-compatible endpoints, and OpenAI Responses. They render
  native provider requests and assemble streams into the shared vocabulary. `openai_common.py`
  shares reasoning and error translation. Import provider modules explicitly to load their SDKs.
  The Anthropic adapter sends set sampling fields and `SamplingParams.extra` unchanged through
  `extra_body`, leaving model support and parameter combinations for the provider to validate.
  Unset typed fields are omitted; `extra` overrides typed values and preserves explicit JSON nulls.
  Each provider module also has a `*Provider` (`ModelProvider`) that builds and caches one API per
  model id over a shared client; `providers.py` has the vendor-agnostic `SingleModelProvider` and
  `ModelOverrides`.
- `implementations/context_policies/scripted.py` sends fixed user messages and checkpoints the
  remaining messages.
- `implementations/context_policies/base_model_user_simulator.py` samples user messages from a base
  model's completions endpoint, prompting it with a document rendered by `base_model_prompt.py`
  (requires the `models` extra). `pilot_steered_user_simulator.py` wraps any serializable user
  simulator: it restores several copies of the base session, takes a candidate turn from each, and
  lets a pilot chat model (prompted by `pilot_prompt.py`) choose among them or write its own. Both
  attach `SimulatedUserText` metadata; steered turns also carry a `PilotSteering` record.
- `implementations/context_policies/lifts.py` lifts user simulators or tool policies into full
  context policies. `user_and_tools.py` combines them, dispatching tool calls to the tool policy and
  consulting the user simulator on turns without tool calls. `no_tools.py` supplies the no-tools
  policy; `_shared.py` contains shared tool-call extraction and simulated-user text helpers.
- `implementations/context_policies/generator.py` adapts an async generator into a context policy,
  either through `GeneratorContextPolicy` or `context_policy_from_generator`.
  Generator policies keep live generator state and do not implement checkpoints.
- `rollouts/chat.py` alternates policy and model turns, emits ordered events, records configured
  failures, enforces a turn limit, and closes sessions on exit. `ChatRollout` retains the unanswered
  context turn if a model call fails. Its `initialization` field records the model's
  initialization as `DynamicData`; consumers decode it with the type they expect. The transcript
  projection currently renders only `SetSystemMessage`. Stored rollouts use the strict dataclass
  converter: every field, including a null `initialization`, is present.

Entity serialization uses the fxtr storage API: `await storage.store(entity)` returns a `BoundID`,
and `await storage.load(reference)` reconstructs the entity. Session checkpoints are `DynamicData`
wrapping each implementation's own tagged state class; they can be stored like any value, or
written as plain JSON (`thaw_fxtr_data(state.data)`) and read back with `DynamicData.from_data`.
Restoring decodes strictly through the fxtr converter, raising `ConversionError` on a missing,
mistyped, or extra field, or on another implementation's state.
Provider rendering explicitly translates data records into provider wire parameters.

## Tests

- `tests/test_types.py` and `test_rollout_types.py`: data vocabulary, strict fxtr storage round
  trips, and agreement between Python's type tags and the shared conversation viewer's constants.
- `tests/test_serialization.py`: storage limits, strict embedded configurations, recursive groups,
  and cacheable variants across storage and JSON-persisted checkpoints and metadata.
- `tests/test_rollout_loop.py`: policies, sessions, retries, events, checkpoints, and cleanup.
- `tests/test_docent_reading_step.py` and `test_projections.py`: local reading replay, SDK conversions,
  projection flattening, and embedded tree integration.
- `tests/test_model_overrides.py`: sampling override behavior.
- `tests/test_anthropic_messages_api.py`, `test_openai_compatible_api.py`, and
  `test_openai_responses_api.py`: provider request/response conversion, streams, and failures using
  constructed SDK objects and mocked clients.
- `tests/test_model_apis_llm_integration.py`: opt-in live provider checks. Both root and package pytest
  configurations exclude `llm_integration` by default.

The repository root owns the workspace lockfile, development dependencies, and lint/type-checking
policy. See `README.md` here for package usage and `../README.md` for workspace commands.
