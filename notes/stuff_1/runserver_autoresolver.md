
# Current state
- Backend runner in fxtr/api/backend_runner.py
    - Accessed via fxtr/api/fxtr_cli.py / fxtr/api/api_lib.py
    - Runs tasks, manages plans
    - Receives various input about bindings during `launch`
- Backend runner always launched by fxtr_cli
- Separately, "auto provider" class in fxtr/providers/auto_provider.py
    - Some duplication of what this does
    - Sort of a low level router between providers that also implements the provider interface, which is itself a low level interface
- Some users of auto provider but mostly in tests

# Desired state
- backend runner gets a new method `activate_collection` which takes a Collection object, and extracts all the runtime/secret bindings from it that are currently passed to `launch`, and stores/caches them at that point
- `launch` no longer takes the runtime/secret bindings as input
- `fxtr_cli` uses the `activate_collection` method to pass these before launching tasks
- helper `attach_to_runserver` in api_lib.py which loads runserver state, then connects to the runserver, similar to how fxtr_cli currently does it, as a ctxmgr
- helper `create_or_attach_runner` there that is a similar ctxmgr but creates a runserver with the config if one isn't found
- refactor fxtr_cli to use that helper
- all existing users of auto provider should instead use `create_or_attach_runner` if outside of tests, or `backend_runner.BackendRunner.create(config)` if inside of tests. tests should be renamed to refer to the backend runner instead of auto_provider. note that backend runner's method is called `request`, not `provide`, and has a different signature
- auto provider removed
