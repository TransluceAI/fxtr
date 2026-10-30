# Runtime Environment Providing Migration Plan

## Background

The codebase is being refactored from an old `SecretsProtocol` system to a new system based on:
- `SecretFromEnvVarDeclaration` / `SecretFromOptionalEnvVarDeclaration` for secrets
- `RuntimeEnvConfigAssignment` for runtime environment configuration

The `fxtr ls` command in [fxtr_cli.py](../fxtr/api/fxtr_cli.py) has already been updated to use the new system (lines 446-568), using:
- `api_lib.discover_secret_env_var_declarations()` - finds all secret declarations in a collection
- `api_lib.discover_runtime_env_config_assignments()` - finds all active runtime env config assignments in a collection

However, **task execution** still needs to be migrated. Currently when tasks are launched:
1. [backend_runner.py](../fxtr/api/backend_runner.py) manages task execution
2. [auto_provider.py](../fxtr/providers/auto_provider.py) provides implementations for plans

Both of these still reference the old `SecretsProtocol` system.

## Goal

Ensure that when tasks are launched or when the auto_provider is invoked, the **same version** of secrets/runtime env config that was identified by `fxtr ls` as active is used. This means:
- The non-overridden `RuntimeEnvConfigAssignment` entities discovered by `discover_runtime_env_config_assignments()` should be used
- The `SecretFromEnvVarDeclaration` / `SecretFromOptionalEnvVarDeclaration` entities discovered by `discover_secret_env_var_declarations()` should be used

## Current State Analysis

### backend_runner.py

**Lines 246-274**: `_setup_providers()` method sets up providers but only sets up UV provider and creates a `SecretsProvider`:
```python
# Line 316-322 (commented out old system):
self._secrets_provider = externals_provider.SecretsProvider(
    config=self._config,
    interactive=True,
    all_plans=set(),  # Will be updated dynamically
)
```

**Lines 493-496**: In `_setup_plan()`, there's commented-out code for handling `ExternalServiceIdentifier` with `SecretsRetriever`:
```python
# elif isinstance(plan, core.ExternalServiceIdentifier):
#     if plan.interface_spec == core.spec_of(core.SecretsRetriever[]):
#         provider = self._secrets_provider
#     else:
raise RuntimeError(f"Unsupported ExternalServiceIdentifier: {_summarize(plan)}")
```

**Problem**: The backend runner doesn't know about or use the new secret/runtime env systems. It needs to:
1. Discover the collection being used for the tasks
2. Call `discover_secret_env_var_declarations()` and `discover_runtime_env_config_assignments()`
3. Create providers that respect these discovered configurations
4. Route `ExternalServiceIdentifier[RuntimeEnvRetriever[T]]` plans to runtime env providers
5. Handle secret declarations properly

### auto_provider.py

**Lines 38-40**: Creates a `SecretsProvider` but this is the old system:
```python
self._secrets_provider = externals_provider.SecretsProvider(config, interactive=interactive)
```

**Lines 89-96**: In `provide()`, there's commented-out code for handling `SecretsProtocol`:
```python
# elif isinstance(plan, core.ExternalServiceIdentifier):
#     if plan.interface_spec == core.spec_of(core.SecretsProtocol):
#         return await self._secrets_provider.provide(plan, dependencies, provenance_lock)
#     else:
raise provider_base.ProviderUnsupportedPlanError(...)
```

**Problem**: The auto_provider doesn't know about the new runtime env system and needs similar changes to backend_runner.

## Key Questions & Design Decisions

### Q1: How does backend_runner know which collection is being used?

**Current situation**: In `materialize_impl()` (fxtr_cli.py:762-959), the collection is loaded and tasks are extracted, then `BackendRunner.launch(tasks)` is called. The BackendRunner receives only the `Task` entities, not the collection.

**Options**:
1. Pass the collection to `BackendRunner.create()` or `BackendRunner.launch()`
2. Have BackendRunner discover the collection from the tasks by loading them and finding their defining collection
3. Store collection reference somewhere global/in config that BackendRunner can access

**Recommendation**: Pass the collection to `BackendRunner.launch()` since that's when we know which tasks we're running and which collection they came from. This is clearest and most explicit.

**Decision:**: Lets actually send only the assignments for the externals into the backend runner on launch, without passing the full collection. And assume there's no conflicts; conflicts can be caught by the caller.

However, we also need to make sure old plans for secrets and runtime env haven't been invalidated. So maybe we cache maps of the active secret and runtime env declarations. Then anytime we call launch with new assignments, we check if any of those plans are out of date; if so we mark them and all dependent plans stale so that we start a new plan worker next time.

### Q2: Should we create new provider types for RuntimeEnvRetriever?

**Current situation**: We have `SecretsProvider` which was for the old `SecretsProtocol` system.

