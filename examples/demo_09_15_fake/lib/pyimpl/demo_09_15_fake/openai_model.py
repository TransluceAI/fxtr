"""OpenAI implementation of SimpleSingleTurnModel."""

from __future__ import annotations

import random

import anyio
import openai
from demo_09_15_fake_defns import index

from fxtr.impl_plans import plan_helpers
from fxtr.object_model import core


class SimpleSingleTurnModel_OpenAIImpl(index.SimpleSingleTurnModel):
    """OpenAI implementation of SimpleSingleTurnModel."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, *, model_name: str, client: openai.AsyncOpenAI):
        """Initialize the OpenAI model implementation.

        Args:
            model_name: The OpenAI model name to use
            client: The OpenAI client to use
        """
        self._model_name = model_name
        self._client = client

    @classmethod
    async def _ainit(
        cls,
        resolver: core.PlanResolver,
        /,
        *,
        model_name: str,
        concurrency_limit: plan_helpers.RuntimeDepPlan[
            core.RuntimeEnvConfigAssignment[index.ConcurrencyLimitConfig]
        ],
        coordinator: plan_helpers.RuntimeDepPlan[core.StorageHandle[int]],
        _openai_api_key: plan_helpers.RuntimeDepPlan[
            core.SecretRetriever[str]
        ] = index.openai_api_key.handle,
    ) -> SimpleSingleTurnModel_OpenAIImpl:
        """Initialize the OpenAI model implementation.

        Args:
            resolver: Plan resolver to get dependencies
            model_name: The OpenAI model name to use
            concurrency_limit: The concurrency limit to use
            coordinator: Fake coordinator to use
        """
        # Get API keys from dependencies
        secrets_impl = resolver(_openai_api_key)
        assert core.implements_interface(secrets_impl, core.SecretRetriever[str])
        api_key = await secrets_impl.get_secret()

        # Get the concurrency limit similarly
        concurrency_limit_impl = resolver(concurrency_limit)
        assert core.implements_interface(
            concurrency_limit_impl,
            core.RuntimeEnvRetriever[index.ConcurrencyLimitConfig],
        )
        concurrency_limit_config = await concurrency_limit_impl.get_runtime_config()
        lmt = concurrency_limit_config.limit
        assert lmt == 42, f"Expected fake concurrency limit to be exactly 42, got {lmt}"

        # Get the coordinator similarly
        coordinator_impl = resolver(coordinator)
        assert core.implements_interface(
            coordinator_impl,
            core.StorageHandle[index.FakeCoordinatorValue],
            allow_untyped_impl=True,
        )
        coordinator_value = (await coordinator_impl.retrieve()).value
        assert (
            coordinator_value == 5678
        ), f"Expected fake coordinator to be exactly 5678, got {coordinator_value}"

        # Initialize OpenAI client
        client = openai.AsyncOpenAI(
            api_key=api_key,
        )

        return cls(model_name=model_name, client=client)

    async def generate(
        self, system_prompt: index.SystemPrompt, user_message: index.SimpleUserMessage
    ) -> index.SimpleResponse:
        """Generate a response using the OpenAI model.

        Args:
            system_prompt: The system prompt to use
            user_message: The user message to respond to

        Returns:
            A SimpleResponse containing the generated text
        """

        await anyio.sleep(0.1 + random.random() * 0.2)
        response_text = f"Fake response {random.randint(0, 10000)}"

        return index.SimpleResponse(
            system_prompt=system_prompt,
            user_message=user_message,
            text=response_text,
        )
