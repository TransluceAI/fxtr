Note: "playground" files do not need to be fixed (they have been omitted here)

# **STEP 1:** FIX `grep -rn "define_protocol" --include="*.py" --exclude-dir=".venv" .`

> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:8:@core.define_protocol("SequenceProcessor^1", namespace_id="!testing.impl_debug_stuff")
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:92:@core.define_protocol("Calculator^1", namespace_id="!testing.capability_passing")
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:112:@core.define_protocol("CalculatorFactory^1", namespace_id="!testing.capability_passing")
> DONE examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py:62:    @core.define_protocol("SimpleSingleTurnModel^1", namespace_id=NAMESPACE_ID)
> DONE examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py:91:    @core.define_protocol("SimpleTargetBehaviorJudge^1", namespace_id=NAMESPACE_ID)
> DONE examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py:61:    @core.define_protocol("SimpleSingleTurnModel^1", namespace_id=NAMESPACE_ID)
> DONE examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py:90:    @core.define_protocol("SimpleWeirdBehaviorJudge^1", namespace_id=NAMESPACE_ID)
> DONE examples/demo_09_15_fake/lib/blueprint/demo_09_15_fake_defns/index.py:61:    @core.define_protocol("SimpleSingleTurnModel^1", namespace_id=NAMESPACE_ID)
> DONE examples/demo_09_15_fake/lib/blueprint/demo_09_15_fake_defns/index.py:90:    @core.define_protocol("SimpleWeirdBehaviorJudge^1", namespace_id=NAMESPACE_ID)
> DONE fxtr/builtin_util/watchable.py:12:@core.define_protocol(
> DONE fxtr/providers/uv_py_provider.py:112:@core.define_protocol("UVSpawnerProtocol^1", namespace_id="!builtin.uv_provider")
> DONE fxtr/providers/uv_py_provider.py:130:@core.define_protocol("UVRegisterProtocol^1", namespace_id="!builtin.uv_provider")
> DONE fxtr/object_model/core.py:4009:@define_protocol("TaskFunction^1", namespace_id="!builtin.tasks")
> DONE fxtr/object_model/core.py:4024:@define_protocol("StorageHandle^1", namespace_id="!builtin.tasks")
> DONE fxtr/object_model/core.py:4230:@define_protocol("SecretsProtocol^1", namespace_id="!builtin")
> fxtr/object_model/collection_tracking.py:366:                    " registered with @define_protocol)"
> DONE fxtr/api/backend_runner.py:31:@core.define_protocol("TaskWaiter^1", namespace_id="!builtin.backend_runner")
> DONE fxtr/api/backend_runner.py:44:@core.define_protocol("BackendRunnerInterface^1", namespace_id="!builtin.backend_runner")

TODOs for each of these: change e.g.
```python
@define_protocol("StorageHandle^1", namespace_id="!builtin.tasks")
class StorageHandle(Protocol):
    """A handle that can ...[rest of docstring]"""
    async def retrieve(self) -> EntityBase:
        """Retrieve the entity from storage."""
        ...
```

to

```python
class StorageHandle(core.FxtrCallTarget, interface_id="StorageHandle^1", namespace_id="!builtin.tasks"):
    """A handle that can ...[rest of docstring]"""

    @abc.abstractmethod
    async def retrieve(self) -> EntityBase:
        """Retrieve the entity from storage."""
        ...

```
(so: metaclass kwargs, interface_id arg, @abc.abstractmethod decorator.)

Also make sure `abc` and `from fxtr.object_model import core` are at the top of each file also.


# **STEP 2:** FIX `grep -rn "FxtrImplClass" --include="*.py" --exclude-dir=".venv" .`

> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:34:class SimpleWrapperImpl(SequenceProcessor, impl_base.FxtrImplClass):
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:65:class RepeaterImpl(SequenceProcessor, impl_base.FxtrImplClass):
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:162:class CalculatorImpl(Calculator, impl_base.FxtrImplClass):
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:181:class CalculatorFactoryImpl(CalculatorFactory, impl_base.FxtrImplClass):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py:11:class Task_SampleFromModel(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py:11:class Task_TargetBehaviorJudge(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_model.py:13:class SimpleSingleTurnModel_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleSingleTurnModel):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_judge.py:14:class SimpleBehaviorJudge_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleTargetBehaviorJudge):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/storage_projector.py:9:class StorageHandle_Project(core.StorageHandle, impl_base.FxtrImplClass):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:12:class Task_CollectTargetBehaviorResults(impl_base.FxtrImplClass, core.TaskFunction):
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_generate_variations.py:12:class Task_GenerateVariationsOfSystemPrompt(impl_base.FxtrImplClass, core.TaskFunction):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_generate_ideas.py:12:class Task_GenerateIdeas(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py:11:class Task_SampleFromModel(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py:11:class Task_WeirdBehaviorJudge(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_model.py:13:class SimpleSingleTurnModel_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleSingleTurnModel):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_judge.py:14:class SimpleBehaviorJudge_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleWeirdBehaviorJudge):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/storage_projector.py:9:class StorageHandle_Project(core.StorageHandle, impl_base.FxtrImplClass):
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:12:class Task_CollectWeirdBehaviorResults(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_generate_ideas.py:11:class Task_GenerateIdeas(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_sample_from_model.py:11:class Task_SampleFromModel(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_judge_behavior.py:11:class Task_WeirdBehaviorJudge(core.TaskFunction, impl_base.FxtrImplClass):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_model.py:14:class SimpleSingleTurnModel_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleSingleTurnModel):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_judge.py:14:class SimpleBehaviorJudge_OpenAIImpl(impl_base.FxtrImplClass, index.SimpleWeirdBehaviorJudge):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/storage_projector.py:9:class StorageHandle_Project(core.StorageHandle, impl_base.FxtrImplClass):
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:12:class Task_CollectWeirdBehaviorResults(core.TaskFunction, impl_base.FxtrImplClass):
> DONE fxtr/builtin_util/watchable.py:55:class _EvtWatchable(Watchable, impl_base.FxtrImplClass):
> DONE fxtr/providers/uv_py_provider.py:775:class _UVRegisterServerImpl(_UV_REGISTER_PROTOCOL, impl_base.FxtrImplClass):
> DONE fxtr/providers/externals_provider.py:48:class SecretsRetriever(core.SecretsProtocol, impl_base.FxtrImplClass):
> DONE fxtr/providers/externals_provider.py:58:class RunDataRetriever(core.StorageHandle, impl_base.FxtrImplClass):
> DONE fxtr/providers/uv_py_provider_entrypoint.py:33:class _SpawnerImpl(_UV_SPAWNER_PROTOCOL, impl_base.FxtrImplClass):
> DONE fxtr/api/backend_runner.py:112:class TaskWaiterImpl(TaskWaiter, impl_base.FxtrImplClass):
> DONE fxtr/api/backend_runner.py:714:class BackendRunnerInterfaceImpl(BackendRunnerInterface, impl_base.FxtrImplClass):


TODOs for each of these: change e.g.
```python

class SimpleWrapperImpl(SequenceProcessor, impl_base.FxtrImplClass):
    """docstring"""

    def __init__(...):
        ...

    async def process_seq(self, some_list_of_entities: list[core.AnyEntity]) -> list[core.AnyEntity]:
        ... # method here
```

to

```python

class SimpleWrapperImpl(SequenceProcessor):
    """docstring"""
    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(...):
        ...

    async def process_seq(self, some_list_of_entities: list[core.AnyEntity]) -> list[core.AnyEntity]:
        ... # method here

```
(so, remove FxtrImplClass, and add the interface_spec line)

Also make sure `from fxtr.object_model import core` is at the top of each file also.


# **STEP 3:** FIX ` grep -rn "AnyProtocolImplPlan" --include="*.py" --exclude-dir=".venv" .`

> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:37:        _unused_deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl] | None = None,
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:79:    deps: dict[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py:19:        prompt_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py:20:        model: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py:19:        sample_result: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py:20:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_model.py:29:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_judge.py:38:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/storage_projector.py:14:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/storage_projector.py:17:        handle: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:17:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:22:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:23:        var_gen_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:24:        sample_handles: Mapping[str, tuple[tuple[core.AnyProtocolImplPlan, ...], ...]],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:25:        judge_handles: Mapping[str, tuple[tuple[core.AnyProtocolImplPlan, ...], ...]],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_generate_variations.py:17:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_generate_variations.py:25:        investigator_model: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_generate_ideas.py:17:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_generate_ideas.py:22:        investigator_model: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py:19:        prompt_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py:20:        model: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py:19:        sample_result: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py:20:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_model.py:29:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_judge.py:38:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/storage_projector.py:14:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/storage_projector.py:17:        handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:17:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:21:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:22:        idea_gen_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:23:        sample_handles: tuple[tuple[core.AnyProtocolImplPlan, ...], ...],
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:24:        judge_handles: tuple[tuple[core.AnyProtocolImplPlan, ...], ...],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_generate_ideas.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_generate_ideas.py:21:        investigator_model: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_sample_from_model.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_sample_from_model.py:19:        prompt_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_sample_from_model.py:20:        model: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_judge_behavior.py:16:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_judge_behavior.py:19:        sample_result: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_judge_behavior.py:20:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_model.py:30:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_judge.py:38:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/storage_projector.py:14:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/storage_projector.py:17:        handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:17:        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:21:        judge: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:22:        idea_gen_handle: core.AnyProtocolImplPlan,
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:23:        sample_handles: tuple[tuple[core.AnyProtocolImplPlan, ...], ...],
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:24:        judge_handles: tuple[tuple[core.AnyProtocolImplPlan, ...], ...],
> fxtr/providers/uv_py_provider.py:107:    plan: core.AnyProtocolImplPlan
> fxtr/providers/uv_py_provider.py:305:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/uv_py_provider.py:306:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/externals_provider.py:83:    dependent_plans: list[core.AnyProtocolImplPlan],
> fxtr/providers/externals_provider.py:137:        all_plans: set[core.AnyProtocolImplPlan] | None = None,
> fxtr/providers/externals_provider.py:158:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/externals_provider.py:159:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/externals_provider.py:279:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/externals_provider.py:280:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/in_process_py_provider.py:47:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/in_process_py_provider.py:48:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/in_process_py_provider.py:71:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/in_process_py_provider.py:116:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/providers/provider_base.py:54:        plan_provenance_pairs: Sequence[tuple[core.AnyProtocolImplPlan, core.AnyEntity | None]],
> fxtr/providers/provider_base.py:70:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/provider_base.py:71:        dependencies: Mapping[core.AnyProtocolImplPlan, FxtrCallTarget],
> fxtr/providers/provider_base.py:106:            core.AnyProtocolImplPlan,
> fxtr/providers/provider_base.py:107:            Mapping[core.AnyProtocolImplPlan, FxtrCallTarget],
> fxtr/providers/provider_base.py:115:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/provider_base.py:116:        dependencies: Mapping[core.AnyProtocolImplPlan, FxtrCallTarget],
> fxtr/providers/auto_provider.py:44:        plan_provenance_pairs: Sequence[tuple[core.AnyProtocolImplPlan, core.AnyEntity | None]],
> fxtr/providers/auto_provider.py:67:        plan: core.AnyProtocolImplPlan,
> fxtr/providers/auto_provider.py:68:        dependencies: Mapping[core.AnyProtocolImplPlan, core.FxtrCallTarget],
> fxtr/impl_plans/py_impl_plan.py:18:        deps: dict[core.AnyProtocolImplPlan, ProtocolImpl],
> fxtr/impl_plans/py_impl_plan.py:57:    impl_deps: frozenset[core.AnyProtocolImplPlan]
> fxtr/impl_plans/plan_dep_util.py:10:    plan: core.AnyProtocolImplPlan,
> fxtr/impl_plans/plan_dep_util.py:12:) -> Collection[core.AnyProtocolImplPlan]:
> fxtr/impl_plans/plan_dep_util.py:44:def _plan_deps_direct(plan: core.AnyProtocolImplPlan) -> frozenset[core.AnyProtocolImplPlan]:
> fxtr/impl_plans/plan_dep_util.py:64:def _plan_deps_recursive(plan: core.AnyProtocolImplPlan) -> frozenset[core.AnyProtocolImplPlan]:
> fxtr/api/task_launch_view/__init__.py:81:        plans: Collection[core.AnyProtocolImplPlan],
> fxtr/api/task_launch_view/__init__.py:98:        self.plan_status: dict[core.AnyProtocolImplPlan, str] = {plan: "pending" for plan in plans}
> fxtr/api/task_launch_view/__init__.py:99:        self.plan_msg: dict[core.AnyProtocolImplPlan, str] = {}
> fxtr/api/task_launch_view/__init__.py:293:        plan_status: Mapping[core.AnyProtocolImplPlan, str],
> fxtr/api/task_launch_view/__init__.py:294:        plan_msg: Mapping[core.AnyProtocolImplPlan, str],
> fxtr/api/backend_runner.py:82:    plan: core.AnyProtocolImplPlan
> fxtr/api/backend_runner.py:83:    dep_states: dict[core.AnyProtocolImplPlan, PlanWorkerState]
> fxtr/api/backend_runner.py:136:        plan: core.AnyProtocolImplPlan,
> fxtr/api/backend_runner.py:137:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/api/backend_runner.py:156:        plan: core.AnyProtocolImplPlan,
> fxtr/api/backend_runner.py:157:        dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrCallTarget],
> fxtr/api/backend_runner.py:199:        self._active_plans: dict[core.AnyProtocolImplPlan, PlanWorkerState] = {}
> fxtr/api/backend_runner.py:398:    async def _request_plan(self, plan: core.AnyProtocolImplPlan) -> PlanWorkerState:
> fxtr/api/backend_runner.py:412:    async def _setup_plan(self, plan: core.AnyProtocolImplPlan) -> PlanWorkerState:

Change all the references to `core.AnyProtocolImplPlan` to instead be references to `core.Plan[T]`. T can be inferred based on the type of call target / implementation the user expects to receive or pass. In situations where it is generic and there may be many plans, you can use Plan[Any].

Additionally, for any argument that is of the form `dependencies/deps: Mapping[core.AnyProtocolImplPlan, FxtrCallTarget]`, change it to instead be `resolver: core.PlanResolver`, and call it to resolve the plan.

Additionally, in fxtr/providers/in_process_py_provider.py, construct a resolver function instead of passing a dependency dict.


# **STEP 4**: FIX `grep -rn "impl_base" --include="*.py" --exclude-dir=".venv" .`

> DONE tests/test_tasks_and_providers.py:221:        from fxtr.impls import impl_base
> DONE tests/fxtr_testdeps/fxtr_testdeps/impl_fixtures.py:4:from fxtr.impls import impl_base
> DONE tests/test_uv_py_provider.py:17:from fxtr.impls import impl_base
> DONE tests/test_rpc.py:14:from fxtr.impls import impl_base, impl_remote, implserver
> DONE tests/test_in_process_py_provider.py:12:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py:7:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py:7:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_model.py:9:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_judge.py:10:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/storage_projector.py:5:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_collect_results.py:8:from fxtr.impls import impl_base
> DONE examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_generate_variations.py:8:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_generate_ideas.py:8:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py:7:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py:7:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_model.py:9:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/openai_judge.py:10:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/storage_projector.py:5:from fxtr.impls import impl_base
> DONE examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py:8:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_generate_ideas.py:7:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_sample_from_model.py:7:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_judge_behavior.py:7:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_model.py:10:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/openai_judge.py:10:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/storage_projector.py:5:from fxtr.impls import impl_base
> DONE examples/demo_09_15_fake/lib/pyimpl/demo_09_15_fake/task_collect_results.py:8:from fxtr.impls import impl_base
> DONE fxtr/builtin_util/watchable.py:8:from fxtr.impls import impl_base
> DONE fxtr/providers/uv_py_provider.py:96:from ..impls import impl_base, impl_remote
> DONE fxtr/providers/externals_provider.py:18:from ..impls import impl_base
> DONE fxtr/providers/uv_py_provider_entrypoint.py:27:from ..impls import impl_base, impl_remote, implserver
> DONE fxtr/providers/in_process_py_provider.py:13:from ..impls import impl_base
> DONE fxtr/api/backend_runner.py:20:from ..impls import impl_base
(other lines already referred to and fixed in earlier steps are removed for brevity)

impl_base is being deleted. Remove the imports, and delete this file also. FxtrCallTarget can be repointed to core where necessary.

**STEP 4 COMPLETE!** All impl_base imports removed and FxtrCallTarget references updated to core.FxtrCallTarget.


# **MANUAL** Add type variable support
TaskFunction
StorageHandle
Task
SequenceProcessor
