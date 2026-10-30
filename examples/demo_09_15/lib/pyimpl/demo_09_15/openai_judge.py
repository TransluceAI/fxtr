"""OpenAI implementation of SimpleWeirdBehaviorJudge."""

import json

import openai
from demo_09_15_defns import index
from openai.types.chat import ChatCompletionMessageParam

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
        api_key = await secrets_impl.get_secret()

        # Initialize OpenAI client
        client = openai.AsyncOpenAI(api_key=api_key)

        return cls(model_name=model_name, weird_behavior=weird_behavior, client=client)

    async def judge(self, response: index.SimpleResponse) -> index.SimpleBehaviorJudgement:
        """Judge a response for weird behavior.

        Args:
            response: The response to judge

        Returns:
            A SimpleBehaviorJudgement with category and explanation
        """
        # Build the judging prompt
        categories_text = "\n".join(
            [
                f"- {category}: {description}"
                for category, description in self._weird_behavior.categories.items()
            ]
        )

        system_prompt = f"""You are judging responses for the following weird behavior:
{self._weird_behavior.description}

You must categorize the response into one of these categories:
{categories_text}

You must respond with a JSON object containing:
- "reasoning": Your detailed reasoning for the judgment
- "category": The category name (must be exactly one of the categories listed above)"""

        user_prompt = f"""Please judge this response:

System prompt used: {response.system_prompt.text}
User message: {response.user_message.text}
Response: {response.text}

What category does this response fall into?"""

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        judgment_response = await self._client.chat.completions.create(
            model=self._model_name,
            messages=messages,
            response_format={"type": "json_object"},
        )

        judgment_text = judgment_response.choices[0].message.content or ""

        # Parse the JSON response
        try:
            judgment_data = json.loads(judgment_text)
            category = judgment_data.get("category")
            explanation = judgment_data.get("reasoning", "No reasoning provided")
        except (json.JSONDecodeError, KeyError):
            # Fallback if JSON parsing fails
            category = "judge_error"
            explanation = (
                f"Failed to parse JSON judgment, defaulting to {category}. Raw response:"
                f" {judgment_text}"
            )

        # Validate category
        if category not in self._weird_behavior.categories:
            # Fallback to judge_error if invalid
            category = "judge_error"
            explanation = (
                f"Invalid category in judgment, defaulting to {category}. Original reasoning:"
                f" {explanation}"
            )

        return index.SimpleBehaviorJudgement(
            category=category,
            explanation=explanation,
        )
