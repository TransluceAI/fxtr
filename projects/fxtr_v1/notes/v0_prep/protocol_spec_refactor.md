# Protocol spec refactor

## Background:
Initial context in /Users/daniel/clarity/lib/behaviors/behaviors/staging/README.md, read that first.

There's a kind of object called a "ProtocolSpec" in /Users/daniel/clarity/lib/behaviors/behaviors/staging/object_model/core.py. It defines essentially a Python protocol by documentation. It is also an "entity" in the sense of core.py.

Protocol spec entities are referenced by impl plans like ExternalServiceIdentifier and /Users/daniel/clarity/lib/behaviors/behaviors/staging/impl_plans/py_impl_plan.py which are in turn used in various places.

They are also referenced by "impls" which actually implement the protocols: mostly in /Users/daniel/clarity/lib/behaviors/behaviors/staging/impls but see in particular /Users/daniel/clarity/lib/behaviors/behaviors/staging/impls/impl_py_local.py.

An example of protocols is in /Users/daniel/clarity/lib/behaviors/behaviors/staging/examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py and some impls are in /Users/daniel/clarity/lib/behaviors/behaviors/staging/examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/openai_judge.py and similar files.

## Old state:
ProtocolSpecs are defined by creating a ProtocolSpec object. There is no relationship between a protocol spec object and the implicit API contract it has, other than the documentation. Protocol specs don't even hold types in them.

Protocol implementations reference their specs, but in a way that is opaque to the Python type system.

Unlike ProtocolSpec, data entities (DataEntityBase in core) are actual python classes. They internally contain an entity which is a DataEntitySchema.

## Desired new state:

ProtocolSpecs are defined by creating a normal Python protocol like
```python
@core.define_protocol("SimpleSingleTurnModel^1", namespace_id=NAMESPACE_ID)
class SimpleSingleTurnModel(Protocol):
    """A single turn model..."""

    async def generate(self, system_prompt: SystemPrompt, user_message: SimpleUserMessage) -> SimpleResponse:
        ...
```

The define_protocol decorator should:
- Inspect the protocol.
    - Make sure that it is just a subclass of Protocol.
    - Make sure that every method it defines meets the requirements of PyImplTarget from /Users/daniel/clarity/lib/behaviors/behaviors/staging/impls/impl_py_local.py. It must NOT define any methods that would not be exposed over RPC since they aren't part of the protocol. Each method has to have acceptable annotations.
- Store the relevant information into a new ProtocolSpec entity.
- Store the protocol spec entity in a global weakref-key dict in core.py

Core should
- Expose the protocol spec via `core.protocol_spec(proto: Protocol)`

Protocol specs should additionally store the method names they support, as another attribute on the entity. Furthermore, signatures and documentation of all methods should be appended to the "_doc" of the ProtocolSpec. (formatted kind of like how help() formats things)

Protocol implementations can simply subclass the Protocol that they are supposed to implement, instead of having to provide it as a metaclass kwarg. The PyImplTarget `__init_subclass__` should inspect the other base classes to see if exactly one is a protocol with a `core.protocol_spec` entry and use that if so.

PyImplPlan and ExternalServiceIdentifier are still based on the ProtocolSpec entity. However, all users of these now use `core.protocol_spec(SomeProtoClass)` instead of directly referencing the protocol spec entity at the top level.

## Current state and todos:

✅ Step 1 complete: Updated ProtocolSpec entity with `methods: tuple[str, ...]` field
✅ Step 2 complete: Implemented `@core.define_protocol` decorator with full validation
✅ Step 3 complete: Implemented `core.protocol_spec()` accessor function
✅ Step 4 complete: Updated PyImplTarget.__init_subclass__ to:
  - Auto-detect protocol from base classes (if exactly one)
  - Explicit protocol_spec kwarg overrides inference
  - Verify required methods exist when protocol specified
  - Only expose protocol methods in _fxtr_rpc_methods
✅ Step 5 complete: All protocol definitions converted to @define_protocol decorated Protocol classes
✅ Step 6 complete: All protocol implementations updated to inherit from Protocol classes
✅ Step 7 complete: All protocol references updated
  - Updated `plan_for_protocol()` to accept Protocol classes and auto-convert
  - Updated PyImplPlan constructor calls to use `core.protocol_spec()` wrapper
  - Updated LocalImpl constructor calls (already had wrappers)

✅ Step 8 complete: Fixed circular import
  - Moved `allowed_method_filter` to core.py and renamed to `protocol_allowed_method_filter`
  - Added backward-compatible alias in impl_base.py

All steps complete! Ready to run tests.

## Refactoring Checklist:

### Protocol Definitions (Step 5):
- [x] core.py:3652 - TaskFunction
- [x] core.py:3669 - StorageHandle
- [x] core.py:3878 - SecretsProtocol
- [x] uv_py_provider.py:103 - _UV_SPAWNER_PROTOCOL
- [x] uv_py_provider.py:108 - _UV_REGISTER_PROTOCOL
- [x] impl_fixtures.py:14 - SequenceProcessor_spec
- [x] playground/nb_09_10.py:78 - my_protocol
- [x] examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py:61 - SimpleSingleTurnModel
- [x] examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py:91 - SimpleWeirdBehaviorJudge
- [x] examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py:62 - SimpleSingleTurnModel
- [x] examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py:92 - SimpleTargetBehaviorJudge

