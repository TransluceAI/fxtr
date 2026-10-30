Overall goal: Revamp how protocol specs are defined.

# Current state

Protocols are defined in the "Protocols and their type specifications" section of core.py. They consist of:
- A "SendableValue" type and typechecking logic (this does not need to change)
- A type `ProtocolSpec` which identifies a type of interface by which objects will communicate
- A decorator `define_protocol` for registering a python Protocol as a ProtocolSpec, and a function `protocol_spec` for retrieving the protocol spec from the protocol
- An abstract base class `FxtrProtocolImpl` that declares that a class implements one of those protocols
- A function `implements_protocol` that somewhat unsafely checks a protocol
- A type `AnyProtocolImplPlan` which refers to some type that is a "protocol implementation plan"

They are then used in a variety of ways:
- Various implementations of things throughout the system use define_protocol and work with the protocol types
- Impl plans in `fxtr/impl_plans` are defined using the system
- Implementation objects in `fxtr/impls` generally are built out of `FxtrProtocolImpl`; these also allow them to be proxied over RPC
- There are providers in `fxtr/providers` that provide implementations based on plans

All of the implementation constructors, as specified by `PyImplPlan`, take as input a deps map which maps plans to their impls. But plans are not annotated with any types, and so users have to cast them.

# More important details about current state

## Key Current Patterns

### Protocol Definition Pattern
```python
# In core.py or example index.py
@core.define_protocol("SimpleSingleTurnModel^1", namespace_id=NAMESPACE_ID)
class SimpleSingleTurnModel(Protocol):
    async def generate(self, system_prompt: SystemPrompt, user_message: SimpleUserMessage) -> SimpleResponse:
        ...
```

### Implementation Constructor Patterns

**Pattern 1: With deps parameter (most common)**
```python
class Task_GenerateIdeas(core.TaskFunction, impl_base.FxtrImplClass):
    def __init__(
        self,
        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
        /,
        *,
        investigator_model: core.AnyProtocolImplPlan,  # plan passed as kwarg
        num_prompts: int,
    ):
        # Extract implementation from deps using plan from kwargs
        self._investigator = deps[investigator_model]
        assert core.implements_protocol(self._investigator, SimpleSingleTurnModel)
```

**Pattern 2: Async initializer with _ainit**
```python
class SimpleSingleTurnModel_OpenAIImpl(impl_base.FxtrImplClass, SimpleSingleTurnModel):
    def __init__(self, *, model_name: str, client: openai.AsyncOpenAI):
        self._model_name = model_name
        self._client = client

    @classmethod
    async def _ainit(
        cls,
        deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
        /,
        *,
        model_name: str,
    ):
        secrets_impl = deps[index.openai_api_keys]
        assert core.implements_protocol(secrets_impl, core.SecretsProtocol)
        api_keys = await secrets_impl.get_secrets()
        client = openai.AsyncOpenAI(api_key=api_keys["OPENAI_API_KEY"])
        return cls(model_name=model_name, client=client)
```

### PyImplPlan Creation Pattern
```python
def setup_openai_single_turn_model(
    model_name: str,
) -> Annotated[PyImplPlan, plan_for_protocol(SimpleSingleTurnModel)]:
    return PyImplPlan(
        protocol_spec=core.protocol_spec(SimpleSingleTurnModel),
        requires="demo_09_15==0.0.1",
        builder="demo_09_15.openai_model:SimpleSingleTurnModel_OpenAIImpl._ainit",
        kwargs=frozendict({"model_name": model_name}),
        impl_deps=frozenset({openai_api_keys}),  # Dependencies declared here
    )
```

### Type Annotation Patterns
```python
# For plan fields
model: Annotated[Any, plan_for_protocol(SimpleSingleTurnModel)]

# For plan variables
model_plan: core.AnyProtocolImplPlan

# For function returns
def setup_model(...) -> Annotated[PyImplPlan, plan_for_protocol(SimpleSingleTurnModel)]:
    ...
```

### Provider Pattern (InProcessPyProvider)
```python
async def provide_one_no_provenance(
    self,
    plan: PyImplPlan,
    dependencies: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl],
) -> impl_base.FxtrProtocolImpl:
    # Load builder function from entry-point string
    builder_func = _load_object_from_entrypoint(plan.builder)

    # Create deps dict from impl_deps
    deps = {dep: dependencies[dep] for dep in plan.impl_deps}

    # Call builder with deps dict
    retval = builder_func(deps, **plan.kwargs)
    if inspect.isawaitable(retval):
        retval = await retval

    return retval
```

