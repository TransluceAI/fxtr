# behaviors

Chat-model rollouts, context policies, and typed conversation records for Python 3.12.4+. This is a
separate package alongside `fxtr`, using fxtr for entity storage. See `CODE_OVERVIEW.md` for
the source map.

## Setup

From the repository root:

```bash
uv sync --all-packages --all-extras
```

Core types, context policies, and sessions use `fxtr`, `anyio`, and `cattrs`. Provider SDKs are
installed with the `models` extra. Import providers from their modules:

- `behaviors.implementations.models.anthropic.AnthropicMessagesAPI`
- `behaviors.implementations.models.openai_compatible.OpenAICompatibleAPI`
- `behaviors.implementations.models.openai_responses.OpenAIResponsesAPI`

Each of those modules also has a `ModelProvider` (`AnthropicMessagesProvider`, ...) that serves any
model id over one shared client. The base-model user simulator
(`behaviors.implementations.context_policies.base_model_user_simulator`) needs the `models` extra
too.

## Find available models

`behaviors` takes provider model IDs directly and has no built-in model catalog. See
[Choosing models](../docs/behaviors/guides/choosing-models.mdx) for OpenAI, Anthropic, and
OpenRouter listing commands, authentication, pagination, searching model names, and selecting the
corresponding adapter. Use the returned `id`, and check that the model
supports the endpoint and features your experiment needs.

## Roll out a model against a scripted user

For a minimal two-turn rollout using `ScriptedUserSimulator` directly:

```python
import anyio

from behaviors.implementations.context_policies import (
    ScriptedUserSimulator,
    context_policy_from_user_simulator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.rollouts.chat import run_chat_rollout
from behaviors.types import ContentText, SamplingParams, UserMessage
from fxtr.storage.memory_storage import MemoryStorage


async def main(model_name: str) -> None:
    api = OpenAIResponsesAPI(model_name)
    try:
        model = chat_session_model_from_api(api, SamplingParams(max_tokens=200))
        user = ScriptedUserSimulator(
            messages=(
                UserMessage(content=(ContentText(text="Name a prime number."),)),
                UserMessage(content=(ContentText(text="Explain why it is prime."),)),
            )
        )
        policy = context_policy_from_user_simulator(user, initialization=None)
        rollout = await run_chat_rollout(model, policy, max_turns=10)

        storage = MemoryStorage()
        reference = await storage.store(rollout)
        restored = await storage.load(reference)
        assert restored == rollout
        print(restored.model_turns)
    finally:
        await api.aclose()


# anyio.run(main, "your-model-name")
```

## Contracts and composition

`ModelAPI` makes one request. `chat_session_model_from_api` wraps it in a `ChatSessionModel` whose
sessions hold conversation history. A `ContextPolicy` controls the initialization, messages, and
tools the model sees on each turn. `run_chat_rollout` alternates the two until the policy ends, a
turn limit is reached, or a configured failure is recorded.
The returned `ChatRollout.initialization` records the initialization passed to the model as
fxtr `DynamicData` (its tagged serialized form); read it back by naming the type you expect, e.g.
`rollout.initialization.try_as_type(SetSystemMessage)`. The transcript projection renders a recorded
`SetSystemMessage` before the turns. A missing recorded initialization loads as `None`.

Turns, `EndRollout`, and `ChatRollout` carry `metadata`: a tuple of `DynamicData` entries, each
wrapping a tagged dataclass the producer defines (`DynamicData.from_tagged(value)`). Readers name
the type they expect: `find_metadata(turn.metadata, MyType)` returns the first matching entry,
decoded, or `None`. The API-backed session records each turn's `ModelResponse` this way.

Use `context_policy_from_user_and_tools` to combine a scripted user with a tool policy, or use
`context_policy_from_generator` / `GeneratorContextPolicy` for custom control flow. Tool policies
provide tool results; user simulators and full context policies can end the rollout.

Sessions expose `aclose()`. The rollout runner owns session cleanup; the caller owns the provider
client and closes it separately. Serializable sessions expose `get_state()` and their factories
expose `restore_session(state)`. A state is `DynamicData` wrapping the implementation's own tagged
state class, so restoring another implementation's state fails on its tag. Persist it with fxtr
storage, or as plain JSON via `thaw_fxtr_data(state.data)` and `DynamicData.from_data(...)`.
Serializable policy combinators nest their components' states.
Generator context policies keep live generator state and do not support checkpoints.

Model failures are categorized data (`ModelCallFailure`). The API-backed session retries configured
categories; the rollout runner records `context_length` by default and raises `ModelCallFailedError`
for other unhandled failures. Configure `failure_categories_to_record` to change that behavior.
Streaming callbacks are awaited in order; a retry emits `StreamRestart` to mark discarded partial
output. A callback exception aborts the rollout.

Entities use versioned `org.transluce.behaviors.<Class>.v1` type IDs. Store/load through fxtr
storage instead of calling serialization methods on entities. The returned `BoundID` selects the
concrete loader; when loading a bare entity ID, pass a concrete `as_type`, such as
`SetSystemMessage`, rather than the abstract `SessionInitialization` base.

Transcript and group trees are embedded in their parent entity. Individual transcripts and groups
can also be stored on their own. Content and tool definitions use explicit tagged unions, preserving
cacheable subclasses and their cache hints. Session states and metadata use the same tagged form,
so they preserve these variants too; provider rendering keeps its usual `role`, `type`, and `kind`
fields.

`behaviors.types.json` re-exports fxtr's `JSONValue` and `JSONObject`. Tool inputs, schemas, and
transcript metadata accept nested JSON lists and dictionaries, preserving integers, floats, and booleans.
Fxtr rejects incorrectly typed fields on save: use `temperature=0.0` for a float field. Storage
requires string dictionary keys, finite floats, and integers from `-2**64` through `2**64 - 1`.

## Projections and Docent integration

`behaviors.interfaces.projections` defines projections from domain objects into transcripts and
agent runs. `ChatRolloutTranscriptProjection` in `behaviors.rollouts.chat` flattens a rollout into
a transcript, including any unanswered context turn.

Install `behaviors[docent]` to convert transcript trees into Docent data models or use
`behaviors.implementations.docent.DocentReadingStep`. The reading step renders a Docent prompt
against a transcript or agent run, calls an injected `ModelAPI`, and parses and validates the
response and citations. It operates on ordinary Python objects without separate child save/load
calls. Provider and Docent SDKs remain optional dependencies. There is no supplied model-driven
user simulator or general judging implementation.

## Development

[`example_projects/behavior_rollouts/`](../example_projects/README.md) is a complete fxtr
experiment using this library (scripted and OpenAI conversations), an independent project with
launchers, a view bundle, and its own setup instructions and tests.

From the repository root:

```bash
uv run --all-packages --all-extras pytest -n auto
uv run --package behaviors --extra models pytest -c behaviors/pyproject.toml behaviors/tests -n auto
uv run ruff check behaviors
uv run --all-packages --all-extras pyright --project behaviors
```

Unit tests use mocked providers. Live tests are excluded by default; opt in explicitly with
`-m llm_integration` and provider credentials. Set `BEHAVIORS_TEST_ANTHROPIC_MODEL` or
`BEHAVIORS_TEST_OPENAI_MODEL` to select models for those tests.
