"""Entry point for clydra - session management, worker loop, and main function."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import signal
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import cbor2
from anyio import open_signal_receiver
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk.types import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    ToolPermissionContext,
)

from clydra.file_list import (
    FileListController,
    FileListControllerAnchorCapturer,
    FileListControllerSnapshotter,
)
from clydra.interrupt import (
    InterruptController,
    InterruptControllerAnchorCapturer,
    InterruptControllerSnapshotter,
)
from clydra.manager import ClydraManager
from clydra.model import ModelController, ModelControllerAnchorCapturer, ModelControllerSnapshotter
from clydra.permission_dialog import (
    PermissionDialogController,
    PermissionDialogControllerAnchorCapturer,
    PermissionDialogControllerSnapshotter,
    PermissionRequest,
    Question,
    QuestionOption,
    QuestionRequest,
)
from clydra.permission_mode import (
    PERMISSION_MODES,
    PermissionModeController,
    PermissionModeControllerAnchorCapturer,
    PermissionModeControllerSnapshotter,
)
from clydra.snapshotter import (
    CLYDRA_ROOT_KEY,
    ClydraManagerAnchorCapturer,
    ClydraManagerSnapshotter,
)
from thingwatcher import Watcher, register_snapshotter
from thingwatcher.api import register_anchor_capturer, register_renderer
from thingwatcher.registry import configure_rendering

# Default port for thingwatcher server
DEFAULT_PORT = 8770

# Directory containing renderer files
RENDERERS_DIR = Path(__file__).parent / "renderers"


# =============================================================================
# SDK message conversion
# =============================================================================


def message_to_dict(message: Any) -> dict[str, Any]:
    """Convert an SDK message to a dict with type information preserved.

    Adds _event_type to the message and _block_type to any content blocks.
    StreamEvent messages are handled specially to preserve the raw event structure.
    """
    event_dict = dataclasses.asdict(message)
    event_dict["_event_type"] = type(message).__name__

    # StreamEvent: already has the right structure, just add event type
    if isinstance(message, StreamEvent):
        return event_dict

    # Add type info to content blocks if present
    if "content" in event_dict and isinstance(event_dict["content"], list):
        # Get the original content blocks from the message to extract types
        original_content = getattr(message, "content", [])
        for i, block in enumerate(event_dict["content"]):
            if isinstance(block, dict) and i < len(original_content):
                block["_block_type"] = type(original_content[i]).__name__

    return event_dict


# =============================================================================
# Transcript history loading
# =============================================================================


def load_transcript_history(transcript_path: str) -> list[dict[str, Any]]:
    """Load and parse JSONL transcript into event dicts."""
    events: list[dict[str, Any]] = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    event = parse_transcript_entry(entry)
                    if event:
                        events.append(event)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return events


def add_block_types(content: Any) -> Any:
    """Add _block_type to content blocks based on their type field.

    Maps transcript format types to our internal _block_type names:
    - "text" -> "TextBlock"
    - "thinking" -> "ThinkingBlock"
    - "tool_use" -> "ToolUseBlock"
    - "tool_result" -> "ToolResultBlock"
    """
    if not isinstance(content, list):
        return content

    type_map = {
        "text": "TextBlock",
        "thinking": "ThinkingBlock",
        "tool_use": "ToolUseBlock",
        "tool_result": "ToolResultBlock",
    }

    for block in content:
        if isinstance(block, dict) and "type" in block:
            block_type = block.get("type")
            if block_type in type_map:
                block["_block_type"] = type_map[block_type]

    return content


def parse_transcript_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a transcript JSONL entry to our event format."""
    entry_type = entry.get("type")
    message = entry.get("message", {})

    if entry_type == "user" and message.get("role") == "user":
        # User message - convert to UserMessage-like structure
        content = message.get("content")
        return {
            "_event_type": "user_message",
            "content": add_block_types(content) if isinstance(content, list) else content,
            "uuid": entry.get("uuid"),
            "timestamp": entry.get("timestamp"),
        }
    elif entry_type == "assistant" and message.get("role") == "assistant":
        # Assistant message - convert to AssistantMessage-like structure
        content = message.get("content", [])
        return {
            "_event_type": "AssistantMessage",
            "content": add_block_types(content),
            "model": message.get("model"),
            "uuid": entry.get("uuid"),
            "timestamp": entry.get("timestamp"),
        }
    elif entry_type == "queue-operation":
        # Skip queue operations - not useful for display
        return None
    else:
        # Unknown entry type - wrap as-is
        return {
            "_event_type": "unknown_history_event",
            "raw": entry,
        }