### RPC Pattern (Current RemoteSubtargetImpl)
```python
class RemoteSubtargetImpl(core.FxtrProtocolImpl):
    def __init__(self, connection: FxtrRPCConnection, handle_id: int, protocol: core.ProtocolSpec):
        self._connection = connection
        self._handle_id = handle_id
        self._protocol = protocol

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(f"Attribute {name} not found")
        return functools.partial(self._method_call, name)

    async def _method_call(self, method: str, *args: core.SendableValue, **kwargs: core.SendableValue):
        return await self._connection.call(self._handle_id, method, *args, **kwargs)

    def protocol_spec(self) -> core.ProtocolSpec:
        return self._protocol
```

## Key Classes Inheriting from FxtrProtocolImpl

**Direct inheritors (need immediate updates):**
1. `FxtrImplClass` (impl_base.py) - Base mixin for most implementations
2. `RemoteSubtargetImpl` (rpc.py) - RPC proxy for remote handles
3. `RemoteImpl` (impl_remote.py) - RPC client managing connection
4. Playground classes (can be deleted)

**Indirect inheritors (via FxtrImplClass, ~30+ classes):**
- All task implementations in examples (Task_GenerateIdeas, Task_SampleFromModel, etc.)
- All model implementations (SimpleSingleTurnModel_OpenAIImpl, etc.)
- All judge implementations (SimpleWeirdBehaviorJudge_OpenAIImpl, etc.)
- All storage projectors (StorageHandle_Project)
- Test fixtures (SequenceProcessor implementations, Calculator implementations)

## Protocols Currently Defined with @define_protocol

**Built-in protocols (8 total):**
1. `TaskFunction` (core.py:3946)
2. `StorageHandle` (core.py:3961)
3. `SecretsProtocol` (core.py:4167)
4. `TaskWaiter` (backend_runner.py:31)
5. `BackendRunnerInterface` (backend_runner.py:44)
6. `UVSpawnerProtocol` (uv_py_provider.py:112)
7. `UVRegisterProtocol` (uv_py_provider.py:130)
8. Watchable protocols (watchable.py:12)

**Test protocols (3 total):**
1. `SequenceProcessor` (fxtr_testdeps/impl_fixtures.py:8)
2. `Calculator` (fxtr_testdeps/impl_fixtures.py:92)
3. `CalculatorFactory` (fxtr_testdeps/impl_fixtures.py:112)

**Example protocols (6 total):**
1. `SimpleSingleTurnModel` in demo_09_15 (index.py:61)
2. `SimpleWeirdBehaviorJudge` in demo_09_15 (index.py:90)
3. `SimpleSingleTurnModel` in demo_09_15_fake (index.py:61)
4. `SimpleWeirdBehaviorJudge` in demo_09_15_fake (index.py:90)
5. `SimpleSingleTurnModel` in demo_agentic_misalignment (index.py:62)
6. `SimpleTargetBehaviorJudge` in demo_agentic_misalignment (index.py:91)

**Total: 17 protocol definitions** to convert from @define_protocol to FxtrCallTarget subclassing

# Desired state

## High level
The system should be redesigned to be based on type annotations, targetting Python 3.12 (so it can use dedicated type syntax). The new system is mocked up in fxtr/playground/protocol_playground_v2.py.

IMPORTANT: READ THE protocol_playground_v2 FILE, it tells you what patterns the migration should use!

Key idea:
- Instead of `FxtrProtocolImpl` and `define_protocol`, we define new protocol specs by subclassing `FxtrCallTarget`, then implement them by subclassing that. Types are virtual subclasses of base classes with equal protocol specs. Protocol specs are always class vars.
- Instead of doing weird stuff to handle AnyProtocolImplPlan, we will have a new abstract base class `Plan[T]` indicating a plan that will be instantiated as (the protocol spec of) type T.
- Instead of `protocol_spec(cls)` we can just do `cls.protocol_spec`.
- Remote implementations use dynamic subclasses stored in a cache to map received handles to their rpc versions. This should use a dynamic wrapper that wraps a target; the specific target class can then have the logic that RemoteImpl or RemoteSubtargetImpl currently have.
- PyImplPlan and its associated providers (e.g. fxtr/providers/in_process_py_provider.py) should pass dependencies using a "resolver" function (defined in `impl_base`) which maps from a Plan[T] to a T
- All implementations of `FxtrProtocolImpl` should be modified to be based on `FxtrCallTarget` instead. All constructors for them should be modified to take the resolver instead of the deps dict, and use it to instantiate what they need.

