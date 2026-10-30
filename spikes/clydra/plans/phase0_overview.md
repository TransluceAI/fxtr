Goal of phase 0: Build a custom claude-code frontend attached to thingwatcher and running via claude agent SDK that can be used as the starting point for future experimentation.

Background:
- claude agent sdk:
    - https://platform.claude.com/docs/en/agent-sdk/python
    - https://github.com/anthropics/claude-agent-sdk-python
    - also checked out in /Users/daniel/tmp/claude-agent-sdk-python
    - we will want to enable
        - system_prompt={"type": "preset", "preset": "claude_code"}
        - setting_sources=["user", "project", "local"]
        - max_thinking_tokens=31999
- thingwatcher:
    - a WIP tool for very quickly spinning up frontends for active Python programs and allowing users to navigate between them and send them messages
    - in `clydra/../thingwatcher` (sibling project)
    - see `thingwatcher/examples/demo_pause_trigger`, `.../demo_multi_chunk`, `.../demo_dynamic_rendering` for examples of how to use

Desired behavior:
- Python package managed with uv, depending on thingwatcher as a relative local import
- Tracks a single thing with thing key `["clydra_root"]`
- This thing corresponds to a Python manager object that manages the clydra session
- Python manager object holds the state for a worker task (an async method on the manager) which manages claude code sessions
- `anyio` for all async ops
- frontend rendering should be extremely minimal, it should be plain text monospace, black background, mostly white. some colors and styles allowed but mostly to emphasize e.g. tool calls, mostly shades of gray or primary colors. can use tasteful square outlines to show "panels" or "cards", but carefully arranged so that they are integer character multiples wide (e.g. you can use calc to subtract some number of pixels from `1ch`). it should be styled like a TUI except it's actually HTML/CSS.
- frontend should support:
    - initially:
        - sending user messages by typing them in a textbox
        - receiving streaming assistant responses back
        - viewing content of every message sent back over the streaming api, but with details/summary collapsing for ones that are not direct messages between user and assistant
        - specifically intercepting message `/clear` to create a new session and `/resume {session-id}` which should resume a claude code session
    - afterward:
        - interrupting at any time, via a separate thing in a separate anyio task that just waits in a loop to be triggered
        - changing permission mode
        - using "@-mentions" via react-mentions and autocompleting to filenames in the dir clydra was started in
        - using slash commands and detecting them via https://platform.claude.com/docs/en/agent-sdk/slash-commands, also via react-mentions
        - showing permission dialogs and allowing them to be accepted or rejected, supporting whatever the SDK allows
- frontend/backend comms should be handled by the backend using ThingDelta to send a stream of events and then waiting on `watcher.receive` when it is ready for user inputs.
    - multiple things for multiple tasks, e.g. there is a thing that just sends interrupts, which is driven by a background task that just loops, receives a trigger, and advances
    - generally we are free to have multiple things that are attached to watcher.receive()
    - the frontend should then appropriately render them
    - in the case of the interrupt, since it is a different async task, it can be a different "thing" that just has a button in it, and can be rendered as a child by the main one
    - backend needs to detect ctrl-C in the terminal and shut itself down cleanly; use anyio signal handlers for this
    - the types of the payloads sent for trigger requests should be well-documented, since we may want to send them directly
- each tsx renderer should be in a single file that is then typechecked with deno so that it can be checked in a self-contained way

Generally it should feel like a frontend for claude code except with all rendering and interaction being driven by thingwatcher.
