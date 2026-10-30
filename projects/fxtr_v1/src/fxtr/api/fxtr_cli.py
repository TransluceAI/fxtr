"""Command-line interface for the staging fxtr API."""

import asyncio
import enum
import signal
import sys
import traceback
import typing
from pathlib import Path
from typing import Annotated, Any

import anyio
import cbrrr
import logfire
import typer
from rich import print as rprint
from rich.console import Console
from rich.markup import escape as resc

from fxtr.debug_visualizer.locator_server import FxtrLocatorDebugServer
from fxtr.logging_config import configure_logging
from fxtr.object_model import core

from ..impl_plans import plan_dep_util
from ..object_model import dag_util
from ..object_model import locator as locator_module
from . import api_lib, backend_runner, fxtrconfig

app = typer.Typer(
    help="Command-line interface for the staging fxtr API", pretty_exceptions_show_locals=False
)
console = Console()


@app.callback()
def main_callback(
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            help="Set log level (DEBUG, INFO, WARNING, ERROR, CRITICAL). Overrides FXTR_LOG_LEVEL env var.",
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Fxtr CLI - manage task collections and runs."""
    # Configure logging (reads from env var if no explicit level)
    configure_logging(log_level)


def find_config_file() -> Path:
    """Find the fxtr.toml config file in current directory or parent directories."""
    current_dir = Path.cwd()

    # Look for fxtr.toml in current directory and parent directories
    for path in [current_dir] + list(current_dir.parents):
        config_file = path / "fxtr.toml"
        if config_file.exists():
            return config_file

    rprint("[red]Error: Could not find fxtr.toml config file[/red]")
    rprint(
        "[yellow]Please ensure you are in a directory containing fxtr.toml or one of its"
        " subdirectories[/yellow]"
    )
    sys.exit(1)


def load_config() -> fxtrconfig.FxtrConfig:
    """Load the fxtr configuration."""
    config_file = find_config_file()
    try:
        return fxtrconfig.parse_fxtr_config(config_file)
    except Exception as e:
        rprint(f"[red]Error loading config from {config_file}: {e}[/red]")
        sys.exit(1)


def task_matches_namespec(task: api_lib.CollectionTask, namespec: str) -> bool:
    """Check if a task matches any of the patterns in the namespec."""
    # Tasks without canonical paths don't match any namespec
    if not task.canonical_path:
        return False

    patterns = [pattern.strip() for pattern in namespec.split(",")]
    canonical_path = task.canonical_path

    for pattern in patterns:
        if pattern.endswith("**"):
            # Prefix match - check if canonical path starts with the pattern (without **)
            prefix = pattern[:-2]
            if canonical_path.startswith(prefix):
                return True
        else:
            # Exact match
            if canonical_path == pattern:
                return True
    return False


def collect_task_dependencies(
    initial_tasks: list[core.Task], all_available_tasks: list[core.Task]
) -> list[core.Task]:
    """Collect all transitive dependencies of the given tasks.

    Args:
        initial_tasks: The tasks to find dependencies for
        all_available_tasks: All available Task objects to search through

    Returns:
        List of Task entities including the initial tasks and all their dependencies,
        ordered so dependencies come before dependents

    Raises:
        RuntimeError: If a required dependency task cannot be found
    """
    # Create a mapping from task CID to Task entity for faster lookups
    cid_to_task: dict[cbrrr.CID, core.Task] = {}
    for blueprint_task in all_available_tasks:
        task_cid = core.entity_cid(blueprint_task)
        cid_to_task[task_cid] = blueprint_task

    # Collect all task result handles that our tasks depend on, recursively
    processed_tasks: set[cbrrr.CID] = set()
    result: list[core.Task] = []

    def _process(task: core.Task) -> None:
        task_cid = core.entity_cid(task)
        if task_cid in processed_tasks:
            return
        processed_tasks.add(task_cid)

        # Get all dependencies of this task's function, recursively
        task_function_deps = plan_dep_util.plan_deps(task.function, recursive=True)

        # Filter for TaskResultHandle dependencies
        task_result_handles = {
            plan for plan in task_function_deps if isinstance(plan, core.TaskResultHandle)
        }

        # Add new dependencies and their corresponding tasks to process
        for handle in task_result_handles:
            handle_task_cid = handle.task_ref
            if handle_task_cid not in processed_tasks:
                # Find the corresponding task and add it to processing queue
                if handle_task_cid in cid_to_task:
                    dependency_task = cid_to_task[handle_task_cid]
                    _process(dependency_task)
                else:
                    raise RuntimeError(
                        "Cannot find required dependency task with CID"
                        f" {handle_task_cid.encode('base32')}. This task is required by task"
                        f" {task_cid.encode('base32')} but is not available in the current module."
                    )

        result.append(task)

    for task in initial_tasks:
        _process(task)

    return result


@app.command()
def execmodule(
    module_name: Annotated[str, typer.Argument(help="Name of the collection source to load")],
) -> None:
    """Load a collection and serialize the result to the active entity store.

    This is a utility command for debugging and ensuring collection data is available
    in the entity store. It does not interact with runsets.
    """
    config = load_config()

    # Check if the module name exists in the config
    if module_name not in config.blueprint.sources:
        rprint(f"[red]Error: Collection source '{module_name}' not found in config[/red]")
        rprint("[yellow]Available collection sources:[/yellow]")
        for source_name in config.blueprint.sources.keys():
            rprint(f"  - {source_name}")
        sys.exit(1)

    rprint(f"[blue]Loading collection source: {module_name}[/blue]")

    # Get the module entry-point string from config
    source_config = config.blueprint.sources[module_name]
    entrypoint = source_config.module

    # Load the collection
    collection = api_lib.load_collection_from_entrypoint(entrypoint)

    # Save the collection to the entity store
    data_locator = api_lib.get_active_data_locator(config)
    subgraph = dag_util.entity_subgraph_to_ipld_blocks(named_roots={}, sequence_roots=[collection])
    for cid, entity_data in subgraph.nodes.items():
        data_locator.write_entity_data(entity_data, cid)

    root_cid = core.entity_cid(collection)
    root_cid_str = root_cid.encode(base="base32")

    rprint(f"[green]Successfully loaded collection: {module_name}[/green]")
    rprint(f"[green]Successfully saved collection data with CID: {root_cid_str}[/green]")

    entity_store_path = fxtrconfig.get_active_entity_store(config)
    rprint(f"[cyan]Data saved to: {entity_store_path}[/cyan]")


@app.command()
def serve(
    collection: Annotated[
        str | None, typer.Option("-c", "--collection", help="Collection name from config")
    ] = None,
    module: Annotated[
        str | None, typer.Option("-m", "--module", help="Python module entry-point")
    ] = None,
    host: Annotated[str, typer.Option(help="Host to bind to")] = "localhost",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
) -> None:
    """Start the debug server serving runs from the active runset."""
    config = load_config()

    # Get the active entity store directory
    entity_store_path = fxtrconfig.get_active_entity_store(config)
    if not entity_store_path.exists():
        rprint(
            "[red]Error: Active entity store directory not found. Run 'execmodule' first to create"
            " data.[/red]"
        )
        rprint(f"[yellow]Expected at: {entity_store_path}[/yellow]")
        sys.exit(1)

    # Create locator for loading data
    data_locator = api_lib.get_active_data_locator(config)

    # Load active runset
    active_runset = api_lib.load_active_runset(config)

    named_urls = {}

    # Determine which collection to use (if any)
    collection_to_serve = None
    display_name = None

    if module:
        # Load module directly
        entrypoint = module
        display_name = module
        collection_to_serve = entrypoint
    elif collection:
        # Load named collection from config
        if collection not in config.blueprint.sources:
            rprint(f"[red]Error: Collection '{collection}' not found in config[/red]")
            rprint("[yellow]Available collection sources:[/yellow]")
            for source_name in config.blueprint.sources.keys():
                rprint(f"  - {source_name}")
            sys.exit(1)
        source_config = config.blueprint.sources[collection]
        entrypoint = source_config.module
        display_name = collection
        collection_to_serve = entrypoint
    else:
        # Use default collection
        try:
            default_collection_name = fxtrconfig.get_default_collection_name(config)
            source_config = config.blueprint.sources[default_collection_name]
            entrypoint = source_config.module
            display_name = default_collection_name
            collection_to_serve = entrypoint
        except RuntimeError as e:
            rprint(f"[yellow]Warning: {e}[/yellow]")
            rprint(
                f"[yellow]Will serve runs from active runset without collection context[/yellow]"
            )
            collection_to_serve = None

    if collection_to_serve:
        try:
            rprint(f"[blue]Loading collection '{display_name}' for serving...[/blue]")

            # Load the collection
            coll = api_lib.load_collection_from_entrypoint(collection_to_serve)

            # Save the collection to the entity store
            subgraph = dag_util.entity_subgraph_to_ipld_blocks(
                named_roots={}, sequence_roots=[coll]
            )
            for cid, entity_data in subgraph.nodes.items():
                data_locator.write_entity_data(entity_data, cid)

            collection_cid = core.entity_cid(coll)
            collection_cid_str = collection_cid.encode(base="base32")

            named_urls[display_name] = f"/view/collection/{collection_cid_str}"
            rprint(
                f"[green]Will serve collection '{display_name}' with CID:"
                f" {collection_cid_str}[/green]"
            )
        except Exception as e:
            rprint(f"[red]Error loading collection '{display_name}': {e}[/red]")
            raise

    # Serve runs from active runset
    rprint(f"[blue]Serving runs from active runset ({len(active_runset.runs)} runs)...[/blue]")

    for task_cid, run_cid in active_runset.runs.items():
        task_cid_str = task_cid.encode(base="base32")
        run_cid_str = run_cid.encode(base="base32")

        # Create a display name using the task CID
        run_display_name = f"active_runset/{task_cid_str}"
        named_urls[run_display_name] = f"/view/entity/{run_cid_str}"

    if named_urls:
        rprint(f"[green]Serving {len(named_urls)} entities:[/green]")
        for name, url in named_urls.items():
            rprint(f"  - {name}: {url}")
    else:
        rprint(
            "[dim]No named URLs available - users can access entities directly via"
            " /view/entity/{cid}[/dim]"
        )

    # Create index callback if collection is provided
    index_callback = None
    if collection_to_serve and display_name:

        def create_index_callback():
            def callback():
                # Re-load the collection and return the URL
                coll = api_lib.load_collection_from_entrypoint(collection_to_serve)
                collection_cid = core.entity_cid(coll)
                collection_cid_str = collection_cid.encode(base="base32")
                return f"/view/collection/{collection_cid_str}"

            return callback

        index_callback = create_index_callback()

    # Create and start the server
    server = FxtrLocatorDebugServer(
        data_locator=data_locator,
        named_urls=named_urls,
        fxtr_config=config,
        dev_mode=True,  # Enable dev mode for now
        index_callback=index_callback,
    )

    rprint(f"[blue]Starting debug server at http://{host}:{port}[/blue]")
    asyncio.run(server.serve(host=host, port=port))


@app.command()
def ls(
    namespec: Annotated[
        str | None,
        typer.Option(
            "-n",
            "--namespec",
            help=(
                "Optional comma-separated list of task names to filter by. Names ending with '**'"
                " match as prefixes."
            ),
        ),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("-c", "--collection", help="Collection name from config")
    ] = None,
    module: Annotated[
        str | None, typer.Option("-m", "--module", help="Python module entry-point")
    ] = None,
) -> None:
    """List all tasks in a collection and their runs in the active runset."""
    config = load_config()

    # Determine which collection to use
    if module:
        # Load module directly
        entrypoint = module
        display_name = module
    elif collection:
        # Load named collection from config
        if collection not in config.blueprint.sources:
            rprint(f"[red]Error: Collection '{collection}' not found in config[/red]")
            rprint("[yellow]Available collection sources:[/yellow]")
            for source_name in config.blueprint.sources.keys():
                rprint(f"  - {source_name}")
            sys.exit(1)
        source_config = config.blueprint.sources[collection]
        entrypoint = source_config.module
        display_name = collection
    else:
        # Use default collection
        try:
            default_collection_name = fxtrconfig.get_default_collection_name(config)
            source_config = config.blueprint.sources[default_collection_name]
            entrypoint = source_config.module
            display_name = default_collection_name
        except RuntimeError as e:
            rprint(f"[red]{e}[/red]")
            sys.exit(1)

    rprint(f"[blue]Listing tasks for collection: {display_name}[/blue]")

    # Load active runset
    active_runset = api_lib.load_active_runset(config)

    # Load the collection and get its tasks
    rprint(f"[blue]Loading collection: {display_name} -> {entrypoint}[/blue]")
    coll = api_lib.load_collection_from_entrypoint(entrypoint)
    rprint(f"[green]Successfully loaded collection[/green]")

    # Extract all Task entities from the collection
    all_module_tasks = api_lib.tasks_in_collection(coll, display_name)

    # Filter tasks based on namespec if provided
    if namespec is not None:
        module_tasks = [task for task in all_module_tasks if task_matches_namespec(task, namespec)]
        rprint(
            f"[green]Found {len(module_tasks)} Task entities (filtered from"
            f" {len(all_module_tasks)} total)[/green]"
        )
    else:
        module_tasks = all_module_tasks
        rprint(f"[green]Found {len(module_tasks)} Task entities[/green]")

    # Display the summary
    rprint(f"\n[bold cyan]═══ Summary for collection: {resc(display_name)} ═══[/bold cyan]")
    rprint(f"[dim]Active runset has {len(active_runset.runs)} runs total[/dim]")

    # Helper function to get all collections to search (reused multiple times)
    def get_all_collections_to_search(coll):
        collections_to_search = set()
        collections_queue = [coll]
        while collections_queue:
            current_coll = collections_queue.pop()
            if current_coll not in collections_to_search:
                collections_to_search.add(current_coll)
                collections_queue.extend(current_coll.dependencies)
        return collections_to_search

    # Helper function to find entity canonical path and collection name
    def find_entity_info(entity_cid, collections_to_search):
        entity_path = None
        entity_collection_name = None
        for search_coll in collections_to_search:
            for defn in search_coll.entity_defns:
                if core.entity_cid(defn.entity) == entity_cid:
                    if defn.canonical_access_path:
                        entity_path_obj = core.frozen_ipldable_to_access_path(
                            defn.canonical_access_path
                        )
                        entity_path = core.access_path_to_str(
                            root_str="", access_path=entity_path_obj
                        )
                    entity_collection_name = search_coll.name
                    break
            if entity_path is not None or entity_collection_name is not None:
                break
        return entity_path, entity_collection_name

    collections_to_search = get_all_collections_to_search(coll)

    # Display secret env var declarations
    secret_declarations = api_lib.discover_secret_env_var_declarations(coll)

    if secret_declarations:
        rprint(
            f"\n[bold]Secret Environment Variable Declarations ({len(secret_declarations)}"
            " handles):[/bold]"
        )

        for handle, declarations in secret_declarations.items():
            handle_cid = core.entity_cid(handle)
            handle_cid_str = handle_cid.encode(base="base32")

            # Find handle's canonical path and collection name
            handle_path, handle_collection_name = find_entity_info(
                handle_cid, collections_to_search
            )

            # Display handle info
            handle_display = handle_path if handle_path else handle_cid_str
            if handle_collection_name:
                rprint(
                    "[yellow]Handle:"
                    f" {resc(handle_collection_name)}:[bold]{resc(handle_display)}[/bold][/yellow]"
                )
            else:
                rprint(f"[yellow]Handle: [bold]{resc(handle_display)}[/bold][/yellow]")

            # Display declarations
            if len(declarations) == 1:
                declaration = declarations[0]
                declaration_cid = core.entity_cid(declaration)
                declaration_cid_str = declaration_cid.encode(base="base32")

                # Determine if optional
                is_optional = isinstance(declaration, core.SecretFromOptionalEnvVarDeclaration)
                optional_str = " (optional)" if is_optional else ""

                # Find declaration's canonical path
                declaration_path, _ = find_entity_info(declaration_cid, collections_to_search)
                declaration_display = declaration_path if declaration_path else f"(anonymous)"

                rprint(
                    f"  [green]✓ Bound to env var ${resc(declaration.env_var)}{optional_str} (in"
                    f" {resc(declaration_display)}, cid: {declaration_cid_str})[/green]"
                )
            else:
                # Multiple declarations - conflict!
                rprint(
                    f"  [red bold]⚠️  CONFLICT: {len(declarations)} declarations found![/red bold]"
                )
                for declaration in declarations:
                    declaration_cid = core.entity_cid(declaration)
                    declaration_cid_str = declaration_cid.encode(base="base32")

                    # Determine if optional
                    is_optional = isinstance(declaration, core.SecretFromOptionalEnvVarDeclaration)
                    optional_str = " (optional)" if is_optional else ""

                    # Find declaration's canonical path
                    declaration_path, _ = find_entity_info(declaration_cid, collections_to_search)
                    declaration_display = declaration_path if declaration_path else f"(anonymous)"

                    rprint(
                        f"  [red]❗ {resc(declaration_display)} → env var"
                        f" ${resc(declaration.env_var)}{optional_str} (cid:"
                        f" {declaration_cid_str})[/red]"
                    )

    # Display runtime env config assignments
    env_assignments = api_lib.discover_runtime_env_config_assignments(coll)

    if env_assignments:
        rprint(f"\n[bold]Runtime Environment Configurations ({len(env_assignments)} plans):[/bold]")

        for plan, assignments in env_assignments.items():
            plan_cid = core.entity_cid(plan)
            plan_cid_str = plan_cid.encode(base="base32")

            # Find plan's canonical path and collection name
            plan_path, plan_collection_name = find_entity_info(plan_cid, collections_to_search)

            # Display plan info
            plan_display = plan_path if plan_path else plan_cid_str
            if plan_collection_name:
                rprint(
                    f"[cyan]Plan: {resc(plan_collection_name)}:[bold]{resc(plan_display)}[/bold]"
                    " (from"
                )
            else:
                rprint(f"[cyan]Plan: [bold]{resc(plan_display)}[/bold][/cyan]")

            # Display assignments
            if len(assignments) == 1:
                assignment = assignments[0]
                assignment_cid = core.entity_cid(assignment)
                assignment_cid_str = assignment_cid.encode(base="base32")

                # Find assignment's canonical path
                assignment_path, _ = find_entity_info(assignment_cid, collections_to_search)
                assignment_display = assignment_path if assignment_path else f"(anonymous)"

                rprint(
                    f"  [green]✓ Active: {resc(assignment_display)} (cid:"
                    f" {assignment_cid_str})[/green]"
                )
            else:
                # Multiple assignments - conflict!
                rprint(
                    f"  [red bold]⚠️  CONFLICT: {len(assignments)} active assignments found![/red"
                    " bold]"
                )
                for assignment in assignments:
                    assignment_cid = core.entity_cid(assignment)
                    assignment_cid_str = assignment_cid.encode(base="base32")

                    # Find assignment's canonical path
                    assignment_path, _ = find_entity_info(assignment_cid, collections_to_search)
                    assignment_display = assignment_path if assignment_path else f"(anonymous)"

                    rprint(
                        f"  [red]❗ {resc(assignment_display)} (cid: {assignment_cid_str})[/red]"
                    )

    # Display runtime coordinator bindings
    coordinator_bindings = api_lib.discover_runtime_coordinator_bindings(coll)

    if coordinator_bindings:
        rprint(
            f"\n[bold]Runtime Coordinator Bindings ({len(coordinator_bindings)} handles):[/bold]"
        )

        for handle, bindings in coordinator_bindings.items():
            handle_cid = core.entity_cid(handle)
            handle_cid_str = handle_cid.encode(base="base32")

            # Find handle's canonical path and collection name
            handle_path, handle_collection_name = find_entity_info(
                handle_cid, collections_to_search
            )

            # Display handle info
            handle_display = handle_path if handle_path else handle_cid_str
            if handle_collection_name:
                rprint(
                    "[cyan]Coordinator:"
                    f" {resc(handle_collection_name)}:[bold]{resc(handle_display)}[/bold]"
                )
            else:
                rprint(f"[cyan]Coordinator: [bold]{resc(handle_display)}[/bold][/cyan]")

            # Display bindings
            if len(bindings) == 1:
                binding = bindings[0]
                binding_cid = core.entity_cid(binding)
                binding_cid_str = binding_cid.encode(base="base32")
                # Find binding's canonical path
                binding_path, _ = find_entity_info(binding_cid, collections_to_search)
                binding_display = binding_path if binding_path else f"(anonymous)"

                rprint(
                    f"  [green]✓ Active: {resc(binding_display)} (cid: {binding_cid_str})[/green]"
                )
            else:
                # Multiple bindings - conflict!
                rprint(
                    f"  [red bold]⚠️  CONFLICT: {len(bindings)} active bindings found![/red bold]"
                )
                for binding in bindings:
                    binding_cid = core.entity_cid(binding)
                    binding_cid_str = binding_cid.encode(base="base32")
                    # Find binding's canonical path
                    binding_path, _ = find_entity_info(binding_cid, collections_to_search)
                    binding_display = binding_path if binding_path else f"(anonymous)"

                    rprint(f"  [red]❗ {resc(binding_display)} (cid: {binding_cid_str})[/red]")

    if not module_tasks:
        rprint("[yellow]No Task entities found in this collection[/yellow]")
    else:
        rprint(f"\n[bold green]Tasks in collection ({len(module_tasks)} found):[/bold green]")

        for blueprint_task in module_tasks:
            task_cid = core.entity_cid(blueprint_task.task_entity)
            task_cid_str = task_cid.encode(base="base32")

            # Check if task is in active runset
            in_runset = task_cid in active_runset.runs

            if blueprint_task.canonical_path:
                canonical_path_str = blueprint_task.canonical_path
                if in_runset:
                    rprint(
                        f"[white]✓ Task [bold]{resc(canonical_path_str)}[/bold] (cid:"
                        f" {task_cid_str})[/white]"
                    )
                else:
                    rprint(
                        f"[dim]  Task [bold]{resc(canonical_path_str)}[/bold] (cid:"
                        f" {task_cid_str})[/dim]"
                    )
            else:
                if in_runset:
                    rprint(f"[white]✓ Task (anonymous) (cid: [bold]{task_cid_str}[/bold])[/white]")
                else:
                    rprint(f"[dim]  Task (anonymous) (cid: [bold]{task_cid_str}[/bold])[/dim]")

            # Show run from active runset if present
            if in_runset:
                run_cid = active_runset.runs[task_cid]
                run_cid_str = run_cid.encode(base="base32")
                rprint(f"    [green]Run in active runset: {run_cid_str}[/green]")

    rprint(f"\n[bold cyan]═══ End summary ═══[/bold cyan]")


class SelectRunBy(str, enum.Enum):
    USE_LAST = "use_last"
    ONLY_UNIQUE = "only_unique"


def _display_final_run_states(
    run_info: dict[cbrrr.CID, tuple[str, cbrrr.CID | None, str]],
    task_canonical_names: dict[cbrrr.CID, str],
) -> None:
    """Display a summary of final run states for all tasks.

    Args:
        run_info: Dict mapping task CID to (status, run_cid, status_message) tuples
        task_canonical_names: Dict mapping task CID to canonical name
    """
    if not run_info:
        return

    rprint(f"\n[bold cyan]═══ Final Run States ═══[/bold cyan]")

    # Sort tasks by canonical name for consistent display
    sorted_task_cids = sorted(
        run_info.keys(), key=lambda cid: task_canonical_names.get(cid, str(cid))
    )

    for task_cid in sorted_task_cids:
        status, run_cid, status_message = run_info[task_cid]
        task_name = task_canonical_names.get(task_cid)

        if not task_name:
            # Fallback to CID if no canonical name was found
            task_name = f"(unknown: {task_cid.encode('base32')[:12]})"

        # Color the status
        if status == "completed":
            status_colored = f"[green]{status}[/green]"
        elif status == "running":
            status_colored = f"[blue]{status}[/blue]"
        elif status == "failed":
            status_colored = f"[red]{status}[/red]"
        elif status == "cancelled":
            status_colored = f"[yellow]{status}[/yellow]"
        else:
            status_colored = status

        # Format the run CID
        run_cid_str = ""
        if run_cid:
            run_cid_str = f" ({run_cid.encode('base32')})"

        # Display the task and its state
        rprint(f"  {resc(task_name)}: {status_colored}{run_cid_str}")

        # Show status message if available
        if status_message:
            # Truncate long messages
            first_line = status_message.split("\n")[0]
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            rprint(f"    [dim]{resc(first_line)}[/dim]")

    rprint(f"[bold cyan]═══ End Run States ═══[/bold cyan]\n")


async def _materialize_with_runner(
    runner_interface: backend_runner.BackendRunnerInterface,
    tasks_to_run: list[core.Task],
    task_canonical_names: dict[cbrrr.CID, str],
    message: str | None,
) -> dict[cbrrr.CID, tuple[str, cbrrr.CID | None, str]]:
    """Run tasks using a BackendRunnerInterface (local or remote).

    Returns a dict mapping task CID to (status, run_cid, status_message) tuples.
    """
    # Subscribe to updates
    watchable = await runner_interface.watch()

    # Launch tasks and get task waiters
    try:
        task_waiters = await runner_interface.launch(
            tasks_to_run,
            message=message,
        )
    except RuntimeError as e:
        rprint(f"[red]Error launching tasks: {e}[/red]")
        raise

    # Start task to show updates in background
    async with anyio.create_task_group() as tg:

        async def _show_updates():
            current_watchable = watchable
            # Track previous states to only show changes
            prev_plan_states: dict[str, dict] = {}
            prev_task_states: dict[str, dict] = {}

            while True:
                result = await current_watchable.poll()
                if result is None:
                    break
                update, next_watchable = result
                current_watchable = next_watchable
                update = typing.cast(Any, update)

                # Display plan state changes
                plan_states = update.get("plan_states", {})
                for plan_cid_str, plan_state in plan_states.items():
                    prev_state = prev_plan_states.get(plan_cid_str)
                    # Only show if state changed or it's new
                    if prev_state != plan_state:
                        plan_cid = cbrrr.CID.decode(plan_cid_str)
                        status = plan_state.get("status", "unknown")
                        msg = plan_state.get("message", "")
                        user_count = plan_state.get("user_count", 0)
                        plan_label = f"plan-{plan_cid.encode('base32')[:12]}"
                        rprint(
                            f"[dim blue]{plan_label}: {status} (users: {user_count}) -"
                            f" {msg}[/dim blue]"
                        )
                        prev_plan_states[plan_cid_str] = plan_state

                # Display task state changes
                task_states = update.get("task_states", {})
                for task_cid_str, task_state in task_states.items():
                    prev_state = prev_task_states.get(task_cid_str)
                    # Only show if state changed or it's new
                    if prev_state != task_state:
                        task_cid = cbrrr.CID.decode(task_cid_str)
                        task_name = task_canonical_names.get(
                            task_cid, task_cid.encode("base32")[:12]
                        )
                        status = task_state.get("status", "unknown")
                        msg = task_state.get("message", "")
                        rprint(f"[dim]{task_name}: {status} - {msg}[/dim]")
                        prev_task_states[task_cid_str] = task_state

        tg.start_soon(_show_updates)

        # Wait for all tasks using task waiters and collect run info
        run_info: dict[cbrrr.CID, tuple[str, cbrrr.CID | None, str]] = {}
        for task_cid_str, task_waiter in task_waiters.items():
            task_cid = cbrrr.CID.decode(task_cid_str)
            status, run_cid, status_message = await task_waiter.wait()
            run_info[task_cid] = (status, run_cid, status_message)

        # Give a short delay for final updates to display
        await anyio.sleep(0.1)

        # Cancel the update display task
        tg.cancel_scope.cancel()

        return run_info

    raise RuntimeError("unreachable")


async def materialize_impl(
    config: fxtrconfig.FxtrConfig,
    namespec: str,
    and_previous: bool = False,
    collection: str | None = None,
    module: str | None = None,
    message: str | None = None,
) -> dict[cbrrr.CID, tuple[str, cbrrr.CID | None, str]]:
    """Run tasks as needed using the active runset for dependencies.

    Tasks already in the active runset are skipped. To re-run a task, first drop it from
    the active runset using `fxtr runset drop`.

    Args:
        config: The fxtr configuration
        namespec: Comma-separated list of task names to filter by. Names ending with '**' match as prefixes.
        and_previous: Also include all tasks that the specified tasks depend on
        collection: Collection name from config
        module: Python module entry-point
        message: The message to use for the run

    Returns:
        Dictionary mapping task CIDs to (status, run_cid, status_message) tuples.
    """
    logfire.configure(
        distributed_tracing=True,
        inspect_arguments=False,
        send_to_logfire="if-token-present",
    )
    logfire.instrument_httpx()

    # Determine which collection to use
    if module:
        # Load module directly
        entrypoint = module
        display_name = module
    elif collection:
        # Load named collection from config
        if collection not in config.blueprint.sources:
            rprint(f"[red]Error: Collection '{collection}' not found in config[/red]")
            rprint("[yellow]Available collection sources:[/yellow]")
            for source_name in config.blueprint.sources.keys():
                rprint(f"  - {source_name}")
            sys.exit(1)
        source_config = config.blueprint.sources[collection]
        entrypoint = source_config.module
        display_name = collection
    else:
        # Use default collection
        try:
            default_collection_name = fxtrconfig.get_default_collection_name(config)
            source_config = config.blueprint.sources[default_collection_name]
            entrypoint = source_config.module
            display_name = default_collection_name
        except RuntimeError as e:
            rprint(f"[red]{e}[/red]")
            sys.exit(1)

    rprint(f"[blue]Materializing tasks from collection: {display_name}[/blue]")

    # Load active runset
    active_runset = api_lib.load_active_runset(config)
    rprint(f"[cyan]Active runset has {len(active_runset.runs)} runs[/cyan]")

    # Load the collection and get its tasks
    rprint(f"[blue]Loading collection: {display_name} -> {entrypoint}[/blue]")
    coll = api_lib.load_collection_from_entrypoint(entrypoint)
    rprint(f"[green]Successfully loaded collection[/green]")

    # Extract all Task entities from the collection
    all_module_tasks = api_lib.tasks_in_collection(coll, display_name)

    # Filter tasks based on namespec
    # TODO-NOW: rework to create requests instead of tasks here; use .@ and .!! syntax for workflows, ** matches !!
    filtered_tasks = [task for task in all_module_tasks if task_matches_namespec(task, namespec)]

    if len(filtered_tasks) == 0:
        rprint(
            f"[yellow]No tasks match namespec '{namespec}' in collection '{display_name}'[/yellow]"
        )
        rprint("[dim]Available tasks:[/dim]")
        for task in all_module_tasks:
            if task.canonical_path:
                rprint(f"  - {task.canonical_path}")
            else:
                task_cid = core.entity_cid(task.task_entity)
                rprint(f"  - (anonymous task: {task_cid})")
        sys.exit(1)

    rprint(f"[green]Found {len(filtered_tasks)} Task entities matching '{namespec}'[/green]")

    # Collect the actual Task entities to run
    initial_tasks = [task.task_entity for task in filtered_tasks]

    # Handle dependency collection if and_previous is enabled
    if and_previous:
        rprint("[blue]Collecting task dependencies due to --and-previous flag[/blue]")
        try:
            # TODO-NOW: extend to also support workflows
            tasks_to_materialize = collect_task_dependencies(
                initial_tasks, [bt.task_entity for bt in all_module_tasks]
            )
            dependency_count = len(tasks_to_materialize) - len(initial_tasks)
            if dependency_count > 0:
                rprint(f"[green]Found {dependency_count} dependency tasks to include[/green]")
            else:
                rprint("[dim]No additional dependency tasks found[/dim]")
        except RuntimeError as e:
            rprint(f"[red]Error collecting dependencies: {e}[/red]")
            sys.exit(1)
    else:
        tasks_to_materialize = initial_tasks

    # Build task canonical names for display (for all tasks being materialized)
    task_canonical_names = {}
    for task in tasks_to_materialize:
        task_cid = core.entity_cid(task)

        # Find the corresponding CollectionTask to get the canonical path
        collection_task = None
        for ct in all_module_tasks:
            # TODO-NOW: extend to also support workflows
            if core.entity_cid(ct.task_entity) == task_cid:
                collection_task = ct
                break

        if collection_task and collection_task.canonical_path:
            task_canonical_names[task_cid] = collection_task.canonical_path

    # Filter out tasks already in active runset (skip them)
    tasks_to_run = []
    tasks_skipped = 0
    all_run_info: dict[cbrrr.CID, tuple[str, cbrrr.CID | None, str]] = {}

    for task in tasks_to_materialize:
        task_cid = core.entity_cid(task)

        if task_cid in active_runset.runs:
            # Task already in runset, skip it
            tasks_skipped += 1
            run_cid = active_runset.runs[task_cid]
            all_run_info[task_cid] = ("completed", run_cid, "Already in active runset")
        else:
            # Task not in runset, need to run it
            tasks_to_run.append(task)

    if tasks_skipped > 0:
        rprint(f"[yellow]Skipping {tasks_skipped} tasks already in active runset[/yellow]")

    if not tasks_to_run:
        rprint("[green]All tasks already in active runset - nothing to do![/green]")
        # Still show the run states for the skipped tasks
        _display_final_run_states(all_run_info, task_canonical_names)
        return all_run_info

    rprint(f"[bold]Starting task execution...[/bold]")
    rprint(f"  Tasks to run: {len(tasks_to_run)}")
    rprint(f"  Using active runset for dependencies")

    # Run the tasks using the backend runner
    try:
        # Create or attach to runner
        async with backend_runner.create_or_attach_runner(config) as runner:
            # Activate the collection (discovers and validates bindings)
            await runner.activate_collection(coll)

            # Run tasks
            executed_run_info = await _materialize_with_runner(
                runner,
                tasks_to_run,
                task_canonical_names,
                message,
            )

        # Merge the executed run info with the skipped tasks
        all_run_info.update(executed_run_info)

        rprint("[bold green]Task materialization completed![/bold green]")

        # Display final run states for all tasks in the materialization set
        _display_final_run_states(all_run_info, task_canonical_names)

        return all_run_info

    except Exception:
        rprint(f"[bold red]Task materialization failed![/bold red]")
        traceback.print_exc()
        raise


@app.command()
def materialize(
    namespec: Annotated[
        str,
        typer.Argument(
            help=(
                "Comma-separated list of task names to filter by. Names ending with '**'"
                " match as prefixes."
            )
        ),
    ],
    and_previous: Annotated[
        bool,
        typer.Option(
            "--and-previous",
            help=(
                "Also include all tasks that the specified tasks depend on. If not enabled, any"
                " previous tasks must already be in the active runset."
            ),
        ),
    ] = False,
    collection: Annotated[
        str | None, typer.Option("-c", "--collection", help="Collection name from config")
    ] = None,
    module: Annotated[
        str | None, typer.Option("-m", "--module", help="Python module entry-point")
    ] = None,
    message: Annotated[
        str | None,
        typer.Option(
            "--message",
            help="The message to use for the run.",
        ),
    ] = None,
) -> None:
    """Run tasks as needed using the active runset for dependencies.

    Tasks already in the active runset are skipped. To re-run a task, first drop it from
    the active runset using `fxtr runset drop`.
    """
    config = load_config()

    try:
        anyio.run(
            lambda: materialize_impl(
                config=config,
                namespec=namespec,
                and_previous=and_previous,
                collection=collection,
                module=module,
                message=message,
            )
        )
    except Exception:
        sys.exit(1)


# ============================================================================
# Runset commands
# ============================================================================

runset_app = typer.Typer(
    help="Manage runsets (sets of task runs)", pretty_exceptions_show_locals=False
)
app.add_typer(runset_app, name="runset")


@runset_app.command("save")
def runset_save(
    name: Annotated[str, typer.Argument(help="Name for the runset")],
) -> None:
    """Save the active runset with a name."""
    config = load_config()

    try:
        # Load active runset
        active_runset = api_lib.load_active_runset(config)

        # Save as named runset
        api_lib.save_named_runset(config, name, active_runset)

        # Get CID for display
        runset_cid = core.entity_cid(active_runset)
        cid_str = runset_cid.encode(base="base32")

        rprint(
            f"[green]Saved active runset as '{name}' with {len(active_runset.runs)} runs[/green]"
        )
        rprint(f"[cyan]CID: {cid_str}[/cyan]")

    except ValueError as e:
        rprint(f"[red]Error: {e}[/red]")
        sys.exit(1)
    except Exception as e:
        rprint(f"[red]Error saving runset: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@runset_app.command("load")
def runset_load(
    name: Annotated[str, typer.Argument(help="Name of the runset to load")],
) -> None:
    """Load a named runset as the active runset."""
    config = load_config()

    try:
        # Load named runset
        named_runset = api_lib.load_named_runset(config, name)

        # Save as active runset
        api_lib.save_active_runset(config, named_runset)

        # Get CID for display
        runset_cid = core.entity_cid(named_runset)
        cid_str = runset_cid.encode(base="base32")

        rprint(
            f"[green]Loaded runset '{name}' as active runset with"
            f" {len(named_runset.runs)} runs[/green]"
        )
        rprint(f"[cyan]CID: {cid_str}[/cyan]")

    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        rprint(
            "[yellow]Available runsets:"
            f" {', '.join(api_lib.list_named_runsets(config)) or 'none'}[/yellow]"
        )
        sys.exit(1)
    except Exception as e:
        rprint(f"[red]Error loading runset: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@runset_app.command("export")
def runset_export(
    name: Annotated[
        str | None, typer.Argument(help="Name of the runset to export (defaults to active)")
    ] = None,
) -> None:
    """Export a runset as Python code."""
    config = load_config()

    try:
        # Load runset
        if name:
            runset = api_lib.load_named_runset(config, name)
            display_name = f"'{name}'"
        else:
            runset = api_lib.load_active_runset(config)
            display_name = "active"

        # Get CID
        runset_cid = core.entity_cid(runset)
        cid_str = runset_cid.encode(base="base32")

        rprint(f"[cyan]# Runset {display_name} with {len(runset.runs)} runs[/cyan]")
        rprint(f'core.RunSetRef(core.decode_cid("{cid_str}"))')

    except FileNotFoundError as e:
        rprint(f"[red]{e}[/red]")
        sys.exit(1)
    except Exception as e:
        rprint(f"[red]Error exporting runset: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@runset_app.command("drop")
def runset_drop(
    identifiers: Annotated[
        list[str] | None, typer.Argument(help="Run CIDs, task CIDs, or task names to drop")
    ] = None,
    and_after: Annotated[
        bool, typer.Option("--and-after", help="Also drop downstream runs")
    ] = False,
    all: Annotated[bool, typer.Option("--all", help="Drop all runs")] = False,
    collection: Annotated[
        str | None, typer.Option("-c", "--collection", help="Collection name from config")
    ] = None,
    module: Annotated[
        str | None, typer.Option("-m", "--module", help="Python module entry-point")
    ] = None,
) -> None:
    """Drop runs from the active runset by run CID, task CID, or task name."""
    config = load_config()

    if not identifiers:
        identifiers = []

    if all and identifiers:
        rprint("[red]Error: Cannot specify both --all and specific identifiers[/red]")
        sys.exit(1)

    if not all and not identifiers:
        rprint("[red]Error: Must specify either --all or specific identifiers to drop[/red]")
        sys.exit(1)

    try:
        # Load active runset
        active_runset = api_lib.load_active_runset(config)

        if all:
            # Drop all runs
            from frozendict import frozendict

            new_runset = core.RunSet(runs=frozendict())
            dropped_count = len(active_runset.runs)
        else:
            # Load collection for name resolution (use default if not specified)
            coll = None
            if module:
                coll = api_lib.load_collection_from_entrypoint(module)
            elif collection:
                if collection not in config.blueprint.sources:
                    rprint(f"[red]Error: Collection '{collection}' not found in config[/red]")
                    sys.exit(1)
                source_config = config.blueprint.sources[collection]
                coll = api_lib.load_collection_from_entrypoint(source_config.module)
            else:
                # Try to use default collection
                try:
                    default_collection_name = fxtrconfig.get_default_collection_name(config)
                    source_config = config.blueprint.sources[default_collection_name]
                    coll = api_lib.load_collection_from_entrypoint(source_config.module)
                except RuntimeError:
                    # No default collection, will only accept CIDs
                    pass

            # Load entity loader for looking up runs
            data_locator = api_lib.get_active_data_locator(config)
            entity_loader = locator_module.CachingEntityLoader.from_locator(data_locator)

            # Resolve identifiers to task CIDs
            task_cids_to_drop = set()
            for identifier in identifiers:
                # Try to parse as CID first
                try:
                    cid = cbrrr.CID.decode(identifier)

                    # Try to load as Run or Task
                    try:
                        entity = entity_loader(cid)
                        if isinstance(entity, core.Run):
                            # It's a run CID, get the task CID
                            task_cid = entity.task
                            # Verify this run is in the active runset
                            if (
                                task_cid not in active_runset.runs
                                or active_runset.runs[task_cid] != cid
                            ):
                                rprint(
                                    f"[red]Error: Run {identifier} is not in the active"
                                    " runset[/red]"
                                )
                                sys.exit(1)
                            task_cids_to_drop.add(task_cid)
                        elif isinstance(entity, core.Task):
                            # It's a task CID directly
                            if cid not in active_runset.runs:
                                rprint(
                                    f"[red]Error: Task {identifier} is not in the active"
                                    " runset[/red]"
                                )
                                sys.exit(1)
                            task_cids_to_drop.add(cid)
                        else:
                            rprint(
                                f"[red]Error: CID {identifier} is neither a Run nor a Task[/red]"
                            )
                            sys.exit(1)
                    except Exception as e:
                        rprint(f"[red]Error loading entity {identifier}: {e}[/red]")
                        sys.exit(1)

                except Exception:
                    # Not a valid CID, try to resolve as task name
                    if coll is None:
                        rprint(
                            f"[red]Error: '{identifier}' is not a valid CID and no collection"
                            " available for name resolution[/red]"
                        )
                        rprint(
                            f"[yellow]Provide a collection with -c/--collection or"
                            f" -m/--module[/yellow]"
                        )
                        sys.exit(1)

                    try:
                        task_cid = api_lib.resolve_task_identifier(identifier, coll)
                        if task_cid not in active_runset.runs:
                            rprint(
                                f"[red]Error: Task '{identifier}' is not in the active runset[/red]"
                            )
                            sys.exit(1)
                        task_cids_to_drop.add(task_cid)
                    except ValueError as e:
                        rprint(f"[red]Error resolving '{identifier}': {e}[/red]")
                        sys.exit(1)

            # If and_after, find descendants
            if and_after:
                all_to_drop = set(task_cids_to_drop)
                for task_cid in task_cids_to_drop:
                    descendants = api_lib.get_run_descendants(config, active_runset, task_cid)
                    all_to_drop.update(descendants)
                task_cids_to_drop = all_to_drop
            else:
                # Check for descendants and error if any
                for task_cid in task_cids_to_drop:
                    descendants = api_lib.get_run_descendants(config, active_runset, task_cid)
                    if descendants:
                        rprint(
                            f"[red]Error: Task {task_cid.encode('base32')} has downstream"
                            " dependencies[/red]"
                        )
                        rprint(
                            f"[yellow]Use --and-after to drop them too, or drop them explicitly"
                            f" first[/yellow]"
                        )
                        sys.exit(1)

            # Drop the runs
            new_runset = active_runset
            dropped_count = 0
            for task_cid in task_cids_to_drop:
                if task_cid in new_runset.runs:
                    new_runset = api_lib.remove_run_from_runset(new_runset, task_cid)
                    dropped_count += 1

        # Save updated runset
        api_lib.save_active_runset(config, new_runset)

        rprint(f"[green]Dropped {dropped_count} runs from active runset[/green]")
        rprint(f"[cyan]Active runset now has {len(new_runset.runs)} runs[/cyan]")

    except Exception as e:
        rprint(f"[red]Error dropping runs: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@runset_app.command("adopt")
def runset_adopt(
    runs: Annotated[list[str], typer.Argument(help="Run CIDs to adopt")],
    and_before: Annotated[
        bool, typer.Option("--and-before", help="Also adopt ancestor runs")
    ] = False,
) -> None:
    """Adopt orphan runs into the active runset by run CID."""
    config = load_config()

    try:
        # Load active runset
        active_runset = api_lib.load_active_runset(config)

        # Load entity loader for looking up runs
        data_locator = api_lib.get_active_data_locator(config)
        entity_loader = locator_module.CachingEntityLoader.from_locator(data_locator)

        # Parse run CIDs and get their task CIDs
        tasks_to_adopt: list[tuple[cbrrr.CID, cbrrr.CID]] = []  # (task_cid, run_cid) pairs

        for run_cid_str in runs:
            try:
                run_cid = cbrrr.CID.decode(run_cid_str)
            except Exception as e:
                rprint(f"[red]Error: '{run_cid_str}' is not a valid CID: {e}[/red]")
                sys.exit(1)

            # Load the run to get its task CID
            try:
                run = entity_loader(run_cid)
                if not isinstance(run, core.Run):
                    rprint(f"[red]Error: CID {run_cid_str} is not a Run entity[/red]")
                    sys.exit(1)

                task_cid = run.task
                tasks_to_adopt.append((task_cid, run_cid))
            except Exception as e:
                rprint(f"[red]Error loading run {run_cid_str}: {e}[/red]")
                sys.exit(1)

        # If and_before, recursively find ancestors
        if and_before:
            all_to_adopt = dict(tasks_to_adopt)  # task_cid -> run_cid
            to_check = [run_cid for _, run_cid in tasks_to_adopt]

            while to_check:
                current_run_cid = to_check.pop()
                ancestors = api_lib.get_run_ancestors(config, current_run_cid)

                for ancestor_task_cid, ancestor_run_cid in ancestors:
                    if ancestor_task_cid not in all_to_adopt:
                        all_to_adopt[ancestor_task_cid] = ancestor_run_cid
                        to_check.append(ancestor_run_cid)

            tasks_to_adopt = list(all_to_adopt.items())
        else:
            # Check for ancestors not in runset and error
            for task_cid, run_cid in tasks_to_adopt:
                ancestors = api_lib.get_run_ancestors(config, run_cid)
                for ancestor_task_cid, _ in ancestors:
                    if ancestor_task_cid not in active_runset.runs:
                        rprint(
                            f"[red]Error: Run {run_cid.encode('base32')} has ancestor not in"
                            " runset[/red]"
                        )
                        rprint(
                            f"[yellow]Ancestor task: {ancestor_task_cid.encode('base32')}[/yellow]"
                        )
                        rprint(
                            f"[yellow]Use --and-before to adopt ancestors too, or adopt them"
                            f" explicitly first[/yellow]"
                        )
                        sys.exit(1)

        # Check for conflicts (task already in runset with a different run)
        for task_cid, run_cid in tasks_to_adopt:
            if task_cid in active_runset.runs:
                existing_run_cid = active_runset.runs[task_cid]
                if existing_run_cid != run_cid:
                    rprint(
                        f"[red]Error: Task {task_cid.encode('base32')} already in active runset"
                        " with a different run[/red]"
                    )
                    rprint(f"[yellow]Existing run: {existing_run_cid.encode('base32')}[/yellow]")
                    rprint(f"[yellow]Trying to adopt: {run_cid.encode('base32')}[/yellow]")
                    sys.exit(1)

        # Adopt all runs (skip if already present with same run)
        new_runset = active_runset
        adopted_count = 0
        for task_cid, run_cid in tasks_to_adopt:
            if task_cid not in new_runset.runs:
                new_runset = api_lib.add_run_to_runset(new_runset, task_cid, run_cid)
                adopted_count += 1

        # Save updated runset
        if adopted_count > 0:
            api_lib.save_active_runset(config, new_runset)
            rprint(f"[green]Adopted {adopted_count} runs into active runset[/green]")
        else:
            rprint(f"[yellow]All specified runs already in active runset[/yellow]")

        rprint(f"[cyan]Active runset now has {len(new_runset.runs)} runs[/cyan]")

    except Exception as e:
        rprint(f"[red]Error adopting runs: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@runset_app.command("list")
def runset_list() -> None:
    """List all named runsets and show active runset info."""
    config = load_config()

    try:
        # Load active runset
        active_runset = api_lib.load_active_runset(config)
        active_cid = core.entity_cid(active_runset)
        active_cid_str = active_cid.encode(base="base32")

        rprint(f"\n[bold cyan]═══ Active Runset ═══[/bold cyan]")
        rprint(f"  Runs: {len(active_runset.runs)}")
        rprint(f"  CID: {active_cid_str}")

        # List named runsets
        named_runsets = api_lib.list_named_runsets(config)

        if named_runsets:
            rprint(f"\n[bold cyan]═══ Named Runsets ═══[/bold cyan]")
            for name in named_runsets:
                try:
                    runset = api_lib.load_named_runset(config, name)
                    runset_cid = core.entity_cid(runset)
                    cid_str = runset_cid.encode(base="base32")
                    rprint(f"  [bold]{name}[/bold]: {len(runset.runs)} runs (CID: {cid_str})")
                except Exception as e:
                    rprint(f"  [bold]{name}[/bold]: [red]Error loading: {e}[/red]")
        else:
            rprint(f"\n[yellow]No named runsets found[/yellow]")

        rprint()

    except Exception as e:
        rprint(f"[red]Error listing runsets: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@app.command()
def runlog(
    task_identifier: Annotated[str, typer.Argument(help="Task name or CID")],
    collection: Annotated[
        str | None, typer.Option("-c", "--collection", help="Collection name from config")
    ] = None,
    module: Annotated[
        str | None, typer.Option("-m", "--module", help="Python module entry-point")
    ] = None,
) -> None:
    """Show all runs for a specific task."""
    config = load_config()

    try:
        # Try to parse as CID first
        task_cid = None
        try:
            task_cid = cbrrr.CID.decode(task_identifier)
        except Exception:
            pass

        # If not a CID, need a collection to resolve
        if task_cid is None:
            if not collection and not module:
                rprint(f"[red]Error: '{task_identifier}' is not a valid CID[/red]")
                rprint(
                    f"[yellow]Please provide a collection with -c/--collection or -m/--module to"
                    f" resolve task names[/yellow]"
                )
                sys.exit(1)

            # Load collection
            if module:
                coll = api_lib.load_collection_from_entrypoint(module)
            else:
                if collection not in config.blueprint.sources:
                    rprint(f"[red]Error: Collection '{collection}' not found in config[/red]")
                    sys.exit(1)
                source_config = config.blueprint.sources[collection]
                coll = api_lib.load_collection_from_entrypoint(source_config.module)

            # Resolve task identifier
            try:
                task_cid = api_lib.resolve_task_identifier(task_identifier, coll)
            except ValueError as e:
                rprint(f"[red]{e}[/red]")
                sys.exit(1)

        # Get all runs for this task
        runs = api_lib.runs_for_task(config, task_cid, only_completed=False)

        if not runs:
            rprint(f"[yellow]No runs found for task {task_cid.encode('base32')}[/yellow]")
            return

        # Display runs
        task_cid_str = task_cid.encode(base="base32")
        rprint(
            f"\n[bold cyan]═══ Runs for task {resc(task_identifier)} ({task_cid_str}) ═══[/bold"
            " cyan]"
        )
        rprint(f"[dim]Found {len(runs)} runs[/dim]\n")

        for run in runs:
            # Color the state
            state = run.state
            if state == "completed":
                state_colored = f"[green]{state}[/green]"
            elif state == "running":
                state_colored = f"[blue]{state}[/blue]"
            elif state == "failed":
                state_colored = f"[red]{state}[/red]"
            elif state == "cancelled":
                state_colored = f"[yellow]{state}[/yellow]"
            else:
                state_colored = state

            # Format the run CID
            run_cid_str = run.run_cid.encode(base="base32") if run.run_cid else "N/A"

            rprint(f"[bold]{resc(run.run_name)}[/bold]")
            rprint(f"  State: {state_colored}")
            rprint(f"  Run CID: {run_cid_str}")

            if run.status_message:
                # Truncate long messages
                message = run.status_message
                lines = message.split("\n")
                if len(lines) > 3:
                    display_message = "\n".join(lines[:3]) + "\n  ..."
                else:
                    display_message = message
                rprint(f"  Message: {resc(display_message)}")

            rprint()

        rprint(f"[bold cyan]═══ End runs ═══[/bold cyan]\n")

    except Exception as e:
        rprint(f"[red]Error retrieving run log: {e}[/red]")
        traceback.print_exc()
        sys.exit(1)


@app.command()
def runserver(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "localhost",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8765,
) -> None:
    """Start a persistent backend runner server for task execution.

    The server allows multiple materialize commands to share a single backend runner,
    improving performance by avoiding repeated environment setup.
    """
    config = load_config()

    async def _runserver_impl():
        """Async implementation of runserver."""
        from ..impls.implserver import ImplServer

        logfire.configure(
            distributed_tracing=True,
            inspect_arguments=False,
            send_to_logfire="if-token-present",
        )
        logfire.instrument_httpx()

        # Check for existing runserver state
        existing_state = api_lib.load_runserver_state(config)
        if existing_state:
            rprint(
                "[red]Error: A runserver is already registered at"
                f" {existing_state.runner_url}[/red]"
            )
            rprint("[yellow]If the server is not running, delete the state file:[/yellow]")
            rprint(f"  {api_lib.get_runserver_state_path(config)}")
            sys.exit(1)

        rprint(f"[bold blue]Starting fxtr runserver at {host}:{port}[/bold blue]")

        try:
            # Create BackendRunner and serve it via ImplServer
            async with backend_runner.BackendRunner.create(config) as runner:
                async with ImplServer(host=host, default_port=port).serving() as server:
                    with server.serve_impl(runner) as runner_url:
                        # Save runserver state with the actual runner URL
                        api_lib.save_runserver_state(config, runner_url)
                        rprint(f"[green]Runserver state saved[/green]")
                        rprint(
                            f"[bold green]Backend runner available at: {runner_url}[/bold green]"
                        )
                        rprint("[dim]Press Ctrl+C to stop the server[/dim]")

                        # Wait for shutdown signal
                        shutdown_event = anyio.Event()

                        async def _handle_signals():
                            with anyio.open_signal_receiver(
                                signal.SIGINT, signal.SIGTERM
                            ) as signal_receiver:
                                async for signum in signal_receiver:
                                    rprint(
                                        f"\n[yellow]Received signal {signum}, shutting"
                                        " down...[/yellow]"
                                    )
                                    shutdown_event.set()
                                    return

                        async with anyio.create_task_group() as tg:
                            tg.start_soon(_handle_signals)
                            await shutdown_event.wait()
                            tg.cancel_scope.cancel()

                        rprint("[blue]Server shutting down...[/blue]")

        finally:
            # Always clean up runserver state file
            api_lib.remove_runserver_state(config)
            rprint("[green]Runserver state cleaned up[/green]")

    try:
        anyio.run(_runserver_impl)
    except KeyboardInterrupt:
        rprint("\n[yellow]Server interrupted[/yellow]")
    except Exception:
        rprint("[bold red]Server error![/bold red]")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    app()
