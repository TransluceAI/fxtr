"""Run the deterministic, two-turn EchoAPI example as a project job."""

import argparse

from behavior_rollouts.scripted_rollout import scripted_experiment
from behaviors.types import ChatRollout, ContentText
from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.project.running import launch_cli


async def report(storage: FxtrEntityStorage, result: Array[BoundID[ChatRollout]]) -> None:
    for key, reference in result.items():
        rollout = await storage.load(reference, as_type=ChatRollout)
        print(key, rollout.status.kind)
        for turn in rollout.model_turns:
            print(
                " ",
                " ".join(
                    block.text
                    for message in turn.messages
                    for block in message.content
                    if isinstance(block, ContentText)
                ),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-staged", action="store_true", help="Pin staged changes")
    parser.add_argument("--no-verify", action="store_true", help="Skip the venv check")
    args = parser.parse_args()
    launch_cli(
        scripted_experiment,
        {
            "prompts": Array.from_items(
                [(("greeting",), "Hello."), (("question",), "What is a prime number?")],
                type=("[prompt: str]", str),
            )
        },
        anchor=__file__,
        allow_staged=args.allow_staged,
        verify_environment=not args.no_verify,
        on_result=report,
    )
