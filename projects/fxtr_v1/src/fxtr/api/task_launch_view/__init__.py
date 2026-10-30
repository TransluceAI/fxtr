"""Task launch progress view using picoview."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Collection, Mapping

import cbrrr
import msgspec
import picoview

from fxtr.api import api_lib
from fxtr.impl_plans import plan_dep_util, py_impl_plan
from fxtr.object_model import core


class PlanData(msgspec.Struct):
    """Data for a single plan in the UI."""

    cid: str
    type_name: str
    status: str
    status_color: str  # CSS color for status
    message: str


class TaskData(msgspec.Struct):
    """Data for a single task in the UI."""

    cid: str
    name: str
    status: str
    status_color: str  # CSS color for status
    message: str


class SummaryData(msgspec.Struct):
    """Progress summary data."""

    completed: int
    running: int
    pending: int
    cancelled: int
    failed: int
    total: int


class DagNode(msgspec.Struct):
    """DAG node data."""

    id: str
    type: str  # "plan" or "task"
    label: str
    status: str
    status_color: str


class DagEdge(msgspec.Struct):
    """DAG edge data."""

    source: str
    target: str


class TaskLaunchViewData(msgspec.Struct):
    """View data for the task launch UI."""

    plans: list[PlanData]
    tasks: list[TaskData]
    plan_summary: SummaryData
    task_summary: SummaryData
    dag_nodes: list[DagNode]
    dag_edges: list[DagEdge]


class TaskLaunchView(picoview.ReactUIView):
    """A picoview UI for displaying task launch progress with plans and tasks in two columns."""

    def __init__(
        self,
        plans: Collection[core.Plan[Any]],
        tasks: Collection[core.Task],
        task_canonical_names: Mapping[cbrrr.CID, str] | None = None,
    ):
        """Initialize the task launch view.

        Args:
            plans: Collection of implementation plans to display
            tasks: Collection of tasks to display
            task_canonical_names: Optional mapping of task CIDs to human-readable names
        """
        super().__init__()
        self.plans = list(plans)
        self.tasks = list(tasks)
        self.task_canonical_names = task_canonical_names or {}

        # Current state that will be updated externally
        self.plan_status: dict[core.Plan[Any], str] = {plan: "pending" for plan in plans}
        self.plan_msg: dict[core.Plan[Any], str] = {}
        self.run_states: dict[core.Task, api_lib.TaskRunState] = {}

    def _tsx_path(self) -> Path:
        return Path(__file__).parent / "task_launch_view.tsx"

    def _viewdata(self) -> TaskLaunchViewData:
        """Return the data to pass to the view."""
        # Process plan data
        plan_data = []
        plan_counts = {"pending": 0, "setup": 0, "ready": 0, "failed": 0, "cancelled": 0}

        for plan in self.plans:
            cid_str = _nice_cid_str(core.entity_cid(plan))
            if isinstance(plan, py_impl_plan.PyImplPlan):
                type_name = "PyImplPlan: " + plan.constructor
            else:
                type_name = type(plan).__name__

            status = self.plan_status.get(plan, "pending")
            message = self.plan_msg.get(plan, "")

            # Map status to color
            status_color = {
                "ready": "#22c55e",  # green
                "setup": "#3b82f6",  # blue
                "failed": "#ef4444",  # red
                "cancelled": "#f59e0b",  # yellow
                "pending": "#6b7280",  # gray
            }.get(status, "#6b7280")

            plan_data.append(
                PlanData(
                    cid=cid_str,
                    type_name=type_name,
                    status=status,
                    status_color=status_color,
                    message=message,
                )
            )
            plan_counts[status] = plan_counts.get(status, 0) + 1

        # Process task data
        task_data = []
        task_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}

        for task in self.tasks:
            task_cid = core.entity_cid(task)
            cid_str = _nice_cid_str(task_cid)
            name = self.task_canonical_names.get(task_cid, f"Task({str(task_cid)[:8]})")
            run_state = self.run_states.get(task)

            if run_state:
                status = run_state.state
                message = run_state.status_message or ""
                if run_state.run_cid:
                    message = f"=> {run_state.run_cid.encode(base='base32')}\n{message}"
            else:
                status = "pending"
                message = ""

            # Map status to color
            status_color = {
                "completed": "#22c55e",  # green
                "running": "#3b82f6",  # blue
                "failed": "#ef4444",  # red
                "cancelled": "#f59e0b",  # yellow
                "pending": "#6b7280",  # gray
            }.get(status, "#6b7280")

            task_data.append(
                TaskData(
                    cid=cid_str,
                    name=name,
                    status=status,
                    status_color=status_color,
                    message=message,
                )
            )
            task_counts[status] = task_counts.get(status, 0) + 1

        # Create summary data
        plan_summary = SummaryData(
            completed=plan_counts.get("ready", 0),  # plans use "ready" instead of "completed"
            running=plan_counts.get("setup", 0),  # plans use "setup" instead of "running"
            pending=plan_counts.get("pending", 0),
            cancelled=plan_counts.get("cancelled", 0),
            failed=plan_counts.get("failed", 0),
            total=len(self.plans),
        )

        task_summary = SummaryData(
            completed=task_counts.get("completed", 0),
            running=task_counts.get("running", 0),
            pending=task_counts.get("pending", 0),
            cancelled=task_counts.get("cancelled", 0),
            failed=task_counts.get("failed", 0),
            total=len(self.tasks),
        )

        # Create DAG nodes and edges
        dag_nodes, dag_edges = self._create_dag_data(plan_data, task_data)

        return TaskLaunchViewData(
            plans=plan_data,
            tasks=task_data,
            plan_summary=plan_summary,
            task_summary=task_summary,
            dag_nodes=dag_nodes,
            dag_edges=dag_edges,
        )

    def _create_dag_data(
        self, plan_data: list[PlanData], task_data: list[TaskData]
    ) -> tuple[list[DagNode], list[DagEdge]]:
        """Create DAG nodes and edges for visualization."""
        nodes = []
        edges = []

        # Create mapping from CID strings to plan/task objects for lookups
        plan_by_cid = {}
        task_by_cid = {}

        for plan in self.plans:
            cid_str = _nice_cid_str(core.entity_cid(plan))
            plan_by_cid[cid_str] = plan

        for task in self.tasks:
            cid_str = _nice_cid_str(core.entity_cid(task))
            task_by_cid[cid_str] = task

        # Add plan nodes
        for plan_info in plan_data:
            # For PyImplPlan nodes, extract just the entrypoint name
            label = plan_info.type_name
            if plan_info.type_name.startswith("PyImplPlan: "):
                label = plan_info.type_name.removeprefix("PyImplPlan: ")

            nodes.append(
                DagNode(
                    id=plan_info.cid,
                    type="plan",
                    label=label,
                    status=plan_info.status,
                    status_color=plan_info.status_color,
                )
            )

        # Add task nodes
        for task_info in task_data:
            nodes.append(
                DagNode(
                    id=task_info.cid,
                    type="task",
                    label=task_info.name,
                    status=task_info.status,
                    status_color=task_info.status_color,
                )
            )

        # Add edges for plan dependencies
        for plan in self.plans:
            plan_cid_str = _nice_cid_str(core.entity_cid(plan))
            deps = plan_dep_util.plan_deps(plan, recursive=False)

            for dep in deps:
                dep_cid_str = _nice_cid_str(core.entity_cid(dep))
                # Only add edge if both nodes exist in our visualization
                if dep_cid_str in plan_by_cid or dep_cid_str in task_by_cid:
                    edges.append(DagEdge(source=dep_cid_str, target=plan_cid_str))

        # Add edges from tasks to their function plans
        for task in self.tasks:
            task_cid_str = _nice_cid_str(core.entity_cid(task))
            task_function_cid_str = _nice_cid_str(core.entity_cid(task.function))

            # Task depends on its function plan
            if task_function_cid_str in plan_by_cid:
                edges.append(DagEdge(source=task_function_cid_str, target=task_cid_str))

        # Add edges for task result handle dependencies
        for plan in self.plans:
            if isinstance(plan, core.TaskResultHandle):
                plan_cid_str = _nice_cid_str(core.entity_cid(plan))
                task_ref_cid_str = _nice_cid_str(plan.task_ref)

                # Task result handle depends on the task it refers to
                if task_ref_cid_str in task_by_cid:
                    edges.append(DagEdge(source=task_ref_cid_str, target=plan_cid_str))

        return nodes, edges

    def update_state(
        self,
        plan_status: Mapping[core.Plan[Any], str],
        plan_msg: Mapping[core.Plan[Any], str],
        run_states: Mapping[core.Task, api_lib.TaskRunState],
    ) -> None:
        """Update the current state of plans and tasks."""
        self.plan_status = dict(plan_status)
        self.plan_msg = dict(plan_msg)
        self.run_states = dict(run_states)

        # Trigger UI update
        self.schedule_update()


def _nice_cid_str(cid: cbrrr.CID) -> str:
    """Get a nice string representation of a CID."""
    b32 = cid.encode(base="base32")
    assert b32.startswith("bafyrei")
    return "@" + b32.removeprefix("bafyrei")