# =============================================================================
# Session management
# =============================================================================


@dataclasses.dataclass
class SessionRestart:
    """Signal to restart the session with new configuration."""

    session_id: str | None = None


def create_can_use_tool_callback(
    watcher: Watcher,
    dialog_ctrl: PermissionDialogController,
):
    """Create a can_use_tool callback that blocks on the dialog thing's receive()."""

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        request_id = str(uuid.uuid4())

        # Handle AskUserQuestion specially
        if tool_name == "AskUserQuestion":
            # Parse questions from input_data
            questions = [
                Question(
                    question=q.get("question", ""),
                    header=q.get("header", ""),
                    options=[
                        QuestionOption(
                            label=o.get("label", ""), description=o.get("description", "")
                        )
                        for o in q.get("options", [])
                    ],
                    multi_select=q.get("multiSelect", False),
                )
                for q in input_data.get("questions", [])
            ]
            dialog_ctrl.pending_request = QuestionRequest(
                request_id=request_id,
                questions=questions,
            )
        else:
            # Regular tool permission request
            dialog_ctrl.pending_request = PermissionRequest(
                request_id=request_id,
                tool_name=tool_name,
                tool_input=input_data,
                suggestions=[
                    s.to_dict() if hasattr(s, "to_dict") else s for s in context.suggestions
                ],
            )

        # Notify frontend that dialog should appear
        watcher.streaming_update(dialog_ctrl)

        # Block until user responds via the dialog thing's receive()
        payload = await watcher.receive(dialog_ctrl)

        # Clear pending request
        dialog_ctrl.pending_request = None
        watcher.streaming_update(dialog_ctrl)

        # Process response
        action = payload.get("action", "deny")

        if action == "abort":
            # Abort = deny with interrupt
            return PermissionResultDeny(
                message="User aborted",
                interrupt=True,
            )
        elif action == "allow":
            if tool_name == "AskUserQuestion":
                # For questions, return the answers in updated_input
                return PermissionResultAllow(
                    updated_input={
                        "questions": input_data.get("questions", []),
                        "answers": payload.get("answers", {}),
                    },
                )
            else:
                # For permissions, pass through updated_input and updated_permissions
                return PermissionResultAllow(
                    updated_input=payload.get("updated_input"),
                    updated_permissions=payload.get("updated_permissions"),
                )
        else:
            # Deny
            return PermissionResultDeny(
                message=payload.get("message", "User denied"),
                interrupt=payload.get("interrupt", False),
            )

    return can_use_tool


def create_client_options(
    manager: ClydraManager,
    dialog_ctrl: PermissionDialogController,
    watcher: Watcher,
    session_id: str | None = None,
) -> ClaudeAgentOptions:
    """Create client options, optionally for resuming a session."""

    async def on_user_prompt(
        input_data: HookInput, _tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        # Always capture transcript_path when available
        transcript_path = input_data.get("transcript_path")
        if transcript_path and manager.transcript_path != transcript_path:
            manager.transcript_path = transcript_path
            watcher.streaming_update(manager)

        # Handle transcript loading for resumed sessions
        if session_id and not manager._transcript_loaded:
            if transcript_path:
                try:
                    history = load_transcript_history(transcript_path)
                    # Prepend history to events
                    current_events = manager.get_events()
                    manager.clear_events()
                    manager.append_events(history + current_events)
                    manager._transcript_loaded = True
                except Exception as e:
                    print(f"Warning: Could not load transcript: {e}")

        # Check for /noop command - allows loading session content without a response
        prompt = input_data.get("prompt", "")
        if prompt == "!noop":
            return {"continue_": False, "stop_reason": "No-op requested, no response needed"}

        return {}  # No modifications to the prompt

    hooks: dict[str, list[HookMatcher]] = {
        "UserPromptSubmit": [HookMatcher(matcher=None, hooks=[on_user_prompt])]
    }

    # Create the can_use_tool callback
    can_use_tool = create_can_use_tool_callback(watcher, dialog_ctrl)

    return ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code"},
        setting_sources=["user", "project", "local"],
        max_thinking_tokens=31999,
        cwd=manager.cwd,
        resume=session_id,
        hooks=hooks,
        include_partial_messages=True,
        can_use_tool=can_use_tool,
    )


