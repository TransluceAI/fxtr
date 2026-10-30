---
name: delayed-followup
description: Schedule a delayed follow-up note that arrives as a channel event after a fixed number of seconds. Use when the user asks you to remind yourself of something after a delay, set a timer, demonstrate the scheduler channel, or otherwise do something in N seconds/minutes from now.
---

# Delayed follow-up

The bundled `scheduler` MCP exposes a `schedule_message` tool. Calling it queues a string to be delivered back to you after a delay through the `claude/channel` mechanism. The delivered message arrives in your next turn as:

```
<channel source="scheduler" delay_seconds="10">your message here</channel>
```

When such a `<channel>` block appears, treat it as a self-directed follow-up: continue whatever task the message describes.

## How to use

Call `schedule_message` with:

- `delay_seconds` (float): how long to wait before delivery
- `message` (string): the text you'll receive back

The tool returns immediately — the delivery happens asynchronously inside the MCP server.

## Example

User: "remind me in 10 seconds to check the build status"

1. Call `schedule_message(delay_seconds=10, message="check the build status with the user")`.
2. Acknowledge to the user that you've queued the reminder.
3. ~10s later a `<channel source="scheduler">` event arrives — react by checking in with the user about the build.
