"""Task for sampling responses from a model."""

from demo_agentic_misalignment_defns import index

from fxtr.object_model import core


class Task_SampleFromModel(core.TaskFunction[index.SimpleResponse]):
    """Task implementation for sampling responses from a model."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        prompt_handle: core.Plan[core.StorageHandle[index.PromptVariation]],
        model: core.Plan[index.SimpleSingleTurnModel],
    ):
        """Initialize the sampling task.

        Args:
            resolver: Plan resolver to get dependencies
            prompt_handle: Handle to the prompt data
            model: The model to sample from
        """
        # Get dependencies
        self._prompt_handle = resolver(prompt_handle)
        self._model = resolver(model)

    async def run_task(self) -> index.SimpleResponse:
        """Sample a response from the model using the prompt variation."""
        # Get the prompt variation data
        prompt_impl = self._prompt_handle
        prompt_data = await prompt_impl.retrieve()

        # The prompt data should be a PromptVariation
        if not isinstance(prompt_data, index.PromptVariation):
            raise ValueError(f"Expected PromptVariation, got {type(prompt_data)}")

        # Get the model
        model_impl = self._model

        # Use the system prompt and user message from the variation
        response = await model_impl.generate(prompt_data.system_prompt, prompt_data.user_message)

        return response
