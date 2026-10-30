"""Task for collecting and summarizing target behavior results."""

from demo_agentic_misalignment_defns import index
from frozendict import frozendict

from fxtr.object_model import core


class Task_CollectTargetBehaviorResults(core.TaskFunction[index.TargetBehaviorDemoResult]):
    """Task implementation for collecting target behavior results."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        target_behavior: index.TargetBehaviorConfig,
        variation_target: str,
        judge: core.Plan[index.SimpleTargetBehaviorJudge],
        var_gen_handle: core.Plan[core.StorageHandle[index.PromptVariationsSweep]],
        sample_handles: dict[
            str, tuple[tuple[core.Plan[core.StorageHandle[index.SimpleResponse]], ...], ...]
        ],
        judge_handles: dict[
            str,
            tuple[tuple[core.Plan[core.StorageHandle[index.SimpleBehaviorJudgement]], ...], ...],
        ],
    ):
        """Initialize the collection task.

        Args:
            resolver: Plan resolver to get dependencies
            target_behavior: The target behavior configuration
            variation_target: The part of the system prompt that was varied
            judge: The judge plan used
            var_gen_handle: Handle to the variation generation results
            sample_handles: Handles to all sample results, keyed by model name
            judge_handles: Handles to all judgment results, keyed by model name
        """
        self._target_behavior = target_behavior
        self._variation_target = variation_target
        self._judge_plan = judge

        # Get all dependencies
        self._var_gen_handle = resolver(var_gen_handle)

        # Build sample and judge handle structures by model
        self._sample_handles = {}
        self._judge_handles = {}

        for model_name, model_sample_groups in sample_handles.items():
            model_sample_handles = []
            for group in model_sample_groups:
                sample_group = [resolver(plan) for plan in group]
                model_sample_handles.append(sample_group)
            self._sample_handles[model_name] = model_sample_handles

        for model_name, model_judge_groups in judge_handles.items():
            model_judge_handles = []
            for group in model_judge_groups:
                judge_group = [resolver(plan) for plan in group]
                model_judge_handles.append(judge_group)
            self._judge_handles[model_name] = model_judge_handles

    async def run_task(self) -> index.TargetBehaviorDemoResult:
        """Collect and summarize all results."""
        # Get the variation generation results
        var_gen_impl = self._var_gen_handle
        var_gen_data = await var_gen_impl.retrieve()

        if not isinstance(var_gen_data, index.PromptVariationsSweep):
            raise ValueError(f"Expected PromptVariationsSweep, got {type(var_gen_data)}")

        # Collect results for each variation
        conditions = []

        for variation_idx, variation in enumerate(var_gen_data.variations):
            # Collect responses and judgements for all models for this variation
            responses_by_model = {}
            judgements_by_model = {}
            judgement_counts_by_model = {}

            for model_name in self._sample_handles.keys():
                # Get all sample results for this variation and model
                sample_group = (
                    self._sample_handles[model_name][variation_idx]
                    if variation_idx < len(self._sample_handles[model_name])
                    else []
                )
                judge_group = (
                    self._judge_handles[model_name][variation_idx]
                    if variation_idx < len(self._judge_handles[model_name])
                    else []
                )

                model_responses = []
                model_judgements = []

                for sample_impl in sample_group:
                    sample_data = await sample_impl.retrieve()
                    if isinstance(sample_data, index.SimpleResponse):
                        model_responses.append(sample_data)

                for judge_impl in judge_group:
                    judge_data = await judge_impl.retrieve()
                    if isinstance(judge_data, index.SimpleBehaviorJudgement):
                        model_judgements.append(judge_data)

                # Count judgements by category for this model
                model_judgement_counts = {}
                for judgement in model_judgements:
                    category = judgement.category
                    model_judgement_counts[category] = model_judgement_counts.get(category, 0) + 1

                responses_by_model[model_name] = tuple(model_responses)
                judgements_by_model[model_name] = tuple(model_judgements)
                judgement_counts_by_model[model_name] = frozendict(model_judgement_counts)

            # Create summary for this condition
            condition_summary = index.TargetBehaviorResponseSummary(
                target_behavior=self._target_behavior,
                variant_description=variation.variant_description,
                system_prompt=variation.system_prompt,
                user_message=variation.user_message,
                responses=frozendict(responses_by_model),
                judge=self._judge_plan,
                judgements=frozendict(judgements_by_model),
                judgement_counts=frozendict(judgement_counts_by_model),
            )

            conditions.append(condition_summary)

        return index.TargetBehaviorDemoResult(
            target_behavior=self._target_behavior,
            variation_target=self._variation_target,
            conditions=tuple(conditions),
        )