## Medium level / TODO list

To implement this, we need to make the following changes across the project:

### Phase 1: Core Type System Changes (fxtr/object_model/core.py)

1. **Add Plan[T] generic base class** (new code after ProtocolSpec definition, around line 2760)
   - Define `class Plan[T: FxtrCallTarget](abc.ABC)` with a `protocol_spec: ClassVar[ProtocolSpec[T]]` attribute
   - This replaces the `AnyProtocolImplPlan` type alias

2. **Add FxtrCallTarget base class** (new code in place of FxtrProtocolImpl, around line 2529)
   - Define `class FxtrCallTarget(abc.ABC)` with `protocol_spec: ClassVar[ProtocolSpec]` attribute
   - Implement `__init_subclass__` hook that:
     - For direct subclasses with `type_id` parameter: creates the protocol spec and adds `__subclasshook__` for virtual subclassing
     - For indirect subclasses: validates they implement all required methods and inherit the protocol spec
   - This replaces `FxtrProtocolImpl` abstract base class AND the `define_protocol` function

3. **Update protocol_spec function** (around line 2774)
   - Change from using `_PROTOCOL_REGISTRY` to accessing `cls.protocol_spec` class variable
   - Eventually this function may be deprecated in favor of direct `cls.protocol_spec` access, but keep it for now as a convenience

4. **Remove implements_protocol function** (around line 2959)
   - (users can just use isinstance)

5. **Remove plan_for_protocol helper** (around line 3166)
   - (users can use new `Plan[T]` generic type)

6. **Update SendableValue type alias** (around line 2546)
   - Change `FxtrProtocolImpl` to `FxtrCallTarget` in the union

### Phase 2: Implementation Base Classes (fxtr/impls/impl_base.py)

7. **Add Resolver protocol type** (at top of file)
   - Define `Resolver = Callable[[Plan[T]], T]` protocol for dependency resolution
   - This replaces the `deps: Mapping[AnyProtocolImplPlan, FxtrProtocolImpl]` pattern

8. **Update FxtrImplClass mixin** (entire class, around line 20-64)
   - Change base class from `FxtrProtocolImpl` to `FxtrCallTarget`
   - Keep the automatic protocol spec inference logic in `__init_subclass__`
   - Update `protocol_spec()` method to return `cls._fxtr_protocol_spec`

### Phase 3: Plan Types (fxtr/impl_plans/)

9. **Update PyImplPlan entity** (fxtr/impl_plans/py_impl_plan.py)
   - Make `PyImplPlan` a generic class: `class PyImplPlan[T: FxtrCallTarget](core.Plan[T], core.DataEntityBase, ...)`
   - Update builder signature documentation to use `Resolver` instead of deps dict
   - Update `protocol_spec` field to be `ClassVar[ProtocolSpec[T]]` inherited from `Plan[T]`

10. **Update ExternalServiceIdentifier** (in core.py around line 2965)
    - Make it generic: `class ExternalServiceIdentifier[T: FxtrCallTarget](core.Plan[T], ...)`
    - Update `protocol_spec` field appropriately

11. **Update TaskResultHandle** (in core.py, search for TaskResultHandle)
    - Make it generic: `class TaskResultHandle[T: FxtrCallTarget](core.Plan[T], ...)`
    - Update `protocol_spec` field appropriately

### Phase 4: Provider Updates (fxtr/providers/)

12. **Update InProcessPyProvider** (fxtr/providers/in_process_py_provider.py, around line 113-198)
    - In `provide_one_no_provenance`, instead of building deps dict, create a resolver function
    - Resolver function should close over `dependencies` mapping and return `dependencies[plan]`
    - Pass resolver as first argument to builder instead of deps dict
    - Update type annotations for `dependencies` parameter to use `Plan[FxtrCallTarget]` keys

13. **Update UVManagedPyImplProvider** (fxtr/providers/uv_py_provider.py, around line 341-348)
    - Update to pass resolver function through to subprocess
    - Update type annotations appropriately

14. **Update UVSpawner** (fxtr/providers/uv_py_provider_entrypoint.py, around line 45-65)
    - Update to receive and use resolver pattern
    - May need to serialize resolver behavior over RPC boundary