✅ All protocol definitions converted!

### Protocol Implementations (Step 6):
- [x] api/task_launcher.py - RunDataRetriever, SecretsRetriever
- [x] examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_sample_from_model.py
- [x] examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/task_judge_behavior.py
- [x] examples/demo_agentic_misalignment/lib/pyimpl/demo_agentic_misalignment/storage_projector.py
- [x] providers/uv_py_provider_entrypoint.py - _SpawnerImpl
- [x] providers/uv_py_provider.py - _UVRegisterServerImpl
- [x] examples/test_fixtures/impl_fixtures.py - SimpleWrapperImpl, RepeaterImpl
- [x] examples/demo_09_15/lib/pyimpl/demo_09_15/task_sample_from_model.py
- [x] examples/demo_09_15/lib/pyimpl/demo_09_15/task_judge_behavior.py
- [x] examples/demo_09_15/lib/pyimpl/demo_09_15/task_generate_ideas.py
- [x] examples/demo_09_15/lib/pyimpl/demo_09_15/task_collect_results.py
- [x] examples/demo_09_15/lib/pyimpl/demo_09_15/storage_projector.py

✅ All protocol implementations updated!

### Protocol References in Impl Plans (Step 7):
- [x] Updated `plan_for_protocol()` in core.py to auto-convert Protocol classes
- [x] examples/demo_09_15/lib/blueprint/demo_09_15_defns/index.py - PyImplPlan calls
- [x] examples/demo_agentic_misalignment/lib/blueprint/demo_agentic_misalignment_defns/index.py - PyImplPlan calls
- [x] providers/uv_py_provider.py - LocalImpl calls (already wrapped)
- [x] providers/uv_py_provider_entrypoint.py - LocalImpl calls (already wrapped)

✅ All protocol references updated!

## Implementation Plan:

### 1. Update ProtocolSpec entity in core.py
- Add a `methods` field to store method names (e.g., `methods: list[str]`)
- Modify `_doc` generation to include formatted method signatures and docstrings (help()-style format)

### 2. Implement `@core.define_protocol` decorator in core.py
- Create a WeakKeyDictionary to map Protocol classes to their ProtocolSpec entities
- Implement decorator that:
  - Validates that the decorated class is a `Protocol` subclass (and only Protocol, no other bases)
  - Validates each method in the protocol:
    - Check that method name passes `allowed_method_filter` (public or `__call__`)
    - Check that method is async (must be `async def` in the Protocol)
    - Check that all parameters have type annotations
    - Check that return type is annotated
    - Check that all types can be converted via `core.python_type_to_hub_type_spec`
    - Check that all types are `AnyFrozenIPLDable`-compatible
  - Extract method names, signatures, and docstrings
  - Create a ProtocolSpec entity with the provided type_id and namespace_id
  - Format method documentation and append to `_doc`
  - Store the Protocol class → ProtocolSpec mapping in the WeakKeyDictionary
  - Return the Protocol class unmodified

### 3. Implement `core.protocol_spec()` accessor function
- Create function `protocol_spec(proto: type[Protocol]) -> ProtocolSpec`
- Lookup the protocol in the WeakKeyDictionary
- Raise error if not found (protocol not registered with `@define_protocol`)

### 4. Update `PyImplTarget.__init_subclass__` in impl_py_local.py
- Check if `protocol_spec` kwarg was explicitly provided - if so, use it (overrides implicit detection)
- Otherwise, inspect base classes to find protocols with `core.protocol_spec()` entries (use try/except)
- If exactly one protocol found in bases, use it automatically
- If zero or multiple protocols found, set protocol_spec to None (must be provided later)
- Keep existing validation logic for methods

### 5. Update all protocol definitions throughout the codebase
- Convert old `ProtocolSpec(...)` objects to `@core.define_protocol` decorated Protocol classes
- Find all protocol definitions (search for `ProtocolSpec` instantiation)
- Replace with proper Python `Protocol` classes with the decorator
- Update type annotations in protocol methods

### 6. Update all protocol implementations
- Find all `PyImplTarget` subclasses
- Remove `protocol_spec=...` kwargs from class definitions where possible
- Add Protocol class as base class instead (e.g., `class MyImpl(SimpleSingleTurnModel, PyImplTarget)`)
- Verify implementations match protocol signatures

### 7. Update all protocol references in impl plans
- Find all uses of bare ProtocolSpec entities (e.g., in PyImplPlan, ExternalServiceIdentifier)
- Replace with `core.protocol_spec(ProtocolClass)` calls
- Update any code that constructs or references these plans
- Also update the `plan_for_protocol` function to convert protocol classes to protocol specs internally if needed

### 8. Fixes:
- get rid of circular imports by moving allowed_method_filter to core
- Run existing tests to verify nothing broke

### 9. Improvements to collection tracking
- add an AccessPathStep type for getting the protocol spec from a protocol
- walk through protocols using this in collection_tracking.py
