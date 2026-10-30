# Implementation Plan: RuntimeCoordinatorBinding Support in Backend Runner

## Overview
Add support for `RuntimeCoordinatorBinding` objects in the backend runner and CLI, similar to how `RuntimeEnvConfigAssignment` objects are currently handled. The key difference is that coordinator bindings bind to a plan instead of a constant value, and they need to grab the implementation from the corresponding worker dynamically.

## Current Understanding

### RuntimeEnvConfigAssignment (existing)
- Binds an `ExternalServiceIdentifier[RuntimeEnvRetriever[T]]` to a constant value `T`
- Discovered via `api_lib.discover_runtime_env_config_assignments(coll)`
- Handled in CLI by passing to `BackendRunnerInterface.launch()`
- In backend runner:
  - Stored in `_active_runtime_env_assignments` dict
  - Used to create `RuntimeEnvConfigProvider`
  - Provider returns `RuntimeEnvRetrieverImpl` with the constant value
  - Provenance is `RuntimeEnvProvenance(assignment_cid)`
  - Plans using these are tracked with `check_usable` checking if assignment still matches

### RuntimeCoordinatorBinding (new)
- Similar structure to `RuntimeEnvConfigAssignment` but binds to a `plan: Plan[I]` instead of a constant value
- Discovered via `api_lib.discover_runtime_coordinator_bindings(coll)` (already exists)
- Needs to return the implementation from the plan worker for `binding.plan`
- Provenance should be `RuntimeCoordinatorProvenance` which includes:
  - `bound_plan: Plan[Any]` - the plan that was bound
  - `bound_provenances: ImplProvenanceSet` - provenances for all deps of the bound plan

## Implementation Steps

### 1. Update CLI (`fxtr/api/fxtr_cli.py`)

**In `materialize_impl()`:**
- After line 902 (where runtime env assignments are discovered), add discovery of coordinator bindings
- Validate no conflicts (same as with runtime env assignments)
- Convert to single-item dict after validation
- Pass coordinator bindings dict to `_materialize_with_runner()` as a new parameter

**In `_materialize_with_runner()`:**
- Accept new `runtime_coordinator_bindings` parameter
- Convert bindings dict to list for protocol call
- Pass to `runner_interface.launch()`

### 2. Update BackendRunnerInterface Protocol (`fxtr/api/backend_runner.py`)

**In `BackendRunnerInterface.launch()` method signature (line 61-89):**
- Add new parameter after `runtime_env_assignments`:
```python
runtime_coordinator_bindings: list[
    tuple[
        core.ExternalServiceIdentifier[Any],
        core.RuntimeCoordinatorBinding,
    ]
],
```

### 3. Update BackendRunner Implementation

**In `BackendRunner.__init__()` (line 258-293):**
- Add cache field similar to `_active_runtime_env_assignments`:
```python
self._active_runtime_coordinator_bindings: dict[
    core.ExternalServiceIdentifier[Any],
    core.RuntimeCoordinatorBinding,
] = {}
```

**In `BackendRunner.launch()` (line 440-542):**
- Update signature to accept `runtime_coordinator_bindings` parameter (as dict, not list):
```python
async def launch(
    self,
    tasks: list[core.Task],
    secret_declarations: dict[...],
    runtime_env_assignments: dict[...],
    runtime_coordinator_bindings: dict[
        core.ExternalServiceIdentifier[Any],
        core.RuntimeCoordinatorBinding,
    ],
    message: str | None = None,
) -> dict[str, TaskWaiter]:
```
- Around line 484, store in cache (similar to runtime_env_assignments):
```python
self._active_runtime_coordinator_bindings = dict(runtime_coordinator_bindings)
```

**In `BackendRunner._check_supports_plan()` (line 680-714):**
- Add check for coordinator bindings (after line 698) with recursive validation:
```python
elif plan in self._active_runtime_coordinator_bindings:
    # RuntimeCoordinator - supported and has binding
    binding = self._active_runtime_coordinator_bindings[plan]

    # Recursively check that the bound plan and all its deps are supported
    self._check_supports_plan(binding.plan)
    for dep in plan_dep_util.plan_deps(binding.plan, recursive=True):
        self._check_supports_plan(dep)
```

**In `BackendRunner._setup_plan()` (line 716-781):**
- Add branch for handling coordinator bindings (after line 741):
```python
elif plan in self._active_runtime_coordinator_bindings:
    # Get the binding
    binding = self._active_runtime_coordinator_bindings[plan]

    # Request the bound plan (this adds it to dep_states below)
    bound_plan_state = await self._request_plan(binding.plan)

    # Create provider using SimpleCallableProvider
    provider = provider_base.SimpleCallableProvider(self._provide_coordinator_binding)

    # Capture the current binding for check_usable
    expected_binding = binding
    check_usable = lambda: self._active_runtime_coordinator_bindings.get(plan) == expected_binding
```