15. **Update AutoProvider** (fxtr/providers/auto_provider.py)
    - Update type annotations from `AnyProtocolImplPlan` to `Plan[FxtrCallTarget]`
    - Ensure it works with new provider interfaces

### Phase 5: RPC System Updates (fxtr/impls/)

16. **Create ProxiedCallTarget base class** (new file or in rpc.py, before RemoteSubtargetImpl)
    - Define `class ProxiedCallTarget(FxtrCallTarget, type_id=NotImplemented)` with `_proxied_target` attribute
    - This is the base for all dynamically generated proxy classes

17. **Add dynamic proxy class cache** (in rpc.py, module level)
    - Create `_proxy_class_cache: dict[cbrrr.CID, type[FxtrCallTarget]] = {}`
    - Create `get_or_create_proxy_class(protocol_spec: ProtocolSpec) -> type[FxtrCallTarget]` function
    - Function should use `types.new_class()` to create dynamic subclass with protocol spec methods

18. **Update RemoteSubtargetImpl** (fxtr/impls/rpc.py, lines 828-866)
    - Convert to a "wrapper target" class that doesn't inherit from FxtrCallTarget directly
    - Should have `call_method(method, *args, **kwargs)` method for actual RPC dispatch
    - This will be wrapped by dynamic proxy classes

19. **Update RPC deserialization** (fxtr/impls/rpc.py, around line 243-280 in _cbor_to_sendable_value)
    - Instead of directly instantiating `RemoteSubtargetImpl`, call `get_or_create_proxy_class(protocol_spec)`
    - Instantiate the dynamic class with a `RemoteSubtargetImpl` wrapper as the proxied target
    - Update weak reference management to work with new structure

20. **Update RemoteImpl** (fxtr/impls/impl_remote.py, lines 77-202)
    - Consider whether to use same dynamic class mechanism or keep as-is
    - Update base class from `FxtrProtocolImpl` to `FxtrCallTarget`
    - Update `protocol_spec()` method to be property returning class variable

### Phase 6: Protocol Definitions (Throughout Codebase)

21. **Update built-in protocol definitions** (in core.py, around lines 3946, 3961, 4167, and in other files)
    - Convert `@define_protocol` decorated Protocol classes to FxtrCallTarget subclasses
    - For `TaskFunction` (line 3946): Change to `class TaskFunction(FxtrCallTarget, type_id="TaskFunction^1")` with proper namespace
    - For `StorageHandle` (line 3961): Same pattern
    - For `SecretsProtocol` (line 4167): Same pattern
    - For `TaskWaiter` (backend_runner.py:31): Same pattern
    - For `BackendRunnerInterface` (backend_runner.py:44): Same pattern
    - For `UVSpawnerProtocol` (uv_py_provider.py:112): Same pattern
    - For `UVRegisterProtocol` (uv_py_provider.py:130): Same pattern
    - For watchable protocols (builtin_util/watchable.py:12): Same pattern

22. **Update test protocol definitions** (fxtr_testdeps/impl_fixtures.py)
    - For `SequenceProcessor` (line 8): Convert to FxtrCallTarget subclass
    - For `Calculator` (line 92): Convert to FxtrCallTarget subclass
    - For `CalculatorFactory` (line 112): Convert to FxtrCallTarget subclass

23. **Update example protocol definitions** (in all example blueprint index.py files)
    - For `SimpleSingleTurnModel` in demo_09_15 (line 61): Convert to FxtrCallTarget subclass
    - For `SimpleWeirdBehaviorJudge` in demo_09_15 (line 90): Convert to FxtrCallTarget subclass
    - For `SimpleSingleTurnModel` in demo_09_15_fake (line 61): Convert to FxtrCallTarget subclass
    - For `SimpleWeirdBehaviorJudge` in demo_09_15_fake (line 90): Convert to FxtrCallTarget subclass
    - For `SimpleSingleTurnModel` in demo_agentic_misalignment (line 62): Convert to FxtrCallTarget subclass
    - For `SimpleTargetBehaviorJudge` in demo_agentic_misalignment (line 91): Convert to FxtrCallTarget subclass

24. **Remove define_protocol decorator and registry**
    - Remove `define_protocol` function from core.py
    - Remove `_PROTOCOL_REGISTRY` from core.py
    - Remove imports of `define_protocol` from all files

### Phase 7: Implementation Constructor Updates (Massive, Throughout Codebase)

