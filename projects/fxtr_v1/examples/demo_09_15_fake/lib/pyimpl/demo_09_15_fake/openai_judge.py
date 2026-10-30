"""OpenAI implementation of SimpleWeirdBehaviorJudge."""

import random

import anyio
import openai
from demo_09_15_fake_defns import index

from fxtr.object_model import core


class SimpleBehaviorJudge_OpenAIImpl(index.SimpleWeirdBehaviorJudge):
    """OpenAI implementation of SimpleWeirdBehaviorJudge."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        *,
        model_name: str,
        weird_behavior: index.WeirdBehaviorConfig,
        client: openai.AsyncOpenAI,
    ):
        """Initialize the OpenAI judge implementation.

        Args:
            model_name: The OpenAI model name to use
            weird_behavior: The weird behavior configuration to judge against
            client: The OpenAI client to use
        """
        self._model_name = model_name
        self._weird_behavior = weird_behavior
        self._client = client

    @classmethod
    async def _ainit(
        cls,
        resolver: core.PlanResolver,
        /,
        *,
        model_name: str,
        weird_behavior: index.WeirdBehaviorConfig,
    ):
        """Initialize the OpenAI judge implementation.

        Args:
            resolver: Plan resolver to get dependencies
            model_name: The OpenAI model name to use
            weird_behavior: The weird behavior configuration to judge against
        """
        # Get API keys from dependencies
        secrets_impl = resolver(index.openai_api_key.handle)
        assert core.implements_interface(secrets_impl, core.SecretRetriever)

        # Initialize OpenAI client
        client = openai.AsyncOpenAI(api_key=await secrets_impl.get_secret())

        return cls(model_name=model_name, weird_behavior=weird_behavior, client=client)

    async def judge(self, response: index.SimpleResponse) -> index.SimpleBehaviorJudgement:
        """Judge a response for weird behavior.

        Args:
            response: The response to judge

        Returns:
            A SimpleBehaviorJudgement with category and explanation
        """

        await anyio.sleep(0.1 + random.random() * 0.2)
        category = random.choice(list(self._weird_behavior.categories.keys()))
        explanation = f"Randomly selected category: {category}"

        return index.SimpleBehaviorJudgement(
            category=category,
            explanation=explanation,
        )