- Update the dep collection to include bound plan:
```python
# Recursively request all direct dependencies
dep_states = {}
for dep in plan_dep_util.plan_deps(plan, recursive=False):
    dep_states[dep] = await self._request_plan(dep)

# For coordinator bindings, also add the bound plan as a dependency
if isinstance(plan, core.ExternalServiceIdentifier) and plan in self._active_runtime_coordinator_bindings:
    binding = self._active_runtime_coordinator_bindings[plan]
    dep_states[binding.plan] = await self._request_plan(binding.plan)
```

**Add new private method `_provide_coordinator_binding()`:**
```python
async def _provide_coordinator_binding(
    self,
    plan: core.Plan[Any],
    dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
    provenance_lock: core.AnyEntity | None = None,
) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
    """Provide implementation for a coordinator binding plan."""
    assert isinstance(plan, core.ExternalServiceIdentifier)

    # Get the binding
    binding = self._active_runtime_coordinator_bindings[plan]

    # Get the bound plan's implementation from dependencies
    bound_impl = dependencies[binding.plan]

    # Get all transitive deps of the bound plan
    all_bound_deps = plan_dep_util.plan_deps(binding.plan, recursive=True)

    # Build provenance set from all dep workers
    bound_provenances = {}
    for dep_plan in all_bound_deps:
        dep_state = self._active_plans[dep_plan]
        assert dep_state.plan_provenance is not None
        bound_provenances[core.entity_cid(dep_plan)] = dep_state.plan_provenance

    provenance = externals_provider.RuntimeCoordinatorProvenance(
        bound_plan=binding.plan,
        bound_provenances=core.ImplProvenanceSet(provenances=frozendict(bound_provenances)),
    )

    # This provider is only called by the backend runner itself, so provenance_lock
    # should never be provided
    if provenance_lock is not None:
        raise RuntimeError(
            "RuntimeCoordinator provider should not receive provenance_lock "
            "(only used internally by backend runner)"
        )

    return bound_impl, provenance
```

**In `BackendRunnerInterfaceImpl.launch()` (line 1067-1090):**
- Accept new `runtime_coordinator_bindings` list parameter
- Convert to dict before passing to `self._runner.launch()`:
```python
runtime_coordinator_bindings_dict = dict(runtime_coordinator_bindings)
```

### 4. No new provider class needed

The `RuntimeCoordinatorProvenance` type already exists in `externals_provider.py` and we'll use `SimpleCallableProvider` with the backend runner's `_provide_coordinator_binding` method.

### 5. Testing Strategy

After implementation:
- Test that coordinator bindings are discovered correctly
- Test that bound plans are executed and their implementations are provided
- Test that provenance includes all dependencies of bound plan
- Test that usability checking works (binding changes trigger re-setup)
- Test that plan worker dependencies are tracked correctly

## Design Decisions (RESOLVED)

1. **Provider architecture:** ✅ Use `provider_base.SimpleCallableProvider` with a private method on BackendRunner as the callable
   - This gives us access to runner state without creating a new provider class
   - The callable will be `self._provide_coordinator_binding` or similar

2. **Dependency tracking:** ✅ Include `binding.plan` in `dep_states` as if it was an actual dependency
   - Request the bound plan during `_setup_plan()` using `await self._request_plan(binding.plan)`
   - Add its worker state to `dep_states` dict

3. **Implementation wrapping:** ✅ Return the bound plan's implementation directly
   - No wrapping needed, just return `bound_plan_worker.impl`

4. **Provenance collection:** ✅ Include all transitive dependencies (same as `_task_worker`)
   - Use `plan_dep_util.plan_deps(binding.plan, recursive=True)` to get all deps
   - Build `ImplProvenanceSet` with provenances from all dep workers
   - Note: We don't need to walk through runtime bindings specially - they have no official deps; their deps are tracked in their own provenance

5. **Timing:** ✅ Request/setup the bound plan during `_setup_plan()`
   - This ensures proper dependency tracking in `dep_states`

6. **Type signature:** ✅ Use `ExternalServiceIdentifier[Any]` for the binding handle type

7. **Provider updates:** ✅ Update coordinator bindings similar to secrets/runtime_env in `launch()`
   - Store new bindings in `self._active_runtime_coordinator_bindings`
   - Plans with bindings no longer in the updated set should be marked stale (same as runtime env configs)