**Options**:
1. Create a new `RuntimeEnvConfigProvider` that handles `ExternalServiceIdentifier[RuntimeEnvRetriever[T]]`
2. Reuse/repurpose existing `SecretsProvider` (but this seems wrong since it's for a different thing)
3. Create multiple provider types for different kinds of external services

**Recommendation**: Create a new `RuntimeEnvConfigProvider` in [externals_provider.py](../fxtr/providers/externals_provider.py). This provider would:
- Take a dict mapping `ExternalServiceIdentifier[RuntimeEnvRetriever[T]]` to active `RuntimeEnvConfigAssignment[T]`
- Implement an impl that returns the configured value when `get_runtime_config()` is called

**Decision:** Yes lets create a new RuntimeEnvConfigProvider.

### Q3: How should secrets be handled in the new system?

**Current situation**: The old `SecretsProvider` reads from environment variables and .env files. The new system uses `SecretFromEnvVarDeclaration` entities that specify which env var to read.

**Options**:
1. Keep using `SecretsProvider` but update it to work with the new declaration system
2. Create a new provider type for the new secret system
3. Secrets are actually just runtime env configs, so use `RuntimeEnvConfigProvider`

**Analysis**: Looking at the code, `SecretFromEnvVarDeclaration` declares a secret that comes from an env var, and it has a `handle: ExternalServiceIdentifier[SecretsRetriever[T]]`. So secrets are handled via `SecretsRetriever` protocol, which is different from `RuntimeEnvRetriever`.

**Recommendation**: Update the existing `SecretsProvider` to:
- Accept the discovered `SecretFromEnvVarDeclaration` / `SecretFromOptionalEnvVarDeclaration` entities
- Use these declarations to determine which env var to read for each handle
- Handle optional vs required appropriately

**Decision:** Yes lets update SecretsProvider to handle the new SecretsRetriever.

### Q4: What about conflicts?

**Current situation**: `discover_runtime_env_config_assignments()` returns a dict where each plan maps to a **list** of assignments (could be multiple if there are conflicts). Same for `discover_secret_env_var_declarations()`.

**Recommendation**:
- For runtime env configs: If there are multiple active (non-overridden) assignments, that's an error - fail with a clear message
- For secrets: If there are multiple declarations for the same handle, that's an error - fail with a clear message
- These checks should happen at provider setup time, not during task execution

**Decision:** Yes, lets fail during launch. There is a method _check_supports_plan, let's use this to ensure there are no conflicts; it may need a new arg for the collection state.


## Revised Implementation Plan

Based on decisions above, here's the revised plan:

### Step 1: Update BackendRunner to accept external service assignments

**File**: [backend_runner.py](../fxtr/api/backend_runner.py)

1. Update `BackendRunner.launch()` signature to accept external service assignments:
   ```python
   async def launch(
       self,
       tasks: list[core.Task],
       secret_declarations: dict[
           core.ExternalServiceIdentifier[core.SecretsRetriever[Any]],
           core.SecretFromEnvVarDeclaration | core.SecretFromOptionalEnvVarDeclaration
       ],
       runtime_env_assignments: dict[
           core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
           core.RuntimeEnvConfigAssignment
       ],
       message: str | None = None
   ) -> dict[str, TaskWaiter]:
   ```

2. Cache these assignments in BackendRunner state (e.g., `_active_secret_declarations` and `_active_runtime_env_assignments`)

3. Before processing new assignments in launch():
   - Compare with cached assignments to detect changes
   - If any ExternalServiceIdentifier plans have changed assignments, mark those plan workers and all dependent plans as stale
   - If any ExternalServiceIdentifier plans were in the cache but NOT in the new assignments, also mark those plan workers and dependents as stale
   - After invalidation, update cache to exactly match the new assignments (replacing old cache completely)
   - This ensures we start fresh plan workers for invalidated external services and don't leak old plans

4. Update `_check_supports_plan()` to validate:
   - If plan is `ExternalServiceIdentifier[SecretsRetriever[T]]`, verify it's in `secret_declarations`
   - If plan is `ExternalServiceIdentifier[RuntimeEnvRetriever[T]]`, verify it's in `runtime_env_assignments`
   - Error immediately if required external service has no declaration/assignment

5. Update callers in [fxtr_cli.py](../fxtr/api/fxtr_cli.py):
   - In `materialize_impl()`, discover declarations/assignments from collection
   - Validate no conflicts (each handle/plan has exactly one declaration/assignment)
   - Pass to `runner_interface.launch()`

6. Update `BackendRunnerInterface` protocol to include new parameters in launch()

### Step 2: Create RuntimeEnvConfigProvider

**File**: [externals_provider.py](../fxtr/providers/externals_provider.py)

Create a new provider class:
```python
class RuntimeEnvConfigProvider(provider_base.ImplProvider):
    """Provider for runtime environment configuration."""

    def __init__(
        self,
        assignments: dict[
            core.ExternalServiceIdentifier[core.RuntimeEnvRetriever[Any]],
            core.RuntimeEnvConfigAssignment
        ]
    ):
        # Validate no conflicts (each plan should have exactly one assignment)
        for plan, assignment in assignments.items():
            # Store assignment value

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        # Return an impl that provides the configured value
        # Return appropriate provenance
```

Also create an implementation class for `RuntimeEnvRetriever[T]` that returns the configured value.

### Step 3: Update SecretsProvider for new declaration system

**File**: [externals_provider.py](../fxtr/providers/externals_provider.py)

Update the `SecretsProvider` class to:
1. Accept a dict of `ExternalServiceIdentifier[SecretsRetriever[T]]` → `SecretFromEnvVarDeclaration | SecretFromOptionalEnvVarDeclaration`
2. Use the declaration's `env_var` field to determine which environment variable to read
3. Handle `SecretFromOptionalEnvVarDeclaration` appropriately (allow missing env vars)
4. Keep .env file loading support (answer to Q1)

### Step 4: Create providers and wire up in BackendRunner

**File**: [backend_runner.py](../fxtr/api/backend_runner.py)

**Note**: Step 1 updates the signature and cache management in `launch()`. This step focuses on creating the actual provider instances and wiring them up in `_setup_plan()`.

In `_setup_providers()` or `launch()`:
1. Create provider instances:
   - Create `SecretsProvider` instance (initially with empty declarations)
   - Create `RuntimeEnvConfigProvider` instance (initially with empty assignments)
2. Store as instance variables (e.g., `self._secrets_provider`, `self._runtime_env_provider`)
3. When `launch()` is called with new assignments (Step 1), update these provider instances with the new data

In `_setup_plan()` (lines 484-530):
1. Uncomment and update the `ExternalServiceIdentifier` handling
2. Route based on interface spec:
   - `SecretsRetriever[T]` → use `self._secrets_provider`
   - `RuntimeEnvRetriever[T]` → use `self._runtime_env_provider`
3. This routing happens when setting up individual plans (uses the providers initialized above)

### Step 5: Update AutoProvider similarly

**File**: [auto_provider.py](../fxtr/providers/auto_provider.py)

Update AutoProvider to support external services:

1. Update `__init__()` to accept:
   - Optional pre-computed secret declarations and runtime env assignments
   - Create SecretsProvider and RuntimeEnvConfigProvider with these

2. Update `auto_provider()` context manager function to:
   - Accept optional `collection` parameter (per answer to Q3)
   - If not provided, load default collection from config
   - Discover declarations/assignments from collection
   - Validate no conflicts (error immediately per answer to Q4)
   - Pass to AutoProvider

3. Update `provide()` method to handle `ExternalServiceIdentifier` properly:
   - Route `SecretsRetriever[T]` to SecretsProvider
   - Route `RuntimeEnvRetriever[T]` to RuntimeEnvConfigProvider
   - Error if external service has no declaration/assignment (per answer to Q2)

### Step 6: Update fxtr_cli.py materialize command

**File**: [fxtr_cli.py](../fxtr/api/fxtr_cli.py)

In `materialize_impl()` and `_materialize_with_runner()`:
1. After loading collection, call `discover_secret_env_var_declarations(coll)` and `discover_runtime_env_config_assignments(coll)`
2. Validate no conflicts - if any handle/plan has multiple declarations/assignments, error with clear message
3. Convert lists to single-item dicts (since we validated no conflicts)
4. Pass these dicts to `runner_interface.launch()`

### Step 7: Update BackendRunnerInterfaceImpl

**File**: [backend_runner.py](../fxtr/api/backend_runner.py)

Update the wrapper implementation (lines 788-804) to pass through the new parameters in launch().

### Step 8: Testing & Validation

1. Test that `fxtr ls` shows the same secret declarations and runtime env configs that get used during task execution
2. Test conflict detection (multiple declarations/assignments for same handle/plan)
3. Test that optional secrets work correctly
4. Test that runtime env config values are correctly passed to task implementations
5. Test both local backend runner and remote runner scenarios
6. Test that plan invalidation works when assignments change between launches

## Summary of Design Decisions

All questions answered by user:

1. **Collection passing**: Pass only the active assignments (not full collection) to BackendRunner.launch(). This works for remote runserver too.

2. **New provider type**: Create RuntimeEnvConfigProvider for RuntimeEnvRetriever plans.

3. **SecretsProvider update**: Update to work with new SecretFromEnvVarDeclaration system, keep .env file loading.

4. **Conflict handling**: Validate no conflicts in fxtr_cli before calling launch(). Error immediately.

5. **Missing declarations**: Error immediately if a plan needs external service but no declaration/assignment found (validated in _check_supports_plan).

6. **AutoProvider API**: Accept optional collection parameter, otherwise load default from config.

7. **Validation timing**: Validate all external service dependencies at task launch time.

8. **Plan invalidation**: Cache assignments in BackendRunner, invalidate affected plan workers if assignments change between launches.
