# Behaviors — code overview

`behaviors` is a Python 3.12.4+ package for running chat models against context policies. It depends
on `fxtr` for typed entity storage. Provider SDKs (`behaviors[models]`) and the Docent SDK
(`behaviors[docent]`) are optional.

`README.md` covers setup and usage and links to the documentation's model selection guide,
`../docs/behaviors/guides/choosing-models.mdx`, for listing and searching OpenAI, Anthropic,
Gemini, and OpenRouter model IDs and selecting adapters. Model discovery uses provider APIs directly;
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
(Anthropic Messages, OpenAI Chat Completions-compatible, OpenRouter, OpenAI Responses, and Gemini)
require `behaviors[models]`; core imports and rollout logic do not require SDKs. Each provider API
dialect has a codec converting between its wire shapes and the vocabulary; an implementation is a
thin transport around its dialect's codec, and the codec also records every change it makes to a
request the dialect cannot carry as is.
`behaviors.judging` is for using a model as a judge. It contains (1) `judge`, which asks a model
for a judgment of the entities it is given and returns a `Judgment`, a fxtr entity with the model's
answer, its citations and every call made, (2) functions that write the JSON Schema for the answer, (3)
functions that write evidence such as an agent run as text the model can cite, and (4) ready-made
inputs. Instructions or a rubric that are only text are a `TextEntity`, from `behaviors.types`.

## Source map

The importable package lives in `src/behaviors/`:

- `types/` defines frozen data records: content blocks, messages, tools and cache hints,
  model requests/responses and response formats, sampling and reasoning parameters, failures,
  retry settings, request adaptation's losses and policy (`adaptation.py`: `RequestLoss`,
  `ResponseLoss`, `AdaptationPolicy`; the `AdaptedRequest` a codec returns is in
  `interfaces/dialect_codecs.py`), wire dialects (`dialect.py`), provider-side tools
  (`provider_tool.py`), streaming events, rollout turns/status, session initializations,
  transcript trees, and `TextEntity`, a piece of text as an entity, with
  `text_entity_to_prompt_text`, which writes it as text for a model to read (`text_entity.py`).
  `__init__.py` exposes the public vocabulary. `ChatRollout`, session initializations,
  `Transcript`, `TranscriptGroup`, `AgentRun` and `TextEntity` are fxtr entities. Union members use
  `TaggedDataclass` tags with `org.transluce.behaviors.<Class>.v<N>` identifiers.
  `namespace.py` defines the type-versioning rules and qualifies versioned names with
  `behaviors_type`, also exported from `behaviors.types`. `json.py` re-exports
  `JSONValue` and `JSONObject` from `fxtr.core.json`; fxtr checks JSON on save and load, and
  field types on load. Cacheable content and tools are explicit `TextContent`, `Content`, and
  `ToolDefinition` union variants, defined in `content.py` and `tool.py` next to their base
  classes; `cache_control.py` holds the `CacheControl` breakpoint both use. A reasoning block
  (`ContentReasoning`) carries its text, its `form` (the model's own reasoning, a provider's
  summary, or redacted) and the provider state to send back, a `DynamicData` that only the codec
  owning its payload type decodes (each payload type is defined in its dialect module); the
  `ContentReasoning` docstring says how the state is filtered per destination and carried through
  a harness (`implementations/dialects/_reasoning_envelope.py`). `rollout.py` also defines
  `find_metadata`, which decodes the first metadata entry of a requested type. Transcript/group
  trees are embedded inside their parents and can also be stored independently.
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
  stream-restart events.