# =============================================================================
# Worker loop
# =============================================================================


async def run_session(
    watcher: Watcher,
    manager: ClydraManager,
    client: ClaudeSDKClient,
    interrupt_ctrl: InterruptController,
    perm_mode_ctrl: PermissionModeController,
    model_ctrl: ModelController,
) -> SessionRestart | None:
    """Run a single session, processing messages until a restart is requested.

    Returns SessionRestart when /clear or /resume commands are received, None otherwise.
    """
    manager._client = client
    manager.state = "waiting_input"
    watcher.tick(
        "info",
        "Session started" if not manager.session_id else f"Resumed session {manager.session_id}",
    )

    while True:
        # Wait for user input (blocks until triggered)
        payload = await watcher.receive(manager)
        text = payload.get("text", "").strip()

        # Handle special commands by returning SessionRestart
        if text == "/clear":
            return SessionRestart(session_id=None)

        if text.startswith("/resume "):
            session_id = text.split(" ", 1)[1].strip()
            print(f"Resuming session {session_id}")
            return SessionRestart(session_id=session_id)

        # Add user message to events
        manager.append_event(
            {
                "_event_type": "user_message",
                "content": text,
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Send to Claude
        manager.state = "streaming"
        interrupt_ctrl.enabled = True
        watcher.streaming_update(manager)
        watcher.streaming_update(interrupt_ctrl)

        await client.query(text)
        trailing_stream_count = 0
        async for message in client.receive_response():
            event_dict = message_to_dict(message)
            is_stream_event = event_dict.get("_event_type") == "StreamEvent"

            # Pop trailing StreamEvents when a non-stream event arrives
            if not is_stream_event and trailing_stream_count > 0:
                manager.pop_events(trailing_stream_count)
                trailing_stream_count = 0

            manager.append_event(event_dict)
            watcher.streaming_update(manager)

            if is_stream_event:
                trailing_stream_count += 1

            # Capture session_id from ResultMessage
            if isinstance(message, ResultMessage):
                if not manager.session_id:
                    print(f"Captured session_id: {message.session_id}")
                manager.session_id = message.session_id

            # Handle init SystemMessage to update permission mode and model
            if event_dict.get("_event_type") == "SystemMessage":
                data = event_dict.get("data", {})
                if data.get("subtype") == "init":
                    if "model" in data:
                        model_ctrl.current_model = data["model"]
                        model_ctrl.error = False
                        watcher.streaming_update(model_ctrl)
                    if "permissionMode" in data and data["permissionMode"] in PERMISSION_MODES:
                        perm_mode_ctrl.current_mode = data["permissionMode"]
                        watcher.streaming_update(perm_mode_ctrl)

        manager.state = "waiting_input"
        interrupt_ctrl.enabled = False
        watcher.streaming_update(manager)
        watcher.streaming_update(interrupt_ctrl)
        watcher.tick("info", "Turn complete")


async def interrupt_worker(
    watcher: Watcher,
    interrupt_ctrl: InterruptController,
) -> None:
    """Background task that handles interrupt triggers."""
    while True:
        payload = await watcher.receive(interrupt_ctrl)

        # Validate payload to avoid accidental triggers
        if payload.get("interrupt") is not True:
            print(f"Interrupt: ignoring invalid payload {payload}")
            continue

        manager = interrupt_ctrl._manager
        if manager and manager._client and interrupt_ctrl.enabled:
            try:
                await manager._client.interrupt()
            except Exception as e:
                print(f"Interrupt failed: {e}")


async def permission_mode_worker(
    watcher: Watcher,
    perm_mode_ctrl: PermissionModeController,
) -> None:
    """Background task that handles permission mode changes."""
    while True:
        payload = await watcher.receive(perm_mode_ctrl)

        new_mode = payload.get("mode")
        if new_mode not in PERMISSION_MODES:
            print(f"Permission mode: ignoring invalid mode {new_mode}")
            continue

        manager = perm_mode_ctrl._manager
        if manager and manager._client:
            try:
                await manager._client.set_permission_mode(new_mode)
                # Only update state if SDK call succeeded
                perm_mode_ctrl.current_mode = new_mode
                watcher.streaming_update(perm_mode_ctrl)
            except Exception as e:
                print(f"Set permission mode failed: {e}")
                # Don't update state on failure


async def model_worker(
    watcher: Watcher,
    model_ctrl: ModelController,
) -> None:
    """Background task that handles model changes."""
    while True:
        payload = await watcher.receive(model_ctrl)

        new_model = payload.get("model", "").strip()
        if not new_model:
            # Empty model submission - ignore
            continue

        manager = model_ctrl._manager
        if manager and manager._client:
            try:
                await manager._client.set_model(new_model)
                # Only update state if SDK call succeeded
                model_ctrl.current_model = new_model
                model_ctrl.error = False
                watcher.streaming_update(model_ctrl)
            except Exception as e:
                print(f"Set model failed: {e}")
                # Set error state and push error event
                model_ctrl.error = True
                watcher.streaming_update(model_ctrl)
                manager.append_event(
                    {
                        "_event_type": "ClydraInternalError",
                        "message": f"Failed to set model to '{new_model}': {e}",
                        "timestamp": datetime.now().isoformat(),
                    }
                )
                watcher.streaming_update(manager)


async def worker(watcher: Watcher, manager: ClydraManager, port: int) -> None:
    """Main worker loop: receive input, send to SDK, stream responses.

    Uses an outer loop to manage client lifecycle with proper context managers.
    SessionRestart exceptions from run_session trigger a new iteration with
    fresh client configuration.
    """
    # Register snapshotters and anchor capturers for all things
    register_snapshotter(ClydraManager, ClydraManagerSnapshotter())
    register_anchor_capturer(ClydraManager, ClydraManagerAnchorCapturer())
    configure_rendering("ClydraRoot", "#4a90d9")

    register_snapshotter(InterruptController, InterruptControllerSnapshotter())
    register_anchor_capturer(InterruptController, InterruptControllerAnchorCapturer())
    configure_rendering("InterruptButton", "#a33333")

    register_snapshotter(PermissionModeController, PermissionModeControllerSnapshotter())
    register_anchor_capturer(PermissionModeController, PermissionModeControllerAnchorCapturer())
    configure_rendering("PermissionMode", "#4a90d9")

    register_snapshotter(PermissionDialogController, PermissionDialogControllerSnapshotter())
    register_anchor_capturer(PermissionDialogController, PermissionDialogControllerAnchorCapturer())
    configure_rendering("PermissionDialog", "#4a4a6a")

    register_snapshotter(ModelController, ModelControllerSnapshotter())
    register_anchor_capturer(ModelController, ModelControllerAnchorCapturer())
    configure_rendering("ModelPicker", "#4a90d9")

    register_snapshotter(FileListController, FileListControllerSnapshotter())
    register_anchor_capturer(FileListController, FileListControllerAnchorCapturer())
    configure_rendering("FileList", "#5a8a5a")

    # Register pre-bundled renderers from dist/main.js
    # Built via: cd clydra/renderers && npm run build
    register_renderer(
        [
            "ClydraRoot",
            "InterruptButton",
            "PermissionMode",
            "PermissionDialog",
            "ModelPicker",
            "FileList",
        ],
        RENDERERS_DIR / "dist" / "main.js",
        imports={
            # CSS extracted by esbuild from diff.module.css
            "./main.css": RENDERERS_DIR
            / "dist"
            / "main.css",
        },
    )
    # Create controllers
    interrupt_ctrl = InterruptController(_manager=manager)
    perm_mode_ctrl = PermissionModeController(_manager=manager)
    model_ctrl = ModelController(_manager=manager)
    dialog_ctrl = PermissionDialogController()
    file_list_ctrl = FileListController(cwd=str(manager.cwd))

    # Track all things
    watcher.track(manager)
    watcher.track(interrupt_ctrl)
    watcher.track(perm_mode_ctrl)
    watcher.track(model_ctrl)
    watcher.track(dialog_ctrl)
    watcher.track(file_list_ctrl)
    watcher.add_root("clydra", manager)

    # Print embed URL for convenience
    key_b64 = base64.urlsafe_b64encode(cbor2.dumps(CLYDRA_ROOT_KEY)).decode()
    print(f"\nEmbed view: http://localhost:{port}/embed?key={key_b64}")
    print(f"Full UI: http://localhost:{port}/")
    print()

    # Create task group for background workers and main session loop
    async with anyio.create_task_group() as tg:
        # Spawn background workers
        tg.start_soon(interrupt_worker, watcher, interrupt_ctrl)
        tg.start_soon(permission_mode_worker, watcher, perm_mode_ctrl)
        tg.start_soon(model_worker, watcher, model_ctrl)

        # Main session loop (outer loop manages client lifecycle)
        session_id: str | None = None
        while True:
            # Reset manager state for new/resumed session
            manager.session_id = session_id
            manager.transcript_path = None  # Will be set by hook
            manager.clear_events()
            manager._transcript_loaded = False

            # Reset permission mode and model to defaults for new session
            perm_mode_ctrl.current_mode = "default"
            watcher.streaming_update(perm_mode_ctrl)
            model_ctrl.current_model = ""
            model_ctrl.error = False
            watcher.streaming_update(model_ctrl)

            options = create_client_options(manager, dialog_ctrl, watcher, session_id)
            async with ClaudeSDKClient(options=options) as client:
                # Sync permission mode with SDK
                try:
                    await client.set_permission_mode("default")
                except Exception as e:
                    print(f"Warning: Could not set initial permission mode: {e}")

                result = await run_session(
                    watcher, manager, client, interrupt_ctrl, perm_mode_ctrl, model_ctrl
                )
                if isinstance(result, SessionRestart):
                    session_id = result.session_id
                else:
                    # run_session returned None (shouldn't happen), restart fresh
                    session_id = None


# =============================================================================
# Entry point
# =============================================================================


async def async_main(port: int = DEFAULT_PORT) -> None:
    """Async entry point for clydra."""
    db_path = f"/tmp/clydra_{datetime.now().isoformat()}.db"
    print(f"Using database: {db_path}")

    manager = ClydraManager(cwd=Path.cwd())
    watcher = Watcher(db_path=db_path, port=port)

    shutdown_event = asyncio.Event()

    async def signal_handler() -> None:
        with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for signum in signals:
                if shutdown_event.is_set():
                    print("\nForce shutdown...")
                    outer_cancel_scope.cancel()
                    return
                else:
                    if signum == signal.SIGINT:
                        print("\nShutting down gracefully...")
                    else:
                        print("\nTerminated! Shutting down gracefully...")
                    shutdown_event.set()
                    workflow_cancel_scope.cancel()

    with anyio.CancelScope() as outer_cancel_scope:
        async with anyio.create_task_group() as signals_tg:
            signals_tg.start_soon(signal_handler)
            async with watcher.watching_and_serving():
                try:
                    with anyio.CancelScope() as workflow_cancel_scope:
                        await worker(watcher, manager, port)
                except Exception as e:
                    print(f"Error: {e}")
                    import traceback

                    traceback.print_exc()
                finally:
                    shutdown_event.set()
                print("Waiting for shutdown...")
                await shutdown_event.wait()
            signals_tg.cancel_scope.cancel()


def run() -> None:
    """Synchronous entry point for clydra (used by console script)."""
    import argparse

    parser = argparse.ArgumentParser(description="Clydra - Claude Code frontend")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port for thingwatcher server (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    print("Starting Clydra...")
    asyncio.run(async_main(port=args.port))


if __name__ == "__main__":
    run()
