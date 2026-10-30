"""Task for collecting and summarizing weird behavior results."""

from typing import Any

from demo_09_15_fake_defns import index
from frozendict import frozendict

from fxtr.object_model import core


class Task_CollectWeirdBehaviorResults(core.TaskFunction[index.WeirdBehaviorDemoResult]):
    """Task implementation for collecting weird behavior results."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        weird_behavior: index.WeirdBehaviorConfig,
        judge: Any,
        idea_gen_handle: core.Plan[core.StorageHandle[index.WeirdBehaviorElicitationPromptList]],
        sample_handles: tuple[tuple[core.Plan[core.StorageHandle[index.SimpleResponse]], ...], ...],
        judge_handles: tuple[
            tuple[core.Plan[core.StorageHandle[index.SimpleBehaviorJudgement]], ...], ...
        ],
    ):
        """Initialize the collection task.

        Args:
            resolver: Plan resolver to get dependencies
            weird_behavior: The weird behavior configuration
            judge: The judge plan used
            idea_gen_handle: Handle to the idea generation results
            sample_handles: Handles to all sample results
            judge_handles: Handles to all judgment results
        """
        self._weird_behavior = weird_behavior
        self._judge_plan = judge

        # Get all dependencies
        idea_gen_handle_impl = resolver(idea_gen_handle)
        assert core.implements_interface(
            idea_gen_handle_impl, core.StorageHandle[index.WeirdBehaviorElicitationPromptList]
        )
        self._idea_gen_handle = idea_gen_handle_impl

        # Build sample and judge handle structures
        self._sample_handles = []
        self._judge_handles = []

        for group in sample_handles:
            sample_group = [resolver(plan) for plan in group]
            self._sample_handles.append(sample_group)

        for group in judge_handles:
            judge_group = [resolver(plan) for plan in group]
            self._judge_handles.append(judge_group)

    async def run_task(self) -> index.WeirdBehaviorDemoResult:
        """Collect and summarize all results."""
        # Get the idea generation results
        idea_gen_impl = self._idea_gen_handle
        idea_gen_data = await idea_gen_impl.retrieve()

        if not isinstance(idea_gen_data, index.WeirdBehaviorElicitationPromptList):
            raise ValueError(
                f"Expected WeirdBehaviorElicitationPromptList, got {type(idea_gen_data)}"
            )

        # Collect results for each prompt
        conditions = []

        for prompt_idx, user_message in enumerate(idea_gen_data.user_messages):
            # Get all sample results for this prompt
            sample_group = (
                self._sample_handles[prompt_idx] if prompt_idx < len(self._sample_handles) else []
            )
            judge_group = (
                self._judge_handles[prompt_idx] if prompt_idx < len(self._judge_handles) else []
            )

            responses = []
            judgements = []

            for sample_impl in sample_group:
                sample_data = await sample_impl.retrieve()
                if isinstance(sample_data, index.SimpleResponse):
                    responses.append(sample_data)

            for judge_impl in judge_group:
                judge_data = await judge_impl.retrieve()
                if isinstance(judge_data, index.SimpleBehaviorJudgement):
                    judgements.append(judge_data)

            # Count judgements by category
            judgement_counts = {}
            for judgement in judgements:
                category = judgement.category
                judgement_counts[category] = judgement_counts.get(category, 0) + 1

            # Create summary for this condition
            condition_summary = index.WeirdBehaviorResponseSummary(
                weird_behavior=self._weird_behavior,
                system_prompt=idea_gen_data.system_prompt,
                user_message=user_message,
                responses=tuple(responses),
                judge=self._judge_plan,
                judgements=tuple(judgements),
                judgement_counts=frozendict(judgement_counts),
            )

            conditions.append(condition_summary)

        return index.WeirdBehaviorDemoResult(
            weird_behavior=self._weird_behavior,
            conditions=tuple(conditions),
        )