## Final Implementation Details

### Staleness Checking
✅ The `check_usable` lambda automatically handles staleness by capturing `expected_binding` and checking if `self._active_runtime_coordinator_bindings.get(plan) == expected_binding`. When bindings change in `launch()`, `_verify_worker_is_usable` will return False for workers with outdated bindings and move them to stale. Follow the existing pattern used for runtime_env_assignments.

### Error Handling for Unsupported Bound Plans
✅ In `_check_supports_plan()`, recursively check all deps of the bound plan to fail fast before starting to prepare plans:

```python
elif plan in self._active_runtime_coordinator_bindings:
    # RuntimeCoordinator - supported and has binding
    binding = self._active_runtime_coordinator_bindings[plan]

    # Recursively check that the bound plan and all its deps are supported
    self._check_supports_plan(binding.plan)
    for dep in plan_dep_util.plan_deps(binding.plan, recursive=True):
        self._check_supports_plan(dep)
```

This ensures we fail early with clear error messages rather than failing partway through plan preparation.

---

## Extension: AutoProvider Support

### Overview
The `AutoProvider` class in `fxtr/providers/auto_provider.py` is a router that dispatches different plan types to specialized providers. It currently handles:
- `PyImplPlan` → python_provider
- `ExternalServiceIdentifier` (SecretsProtocol) → SecretsProvider
- `ExternalServiceIdentifier` (RuntimeEnvRetriever) → RuntimeEnvConfigProvider
- `TaskResultHandle` → TaskResultHandleProvider
- `ConstantHandle` → ConstantProvider

We need to add support for `RuntimeCoordinatorBinding` so that AutoProvider can handle coordinator bindings the same way it handles runtime env assignments.

### High-Level Plan

**Approach:** AutoProvider will handle coordinator bindings by maintaining its own cache of bound plan implementations. When providing a coordinator handle:
1. Check if we have a cached implementation for this binding
2. If not cached, recursively use AutoProvider itself to provide the bound plan
3. Cache the result (impl + provenance)
4. Return the cached implementation with RuntimeCoordinatorProvenance

**Key differences from BackendRunner:**
- AutoProvider assumes bindings never change (no staleness checking)
- Uses recursive self-provision rather than worker state lookup
- Simpler caching (just dict of handle → cached result)

### Implementation Steps

**1. Update `AutoProvider.__init__()` (line 28-59):**
- Add new parameter `runtime_coordinator_bindings` similar to runtime_env_assignments
- Store bindings dict in `self._runtime_coordinator_bindings`
- Create cache dict: `self._coordinator_impl_cache: dict[ExternalServiceIdentifier, tuple[FxtrCallTarget, RuntimeCoordinatorProvenance]] = {}`

**2. Update `AutoProvider.provide()` (line 84-139):**
- In the `ExternalServiceIdentifier` branch (line 108-126), add coordinator binding handling
- Try coordinator bindings after runtime env provider, before raising error
- Logic:
  ```python
  # Try coordinator bindings
  if plan in self._runtime_coordinator_bindings:
      return await self._provide_coordinator_binding(plan, dependencies, provenance_lock)
  ```

**3. Add new method `_provide_coordinator_binding()`:**
- Check cache first: `if plan in self._coordinator_impl_cache: return self._coordinator_impl_cache[plan]`
- Get binding from `self._runtime_coordinator_bindings[plan]`
- Recursively provide bound plan: `bound_impl, bound_prov = await self.provide(binding.plan, dependencies, provenance_lock)`
- Get all transitive deps using `plan_dep_util.plan_deps(binding.plan, recursive=True)`
- For each dep, recursively provide to get provenance: `await self.provide(dep, dependencies, provenance_lock)`
- Build `RuntimeCoordinatorProvenance` with bound_plan and bound_provenances
- Cache result in `self._coordinator_impl_cache[plan]`
- Return cached result

**4. Update `auto_provider()` context manager (line 142-273):**
- After line 187, add: `runtime_coordinator_bindings_by_handle = api_lib.discover_runtime_coordinator_bindings(collection)`
- Add validation for conflicts (similar to secrets/runtime env, lines 190-202)
- Convert to single-item dict (similar to lines 204-214)
- Pass to AutoProvider constructor (line 265-271)

### Questions
- For the recursive provision of deps when building provenance: should I pass the same `dependencies` dict, or an updated one? I think same dict since we're just collecting provenances?
- Should the cache be keyed by just the handle, or by `(handle, binding)` to handle binding changes? (Probably just handle since we assume no changes)
