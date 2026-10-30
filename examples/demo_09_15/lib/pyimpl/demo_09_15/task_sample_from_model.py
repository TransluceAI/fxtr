"""Task for sampling responses from a model."""

from demo_09_15_defns import index

from fxtr.object_model import core


class Task_SampleFromModel(core.TaskFunction[index.SimpleResponse]):
    """Task implementation for sampling responses from a model."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        prompt_handle: core.Plan[core.StorageHandle[index.SimpleUserMessage]],
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
        """Sample a response from the model using the prompt."""
        # Get the prompt data
        prompt_impl = self._prompt_handle
        prompt_data = await prompt_impl.retrieve()

        # The prompt data should be a SimpleUserMessage
        if not isinstance(prompt_data, index.SimpleUserMessage):
            raise ValueError(f"Expected SimpleUserMessage, got {type(prompt_data)}")

        # Get the model
        model_impl = self._model

        # Create a simple system prompt for the evaluation
        system_prompt = index.SystemPrompt(
            text="You are a helpful AI assistant. Please respond to the user's request."
        )

        # Generate response
        response = await model_impl.generate(system_prompt, prompt_data)

        return response