- `implementations/models/anthropic.py`, `openai_compatible.py`, `openrouter.py`,
  `openai_responses.py`, and `gemini.py` implement Anthropic Messages, OpenAI Chat
  Completions-compatible endpoints, OpenRouter (the Chat Completions adapter with OpenRouter's
  base URL, key and codec variant), OpenAI Responses, and Gemini `generateContent`. Each is a thin
  transport: its dialect's codec renders the request and decodes the streamed response, and the
  module owns credentials, the base URL and the HTTP round trip. `_raw_stream.py` sends a rendered
  request through a provider SDK client (or, for Gemini, the module's own small client) and
  decodes the raw stream, refusing an SDK client that needs the SDK's send-time steps
  (`require_supported_client`); `_openai_options.py` gives the OpenAI adapters' requests the SDK's
  model-endpoint authentication; `_workarounds.py` holds per-model fixes learned from a provider's
  rejections, which `_raw_stream.call_and_decode` applies and retries with. Import provider modules
  explicitly to load their SDKs. Each provider module but `openrouter.py` also has a `*Provider`
  (`ModelProvider`) that builds and caches one API per model id over a shared client;
  `providers.py` has the vendor-agnostic `SingleModelProvider` and `ModelOverrides`.
- `implementations/dialects/` holds one codec per wire dialect (the protocols are in
  `interfaces/dialect_codecs.py`): `anthropic_messages.py`, `openai_responses.py`,
  `openai_chat_completions.py` and `gemini_generate_content.py`, each also defining its provider's
  reasoning-state payload types. A codec has a model side (what a `ModelAPI` needs: adapt, render,
  decode the provider's answer) and, for every dialect but chat completions, a harness side (what
  a server needs to serve a harness that speaks the dialect: recognize and decode its requests,
  render an answer back); translating between dialects composes one codec's decode with another's
  encode. `codec_utils.py` is the public toolkit for writing a codec: SSE framing, JSON bodies, and
  `own_reasoning_state`, which reads a state as the codec's own payload type. `_common.py` and
  `_openai_common.py` hold the other shared internals, and `_reasoning_envelope.py` carries
  reasoning through a harness whose dialect is not the reasoning's own. `reconstruction.py` is the
  reconstruction check: whether a codec reproduces a request or response when it decodes it and
  re-encodes it in the same dialect (`compare_request`, `compare_response`), which the dialect
  coverage audit runs over its corpus. `coverage/` holds each dialect's coverage table, which
  `tests/dialect_coverage/` checks against the provider's request schema. Every installed SDK
  version is audited for unhandled paths; table comparisons run only with the lockfile's SDK
  versions, which the snapshots describe.
- A request reaches a provider in three steps, all in
  `implementations/models/_raw_stream.call_and_decode`: the codec's `adapt_request` changes it to
  what the dialect can carry (built from the steps in `dialects/adaptation.py`, among them
  removing reasoning state the codec's `accepts_reasoning_state` rejects, and for response
  formats, strict tools and tool output schemas from those in `dialects/contract_adaptation.py`,
  whose module docstring gives the rule for fitting a schema to what a provider enforces, and
  which read each provider's JSON Schema subset from `dialects/schema_support.py`), recording
  each change as a `RequestLoss`; the request's `AdaptationPolicy`
  (`ModelRequest.adaptation_policy`) refuses changes in meaning if asked to; the codec's
  `encode_request` renders the result as it is. The losses travel on the result
  (`ModelResponse.adaptation_losses`, `ModelCallFailure.adaptation_losses`). On the harness side,
  a codec's `render_stop` says what its encoders render of a response's ending other than as
  given, and whether the call must be answered as an error instead; the encoders take their stop
  fields from the same mapping.
- `implementations/models/overrides.py` wraps a model API to change every request:
  `SamplingOverrides` pins sampling parameters and `FinalBlockCacheControl` marks every request's
  final block as a cache breakpoint.
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
- `rollouts/chat.py` alternates policy and model turns, emits ordered events (each turn, the
  model's stream deltas, and last how the rollout ended), records configured
  failures, enforces a turn limit, and closes sessions on exit. `ChatRollout` retains the unanswered
  context turn if a model call fails. Its `initialization` field records the model's
  initialization as `DynamicData`; consumers decode it with the type they expect. The transcript
  projection currently renders only `SetSystemMessage`. Stored rollouts use the strict dataclass
  converter: every field, including a null `initialization`, is present.
- `rollouts/chat_preview.py` holds `ChatRolloutPreview`, an event callback folding a rollout's
  events into a `Partial[ChatRollout]` of the rollout so far (fxtr's `entity_defns/partial.py`):
  finished turns, plus a partial last model turn assembled from stream events while the model
  generates, dropped on `StreamRestart` until the retried reply begins, and the rollout's `status`
  and `end` once it has ended. After each event it hands a publish function, usually a
  step's `ctx.preview`, a function building the rollout so far, so the preview is built only when
  it is written; that is how the viewer shows a rollout while its step runs.
- `judging/` is the judge: asking a model to assess evidence against a rubric. Each file is about
  one thing, and the function that writes an entity as prompt text lives next to that entity.
  - `judge.py` has `judge`, the function that makes a judgment end to end. It takes
    everything the model is given (instructions, rubric, evidence) as fxtr entities, each under
    a name, and a context template: the messages to send, with a placeholder (`{name}`) for
    each entity, `{output_schema}` for the output schema and `{citation_instructions}` for the
    text on citing an entity, written with the entities' names. A `CustomFormat` is one kind of
    field with a check of its own. Its `schema` method writes the field into the output schema
    under the format's name, and `judge` takes the same object in `custom_formats`. It holds what has to agree
    for the field: the schema of its value, the validator (`CustomFormatValidator`) that checks a field with that format, the
    functions that write entities as prompt text which that validator relies on, by entity class, and the text that describes to the
    model what each of those functions writes. `judge` writes each entity as prompt text with the function for its class
    (`EntityToPromptText`), from a custom format or else from `entity_to_prompt_text`, and fills the
    template in (`fill_context_template`, which a caller can also use by itself to fill a template with text of their own), adding the custom formats' text for the classes it was given to the citation
    instructions. Each entity is written in a section that
    shows its name, whose boundary marker is computed from the content, so the same inputs
    always produce the same request. `judge` then calls the model, reads the answer as a JSON
    object (an answer cut off at the output limit is not read), and checks it: against the output schema; with the validator of the custom
    format of each field whose schema names a `format`, which is
    only called with a value that fits the rest of that field's schema; that fxtr can store
    the answer. It checks nothing about citations itself, which is for each custom format's validator. It reads the citations out of the fields that have a custom format. Any other field is free text. A rejected answer goes back to the model in the same conversation, with what
    was wrong. The caller sets how many answers the model gets (`max_answers`), and the schedule `generate_with_retries` follows for a failed call
    (`retry`, `failure_categories_to_retry`); the defaults are three answers and three calls for the failures that may pass. A constant above `judge` fixes which failures return a judgment with no output where the rest raise
    `ModelCallFailedError`, since getting that wrong would cache a passing failure. A mistake
    in the call (a template `judge` cannot fill in or that leaves an entity out, a schema it cannot use, custom formats that do not match the schema or that write a class differently, an entity with no
    function to write it, an entity fxtr cannot store) raises before the first model call. The result is a `Judgment`, a fxtr entity holding
    the ID of each entity by name, the answer, a `Citation` for each citation, and a `JudgeAttempt` for
    each answer, with an `InvalidOutputReason` for each thing wrong with an answer that was not
    accepted. `judge`'s docstring has the rules for when it returns a failed judgment
    and when it raises. `judge` needs the `jsonschema` package, which `behaviors` only installs
    with the `docent` extra.
  - `schemas.py` writes output schemas. Each of `binary_schema`, `continuous_schema` (a
    score on a scale), `categorical_schema` and `structured_schema` (a value
    with the caller's own schema) returns the schema for a complete answer that starts with a
    `discussion`. A field with a check of its own, such as text with citations, is written by
    its `CustomFormat`'s `schema` method, to use as a `discussion` or inside a schema written by hand.
  - `citations.py` defines the citation syntax (`[run]` for an entity
    named `run`, or `[run:<address>]` for a part of it). Bracketed text is a citation only when
    it starts with an entity's name. It is read wherever it sits, with or without a space next to it. It has
    `citation_instructions_text`, which explains the syntax to the model with the entities'
    names. It covers only the syntax: each custom format's instructions say which entities can be cited. It also has two checks for a citation validator to call, which `judge` does not run: `misnamed_citation_problems` (a citation whose name is not an entity's, judged by whether its address has the format's shape, so that other bracketed text such as a quoted log line is left alone) and `malformed_citation_problems` (an attempt at a citation that is not well formed, such as `[run:]`). It has `strings_in_checked_values` (the strings of an answer that are in a field with a custom format, the only ones searched for citations), the `Citation` type, `find_citations_in_output`, which `judge` calls to read the
    citations out of an answer, and `check_citation_addresses`, which a custom format validator
    calls with a function that says whether an address is valid for an entity.
  - `agent_run_evidence.py` has everything for judging an `AgentRun` or a `Transcript`, which is
    cited by line. That takes three parts that have to agree, held together by the
    `CustomFormat` `AGENT_RUN_LINE_CITATION_FORMAT`, which the defaults use: `agent_run_to_prompt_text` and
    `transcript_to_prompt_text` label each message block (`T<n>B<n>` in an agent run, `B<n>` in a
    transcript) and each line of it (`L<n>`: each non-empty line of its text and each tool call,
    `message_lines`), `AGENT_RUN_LINES_FORMAT_INSTRUCTIONS_TEXT` and
    `TRANSCRIPT_LINES_FORMAT_INSTRUCTIONS_TEXT` describe that to the model, and
    `agent_run_line_citation_validator` requires every citation of one to name lines that exist
    (`T0B3L4`, `T0B3L4-6`), and sends back anything else: a whole block, a whole entity, a label with no entity name, any citation of another kind of entity, a citation that is not well formed, and text shaped like a citation that names no entity. The viewer's judgment view opens a transcript
    or an agent run at such an address, and highlights the cited lines (fxtr-view-behaviors,
    which counts lines the same way). Judging another kind of entity means writing the same
    three parts for it and a `CustomFormat` of them.
  - `defaults.py` has the ready-made inputs: `DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY` for judging
    agent runs and transcripts, and `DEFAULT_ENTITY_TO_PROMPT_TEXT`, the function that writes each class above as prompt text,
    which for agent runs and transcripts are the ones `AGENT_RUN_LINE_CITATION_FORMAT` has.

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
- `tests/test_chat_preview.py`: `ChatRolloutPreview`: folding turns and every kind of delta,
  restarts, the partial rollout's wire form, and a live rollout's publications.
- `tests/test_docent_reading_step.py` and `test_projections.py`: local reading replay, SDK conversions,
  projection flattening, and embedded tree integration.
- `tests/test_model_overrides.py`: sampling override behavior.
- `tests/test_judging_judge.py`: the judge against a scripted model: schema functions, prompt text,
  citations and the ones that are refused, cut-off and wrapped answers, corrections and what each tells the model, custom format validators, the shared
  retry budget and its waits, filling in the template, call mistakes, and storage.
- `tests/test_anthropic_messages_api.py`, `test_openai_compatible_api.py`, and
  `test_openai_responses_api.py`: provider request/response conversion, streams, and failures using
  constructed SDK objects and mocked clients.
- `tests/test_gemini_api.py` and `test_openrouter_api.py`: the Gemini and OpenRouter adapters,
  against mocked transports; `test_sdk_raw_streaming.py`, `test_sse_frame_parser.py` and
  `test_dropped_streams.py`: sending through the SDK clients and decoding raw streams.
- `tests/test_request_adaptation.py`, `test_foreign_reasoning_state.py`,
  `test_reasoning_envelope.py`, `test_dialect_extras.py`, `test_provider_tools.py` and
  `test_tool_names.py`: what each codec adapts, keeps or drops, and how reasoning state and
  dialect extras cross between dialects.
- `tests/test_reconstruction.py`: the reconstruction check, against the hand-written Claude Code
  and Codex requests in `tests/fixtures/`.
- `tests/test_structured_output.py` and `test_schema_support.py`: response formats, strict tools,
  refusals and the per-provider schema rules; `tests/dialect_coverage/` holds the codec coverage
  audit over each provider's request schema, the cross-dialect contract invariant, and a schema
  corpus (`schema_corpus.py`) with what each provider did with it live and the wire form each
  took (`schema_corpus_accepted.jsonl`), which `test_schema_corpus.py` holds adaptation to
  offline.
- `tests/test_model_apis_llm_integration.py`, `test_gemini_api_llm_integration.py`,
  `test_structured_output_llm_integration.py` and
  `dialect_coverage/test_schema_acceptance_llm_integration.py` (the corpus, sent to every
  provider): opt-in live provider checks. Both root and package pytest configurations exclude
  `llm_integration` by default.

The repository root owns the workspace lockfile, development dependencies, and lint/type-checking
policy. See `README.md` here for package usage and `../README.md` for workspace commands.
