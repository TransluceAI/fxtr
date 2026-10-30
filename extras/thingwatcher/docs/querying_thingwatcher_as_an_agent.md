# Querying Thingwatcher as an Agent

Quick reference for AI agents to query a running thingwatcher server.

## Tools

- `graphqurl` - GraphQL client CLI
- `curl` - for REST endpoints (debug triggers)
- `jq` - JSON formatting (optional but recommended)

## Server URL

Default: `http://localhost:8765/graphql/` (port may vary)

**Important:** The trailing slash is required. The server redirects `/graphql` to `/graphql/`.

## Identifiers: shortId, key, and keyJSON

Things have three ways to be identified:
- **shortId**: Simple integer (1, 2, 3...) - easy to type, stable within a trace
- **key**: Base64-encoded CBOR - opaque, always available
- **keyJSON**: JSON representation of the key - human-readable when the key is JSON-compatible

**Prefer shortId for CLI interaction.** Most queries that accept `key` also accept `shortId` and `keyJSON`.

The `keyJSON` field is nullable - it returns `null` if the key contains non-JSON types (bytes, datetime, etc.). In that case, fall back to `shortId` or `key`.

## Essential Queries

```bash
# Current tick (returns a Tick object)
graphqurl http://localhost:8765/graphql/ -q '{ latestTick { index level desc } }'

# All ticks with descriptions
graphqurl http://localhost:8765/graphql/ -q '{ ticks { index level desc } }'

# Things at the latest tick (includes keyJSON for human-readable keys)
graphqurl http://localhost:8765/graphql/ -q '{ latestTick { things { shortId keyJSON thingType data } } }'

# Things at a specific tick
graphqurl http://localhost:8765/graphql/ -q '{ tick(index: 1) { things { shortId keyJSON thingType data } } }'

# What changed between two ticks
graphqurl http://localhost:8765/graphql/ -q '{ tick(index: 2) { changes(since: 1) { kind thing { shortId thingType } } } }'

# Get a specific thing BY SHORTID (preferred)
graphqurl http://localhost:8765/graphql/ -q '{ thing(shortId: 1) { thingType data } }'

# Get a specific thing at a specific tick
graphqurl http://localhost:8765/graphql/ -q '{ thing(shortId: 1, atTick: 1) { thingType dataChunks { data } } }'

# Get thing by key (if you have it)
graphqurl http://localhost:8765/graphql/ -q '{ thing(key: "BASE64_KEY") { thingType data } }'

# Get thing by keyJSON (if you have a JSON key)
graphqurl http://localhost:8765/graphql/ -q '{ thing(keyJSON: {"id": 1}) { thingType data } }'

# History of a thing by shortId
graphqurl http://localhost:8765/graphql/ -q '{ thingHistory(shortId: 1) { dataChunks { data } } }'
```

## Links and Graph Traversal

```bash
# Get things with their links (linkKeyJSON shows link keys in JSON form)
graphqurl http://localhost:8765/graphql/ -q '{ latestTick { things { shortId links { linkType linkKeyJSON toThing { shortId thingType data } } } } }'

# Traverse links to see related things (all in one query!)
graphqurl http://localhost:8765/graphql/ -q '{ thing(shortId: 1) { data links { linkType linkKeyJSON toThing { shortId data } } } }'
```

## Keys Deep Dive

Things are uniquely identified by CBOR keys. You have three options:

1. **shortId** (integer) - Simplest for CLI. Just use `shortId: 1`.
2. **keyJSON** (JSON) - Use when you have a JSON-compatible key like `keyJSON: {"id": 1}` or `keyJSON: ["task", "main"]`
3. **key** (base64 string) - Use the opaque base64 if you need to round-trip exactly

When querying output, `keyJSON` shows what the key actually represents. Example response:
```json
{"shortId": 1, "key": "gmR0YXNrZG1haW4=", "keyJSON": ["task", "main"], "thingType": "Task"}
```

If `keyJSON` is `null`, the key contains non-JSON types - use `shortId` or `key` instead.

## Pause/Trigger

```bash
# See all currently paused/triggerable things
graphqurl http://localhost:8765/graphql/ -q '{ latestTick { things(triggerable: true) { shortId keyJSON thingType data } } }'

# Trigger by shortId (preferred - so simple!)
graphqurl http://localhost:8765/graphql/ -q 'mutation { triggerPause(shortId: 1) { ok error } }'

# Trigger by keyJSON (if you have a JSON key)
graphqurl http://localhost:8765/graphql/ -q 'mutation { triggerPause(keyJSON: {"task": "main"}) { ok error } }'

# Trigger by key (if needed)
graphqurl http://localhost:8765/graphql/ -q 'mutation { triggerPause(key: "BASE64_KEY") { ok error } }'

# Trigger with a simple payload (inline JSON)
graphqurl http://localhost:8765/graphql/ -q 'mutation { triggerPause(shortId: 1, payload: {message: "hello"}) { ok } }'

# Trigger with a complex payload (use heredoc for clean quoting)
graphqurl http://localhost:8765/graphql/ \
  -q 'mutation T($shortId: Int!, $payload: JSON) { triggerPause(shortId: $shortId, payload: $payload) { ok } }' \
  --variablesJSON "$(cat <<'EOF'
{"shortId":1,"payload":{"message":"hello","value":42}}
EOF
)"

# View trigger history (shows thing data at trigger time)
graphqurl http://localhost:8765/graphql/ -q '{ triggerRecords { id triggerTick thing { shortId thingType data } payload } }'
```

## Other Actions

```bash
# Fire a debug trigger (for test synchronization)
curl -X POST http://localhost:8765/debug_trigger/TRIGGER_NAME

# Manually trigger a tick (returns the new Tick)
graphqurl http://localhost:8765/graphql/ -q 'mutation { triggerTick(level: "info", desc: "manual") { index level } }'
```

## Tips

- Pipe to `jq '.'` for formatted output: `graphqurl ... | jq '.'`
- Use `data` field for first chunk's data, `dataChunks` for full chain
- Filter triggerable things: `latestTick { things(triggerable: true) { ... } }`
- Combine multiple queries in one request using GraphQL aliases:
  ```bash
  graphqurl http://localhost:8765/graphql/ -q '{
    tick: latestTick { index }
    triggers: latestTick { things(triggerable: true) { shortId thingType } }
  }'
  ```
