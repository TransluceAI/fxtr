"""OpenAI implementation of SimpleSingleTurnModel."""

import openai
from demo_09_15_defns import index
from openai.types.chat import ChatCompletionMessageParam

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
    ):
        """Initialize the OpenAI model implementation.

        Args:
            resolver: Plan resolver to get dependencies
            model_name: The OpenAI model name to use
        """
        # Get API keys from dependencies
        secrets_impl = resolver(index.openai_api_key.handle)
        assert core.implements_interface(secrets_impl, core.SecretRetriever)

        # Get the secrets as a Python object
        secrets = secrets_impl
        api_key = await secrets_impl.get_secret()

        # Initialize OpenAI client
        client = openai.AsyncOpenAI(api_key=api_key)

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
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt.text},
            {"role": "user", "content": user_message.text},
        ]

        response = await self._client.chat.completions.create(
            model=self._model_name,
            messages=messages,
        )

        response_text = response.choices[0].message.content or ""

        return index.SimpleResponse(
            system_prompt=system_prompt,
            user_message=user_message,
            text=response_text,
        )
