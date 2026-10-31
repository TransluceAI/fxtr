"""Compare OpenAI models over ten ordered question sequences using behaviors rollouts.

Run with ``uv run python launch_openai.py --model MODEL`` from the project directory.
Each model/sequence pair has an isolated session; settings are recorded as workflow inputs.
"""

import dataclasses
from functools import partial
from typing import Annotated, cast

import anyio

from behaviors.implementations.context_policies import (
    ContextPolicyGenerator,
    context_policy_from_generator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI
from behaviors.rollouts.chat import (
    ChatRolloutEvent,
    ContextTurnEvent,
    ModelTurnEvent,
    run_chat_rollout,
)
from behaviors.types import (
    ChatRollout,
    ContentText,
    ContextTurn,
    DynamicData,
    EndRollout,
    RolloutFailed,
    SamplingParams,
    SetSystemMessage,
    SystemMessage,
    UserMessage,
)
from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.experiment.handles import ArrayHandle, ScalarHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

DEFAULT_QUESTION_SEQUENCES: dict[str, list[str]] = {
    "modular_arithmetic": [
        "Find the smallest positive integer n with n mod 3 = 2, n mod 5 = 3, and n mod 7 = 2. "
        "Verify all three remainders.",
        "Describe all positive solutions to those same three constraints, and give the next two.",
        "Add the constraint that n is divisible by 11. Find the smallest positive solution.",
        "List every solution to all four constraints below 2000. Justify completeness.",
        "Now change only n mod 7 from 2 to 3, retaining the other three constraints. "
        "Find the smallest positive solution and verify it.",
    ],
    "geometry": [
        "Consider a rectangle with width 4 and height 7. What is its area?",
        "What is its perimeter?",
        "If we double only its width, what is the new area?",
        "What is the new perimeter?",
        "Explain why doubling the width did not double the perimeter.",
    ],
    "conditional_probability": [
        "Three boxes contain BB, BR, and RR respectively (B = blue, R = red). Choose a box "
        "uniformly, then draw one of its marbles uniformly. It is blue. What is the probability "
        "you chose BB? Give an exact fraction and a short justification.",
        "Return that marble, mix the same box, and draw again. It is blue again. "
        "What is the updated probability the chosen box is BB?",
        "Do not return the second marble. Draw the remaining marble from that same box. "
        "Before seeing it, what is the probability it will be blue?",
        "The third marble is actually red. Which box was selected, and what was the "
        "probability of this third-draw observation given the first two observations?",
        "Restart from the original experiment. Instead of replacing between the first two "
        "draws, draw both marbles without replacement and observe two blues. What is the "
        "posterior probability of BB, and why is it different from the replacement case?",
    ],
    "string_edits": [
        "Start with the exact ASCII string abacabad. Reverse it. Output only the resulting string.",
        "From your current string, delete every a. Output only the resulting string.",
        "Insert x immediately after every b in the current string, simultaneously. "
        "Output only the resulting string.",
        "Rotate the current string two positions to the left. Output only the resulting string.",
        "Replace every non-overlapping occurrence of bx with Q, scanning left to right. "
        "Output only the resulting string.",
    ],
    "story": [
        "Invent a robot character with a name and a one-sentence goal.",
        "Give that character a small obstacle to achieving the goal.",
        "How could it solve the obstacle without violence?",
        "Write the ending in one sentence.",
        "Give this story a title that refers to the original goal.",
    ],
    "translation": [
        "Translate 'The little cat sleeps' into French.",
        "Which word in your translation means 'cat'?",
        "Change the sentence so it refers to two cats.",
        "Translate that plural sentence back into English.",
        "Briefly explain the changes you made in French.",
    ],
    "transactions": [
        "Balances start at A=100, B=80, C=20. Transactions read a snapshot at begin and buffer "
        "absolute balances; committing overwrites only the accounts they wrote, with no conflict "
        "detection. T1 and T2 both begin at the initial state. T1 transfers 30 from A to B; "
        "T2 transfers 50 from A to C. List each transaction's buffered writes.",
        "Commit T1, then T2, using those buffered writes. Give all final balances and their total. "
        "Explain any discrepancy from the initial total.",
        "Reset to the initial balances and the same two initial snapshots. Commit T2, then T1 "
        "instead. Give all final balances and their total.",
        "Reset again. Add a rule: reject a commit if any account it read has changed since its "
        "snapshot. Both begin at the initial state; T1 commits, then T2 attempts to commit. "
        "Which commits succeed, and what balances remain?",
        "Retry the rejected transfer using a fresh snapshot of the current balances and commit "
        "it under the validation rule. Give final balances and total; explain why this differs "
        "from the first commit order without validation.",
    ],
    "plants": [
        "Name the process by which plants use sunlight to make sugar.",
        "What gas do plants take in for that process?",
        "What gas is released?",
        "What role does water play in that process?",
        "Summarize the inputs and outputs in one sentence.",
    ],
    "scheduling": [
        "One worker runs non-preemptive jobs starting no earlier than time 0. A has duration 3, "
        "release 0, deadline 4; B duration 2, release 0, deadline 7; C duration 2, release 3, "
        "deadline 5; D duration 1, release 0, deadline 8. Deadlines include equality. "
        "Find a feasible schedule and give start/end times.",
        "Keeping all constraints, add that D must finish before B starts (equality allowed). "
        "Is a feasible schedule possible? Give one or prove impossibility.",
        "Keep that precedence constraint, but extend only B's deadline to 8. "
        "Give a feasible schedule and its completion time.",
        "Keep the updated constraints and shorten A's duration from 3 to 2. "
        "What is the earliest completion time? Give a schedule and a lower-bound argument.",
        "For your last schedule, list every job's release time, deadline, start, and finish. "
        "Verify both the timing constraints and the D-before-B constraint.",
    ],
    "logic": [
        "Three people A, B, C each either always tell the truth or always lie. A says 'B is a "
        "liar'; B says 'C is a liar'; C says 'A and B are both liars'. Find every consistent "
        "assignment, interpreting 'and' as logical conjunction.",
        "Is the assignment unique even without being told how many liars there are? "
        "Justify this by considering both possible types of B.",
        "Change only C's statement to 'A or B is a liar', with inclusive or. "
        "Find every consistent assignment under these revised statements.",
        "For the revised statements, suppose exactly one person tells the truth. "
        "Which assignments remain? If none, explain the contradiction.",
        "Now restore C's original 'and' statement and instead suppose exactly two people "
        "tell the truth. Is that possible? Distinguish the truth of a statement from its negation.",
    ],
}


@dataclasses.dataclass(frozen=True)
class QuestionIndex(
    TaggedDataclass, fxtr_type="org.transluce.examples.behavior_rollouts.QuestionIndex.v1"
):
    """Context-turn metadata: the position of the turn's question in its sequence."""

    question_index: int


@dataclasses.dataclass(frozen=True)
class QuestionsAsked(
    TaggedDataclass, fxtr_type="org.transluce.examples.behavior_rollouts.QuestionsAsked.v1"
):
    """End-of-rollout metadata: how many questions the conversation asked."""

    questions_asked: int


def question_sequences_array(sequences: dict[str, list[str]]) -> Array[list[str]]:
    """One named sequence per row; question order is the list's order, not a mapped dim."""
    if not sequences or any(
        not name.strip() or not questions or any(not question.strip() for question in questions)
        for name, questions in sequences.items()
    ):
        raise ValueError("Provide named, nonempty question sequences with nonempty questions")
    return Array.from_items(
        [((name,), questions) for name, questions in sequences.items()],
        type=("[sequence: str]", list[str]),
    )


def models_array(models: list[str]) -> Array[str]:
    """Use unique model names as both axis keys and provider model identifiers."""
    if not models or any(not model.strip() for model in models) or len(set(models)) != len(models):
        raise ValueError("Provide unique, nonempty model names")
    return Array.from_items([((model,), model) for model in models], type=("[model: str]", str))


async def print_turn(event: ChatRolloutEvent, *, context: StepContext) -> None:
    """Print each complete turn and record it in the step's logs for the debug UI."""
    if not isinstance(event, (ContextTurnEvent, ModelTurnEvent)):
        return
    for message in event.turn.messages:
        text = "".join(block.text for block in message.content if isinstance(block, ContentText))
        line = f"{message.role}: {text or '[no text output]'}"
        context.log(line)
        print(f"  {line}", flush=True)


@step(name="openai_rollout.openai_conversation")
async def openai_conversation(
    context: StepContext,
    model_name: str,
    questions: list[str],
    max_tokens: int,
) -> ChatRollout:
    """Ask each question in order and return the conversation for fxtr to store."""

    async def ask_questions() -> ContextPolicyGenerator:
        for index, question in enumerate(questions):
            yield ContextTurn(
                messages=(UserMessage(content=(ContentText(text=question),)),),
                available_tools=None,
                metadata=(DynamicData.from_tagged(QuestionIndex(question_index=index)),),
            )
        yield EndRollout(
            metadata=(DynamicData.from_tagged(QuestionsAsked(questions_asked=len(questions))),)
        )

    api = OpenAIResponsesAPI(model_name)
    try:
        model = chat_session_model_from_api(api, SamplingParams(max_tokens=max_tokens))
        policy = context_policy_from_generator(
            ask_questions,
            initialization=SetSystemMessage(
                message=SystemMessage(content=(ContentText(text="Answer briefly and clearly."),))
            ),
        )
        # The policy needs one more poll after the last answer to yield EndRollout.
        return await run_chat_rollout(
            model,
            policy,
            max_turns=len(questions) + 1,
            event_callback=partial(print_turn, context=context),
        )
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()


@workflow(name="openai_rollout.openai_experiment")
async def openai_experiment(
    context: WorkflowContext,
    questions: Annotated[ArrayHandle[list[str]], "[sequence: str]"],
    model_name: Annotated[ArrayHandle[str], "[model: str]"],
    max_tokens: ScalarHandle[int],
) -> Annotated[ArrayHandle[BoundID[ChatRollout]], "[model: str, sequence: str]"]:
    """Run the Cartesian product of models and sequences, broadcasting the token budget."""
    rollouts = context.run_step(
        "conversation",
        openai_conversation,
        {
            "questions": questions,
            "model_name": model_name,
            "max_tokens": max_tokens,
        },
        map_over=["model", "sequence"],
    ).expect_type(("[model: str, sequence: str]", BoundID[ChatRollout]))
    # Entity-returning steps store their results as references.
    return cast(ArrayHandle[BoundID[ChatRollout]], rollouts)


async def report_results(storage: FxtrEntityStorage, result: Array[BoundID[ChatRollout]]) -> int:
    """Load each mapped rollout while its storage is open and report per-sequence outcomes."""
    failed = False
    for (model, name), reference in result.items():
        rollout = await storage.load(reference, as_type=ChatRollout)
        print(f"\n{model} / {name}: {rollout.status.kind}, {len(rollout.model_turns)} responses")
        if isinstance(rollout.status, RolloutFailed):
            failed = True
            failure = rollout.status.failure
            print(f"Failure ({failure.category}): {failure.description}")
    return int(failed)