25. **Update all implementation __init__ signatures** (across all implementation files)
    - Change from `deps: Mapping[core.AnyProtocolImplPlan, impl_base.FxtrProtocolImpl], /,` to `resolve: impl_base.Resolver, /,`
    - Change from `deps[plan]` to `resolve(plan)` in constructor bodies
    - Update all type assertions from `implements_protocol` to just use returned type
    - Files to update in fxtr/impls/: Any files with FxtrProtocolImpl implementations
    - Files to update in examples: All implementation files in lib/pyimpl directories (~28 files)
    - Files to update in tests: All implementation files in fxtr_testdeps

26. **Update all _ainit classmethod signatures** (for async initializers)
    - Change from `deps: Mapping[...]` to `resolve: impl_base.Resolver`
    - Update method bodies to use `resolve(plan)` instead of `deps[plan]`
    - Particularly important for OpenAI model implementations in examples

27. **Update entry-point builder functions** (in examples)
    - Change signature from `deps: dict[...]` to `resolve: Resolver`
    - Update bodies to use `resolve()` calls

### Phase 8: Type Annotation Updates (Throughout Codebase)

28. **Update plan field type annotations** (in data entities)
    - Change from `Annotated[Any, plan_for_protocol(Protocol)]` to `Plan[Protocol]`
    - Change from `core.AnyProtocolImplPlan` to `Plan[FxtrCallTarget]`
    - This affects kwargs in PyImplPlan instances and fields in config entities
    - Search for uses of `plan_for_protocol` to find all locations
    - Search for uses of `AnyProtocolImplPlan` to find all locations

29. **Update function return type annotations** (builder functions)
    - Change from `Annotated[PyImplPlan, plan_for_protocol(Protocol)]` to `PyImplPlan[Protocol]`
    - This affects all builder functions in blueprint index.py files

30. **Update dependency tracking** (in impl_plans/plan_dep_util.py)
    - Update type annotations to use `Plan[FxtrCallTarget]` instead of `AnyProtocolImplPlan`
    - Ensure dependency extraction still works correctly

### Phase 9: Backend/Orchestration Updates (fxtr/api/)

31. **Update backend_runner.py** (throughout file, especially around line 497-522)
    - Update type annotations for plan handling to use `Plan[FxtrCallTarget]`
    - Ensure provider calling logic works with new resolver pattern
    - Update `_plan_worker` to handle new types correctly

32. **Update any other API files** (fxtr/api/)
    - Search for uses of `AnyProtocolImplPlan` and `FxtrProtocolImpl`
    - Update to use new types

### Phase 10: Collection Tracking Updates (fxtr/object_model/)

33. **Update collection_tracking.py** (around line 366 and elsewhere)
    - Update error messages about protocol registration
    - Update any protocol spec handling logic
    - Ensure collection modules work with FxtrCallTarget subclasses

### Phase 11: Testing and Validation

34. **Update test utilities** (tests/)
    - Update any test helper functions using old types
    - Update test assertions about protocol specs

35. **Run all tests and fix issues**
    - Run pytest on entire test suite
    - Fix any type checking errors with pyright/mypy
    - Fix any runtime errors from migration

### Phase 12: Documentation and Cleanup

37. **Update docstrings and comments**
    - Update docstrings mentioning `FxtrProtocolImpl` to mention `FxtrCallTarget`
    - Update docstrings mentioning `define_protocol` to mention subclassing
    - Update comments about deps dict to mention resolver function

38. **Update error messages**
    - Search for error messages mentioning old system
    - Update to reference new system

39. **Final cleanup**
    - Remove any unused imports
    - Remove old code paths that are no longer used

## Implementation Strategy Notes

**Recommended order:**
1. Start with Phase 1-2 (core types and base classes) - this establishes the foundation
2. Do Phase 3 (plan types) to get the generic types in place
3. Do Phase 4 (providers) to establish resolver pattern
4. Do Phase 5 (RPC) which is complex but isolated
5. Do Phase 6 (protocol definitions) - convert all protocols at once to avoid confusion
6. Do Phase 7-8 together (implementation updates and type annotations) - this is the bulk of the work
7. Do Phase 9-10 (backend and tracking)
8. Do Phase 11-12 (testing and cleanup)

**Testing strategy:**
- Tests will break during the migration and that's expected
- Don't worry about tests until the end of phase 5. At that point some of the self-contained tests should start to work and other should begin to work as we get through the necessary. Focus on the test_in_process_py_provider, test_rpc, and test_uv_py_provider tests to start.
- All tests should pass by the end of Phase 11
