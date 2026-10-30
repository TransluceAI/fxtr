"""Task for judging target behaviors."""

from demo_agentic_misalignment_defns import index

from fxtr.object_model import core


class Task_TargetBehaviorJudge(core.TaskFunction[index.SimpleBehaviorJudgement]):
    """Task implementation for judging target behaviors."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        sample_result: core.Plan[core.StorageHandle[index.SimpleResponse]],
        judge: core.Plan[index.SimpleTargetBehaviorJudge],
    ):
        """Initialize the judging task.

        Args:
            resolver: Plan resolver to get dependencies
            sample_result: Handle to the sample result
            judge: The judge to use for evaluation
        """
        # Get dependencies
        self._sample_handle = resolver(sample_result)
        self._judge = resolver(judge)

    async def run_task(self) -> index.SimpleBehaviorJudgement:
        """Judge the sample result for target behavior."""
        # Get the sample data
        sample_impl = self._sample_handle
        sample_data = await sample_impl.retrieve()

        # The sample data should be a SimpleResponse
        if not isinstance(sample_data, index.SimpleResponse):
            raise ValueError(f"Expected SimpleResponse, got {type(sample_data)}")

        # Get the judge
        judge_impl = self._judge

        # Judge the response
        judgement = await judge_impl.judge(sample_data)

        return judgement
