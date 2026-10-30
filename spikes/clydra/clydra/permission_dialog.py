"""Permission dialog controller for clydra - handles tool permission and question requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

# The key for the permission dialog thing
PERMISSION_DIALOG_KEY = ["clydra_permission_dialog"]


@dataclass
class PermissionRequest:
    """A pending permission request waiting for user response.

    Attributes:
        request_id: Unique ID for this request (for frontend state reset detection).
        tool_name: The name of the tool requesting permission.
        tool_input: The input data for the tool.
        suggestions: SDK permission suggestions that the user can apply.
    """

    request_id: str
    tool_name: str
    tool_input: dict[str, Any]
    suggestions: list[dict[str, Any]]


@dataclass
class QuestionOption:
    """An option for a question.

    Attributes:
        label: Short label for the option.
        description: Longer description of the option.
    """

    label: str
    description: str


@dataclass
class Question:
    """A question from Claude's AskUserQuestion tool.

    Attributes:
        question: The full question text.
        header: Short label for the question (max 12 chars).
        options: Available choices for this question.
        multi_select: Whether multiple options can be selected.
    """

    question: str
    header: str
    options: list[QuestionOption]
    multi_select: bool


@dataclass
class QuestionRequest:
    """A pending question request from AskUserQuestion tool.

    Attributes:
        request_id: Unique ID for this request (for frontend state reset detection).
        questions: List of questions to ask the user.
    """

    request_id: str
    questions: list[Question]


@dataclass
class PermissionDialogController:
    """Controller for permission and question dialogs.

    This thing's receive() is used to block until user responds to a request.
    The can_use_tool callback sets pending_request, updates the thing, then awaits receive().

    Attributes:
        pending_request: The current pending request (permission or question), or None.
    """

    pending_request: PermissionRequest | QuestionRequest | None = None


class PermissionDialogControllerSnapshotter(StatefulOwningSnapshotter):
    """Simple snapshotter for PermissionDialogController - always returns full state."""

    def snapshot_owned(self, controller: PermissionDialogController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: PermissionDialogController) -> ThingState:
        pending_data = None
        req = controller.pending_request

        if isinstance(req, PermissionRequest):
            pending_data = {
                "type": "permission",
                "request_id": req.request_id,
                "tool_name": req.tool_name,
                "tool_input": req.tool_input,
                "suggestions": req.suggestions,
            }
        elif isinstance(req, QuestionRequest):
            pending_data = {
                "type": "question",
                "request_id": req.request_id,
                "questions": [
                    {
                        "question": q.question,
                        "header": q.header,
                        "options": [
                            {"label": o.label, "description": o.description} for o in q.options
                        ],
                        "multiSelect": q.multi_select,
                    }
                    for q in req.questions
                ],
            }

        return ThingState(
            key=PERMISSION_DIALOG_KEY,
            thing_type="PermissionDialog",
            thing_data={"pending_request": pending_data},
            links=[],
        )


class PermissionDialogControllerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for PermissionDialogController - returns a ref to the dialog thing."""

    def handle(self, anchor: PermissionDialogController) -> ThingRef:
        return ThingRef(key=PERMISSION_DIALOG_KEY)
