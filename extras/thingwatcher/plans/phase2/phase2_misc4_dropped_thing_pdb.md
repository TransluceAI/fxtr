# Debugging: Beta Widget Disappeared from View

## Issue Summary
- Running `examples/demo_embed.py` with in-memory database
- Triggered beta widget
- Beta disappeared from the view
- Links to beta still exist (orphaned links)
- No errors visible

## Hypotheses

1. **Receive loop exited** - The `widget_receive_loop` for beta crashed or exited after the trigger
2. **Snapshotter didn't include beta** - Widget wasn't in `_tracked` during a tick
3. **Disappearance detection fired incorrectly** - Something caused beta's key to not be in `seen_keys` during a tick
4. **Database invalidation** - `valid_until` was set on beta's thing_state row

## Key Data Structures to Inspect

### Watcher In-Memory State
- `watcher._current_things` - maps key_bytes -> CurrentThingInfo for things the watcher believes exist
- `watcher._tracked` - maps obj_id -> TrackedObject for registered objects
- `watcher._paused_anchors` - maps anchor_id -> (anchor, PauseInfo) for objects waiting in receive()
- `watcher._triggerable_at_tick` - maps key_bytes -> list of (anchor_id, PauseInfo) for triggerable things
- `watcher._roots` - maps root_name -> (anchor, typename)
- `watcher._current_roots` - maps root_name -> CurrentRootInfo

### Ownership Tracking (for StatefulOwningSnapshotter)
- `watcher._owned_things` - maps obj_id -> set of key_bytes it owns
- `watcher._thing_owners` - maps key_bytes -> obj_id
- `watcher._stateless_keys` - set of key_bytes from stateless sources

---

## PDB Commands to Run

### Step 1: Get the Watcher Object

```python
# The watcher is likely accessible via context or as a local
# In demo_embed.py, it's created and used in main()
# From line 296 (anyio.run), you may need to inspect the running tasks

# Find all running asyncio tasks
import asyncio
tasks = asyncio.all_tasks()
for t in tasks:
    print(t.get_name(), t.get_coro())
```

If you have the event loop:
```python
# If you have `loop` variable
loop = asyncio.get_event_loop()
tasks = asyncio.all_tasks(loop)
```

### Step 2: Find the Watcher Instance

```python
# Option A: From context var
from thingwatcher.context import _current_watcher
watcher = _current_watcher.get()
print(watcher)

# Option B: Look at task frames/locals
# If debugging inside main(), watcher should be local
```

### Step 3: Inspect Tracked Objects

```python
# See what's being tracked
print("Tracked objects:")
for obj_id, tracked in watcher._tracked.items():
    obj = tracked.obj_ref()
    print(f"  id={obj_id} type={tracked.typename} alive={obj is not None}")
    if obj is not None:
        print(f"    obj={obj}")
```

### Step 4: Check if Beta is in Current Things

```python
import cbor2

# Compute beta's key
beta_key = {"widget": "beta"}
beta_key_bytes = cbor2.dumps(beta_key, canonical=True)

# Check if in _current_things
if beta_key_bytes in watcher._current_things:
    info = watcher._current_things[beta_key_bytes]
    print(f"Beta found in current_things: thingstate_id={info.thingstate_id}, type={info.thing_type}")
else:
    print("Beta NOT in _current_things - it was invalidated!")
```

### Step 5: Check Paused Anchors (Receive Loops)

```python
print("Paused anchors (things in receive()):")
for anchor_id, (anchor, pause_info) in watcher._paused_anchors.items():
    print(f"  anchor_id={anchor_id}")
    print(f"    typename={pause_info.typename}")
    print(f"    is_paused={pause_info.is_paused}")
    print(f"    anchor={anchor}")
```

### Step 6: Check Triggerable Map

```python
print("Triggerable at current tick:")
for key_bytes, pause_list in watcher._triggerable_at_tick.items():
    key = cbor2.loads(key_bytes)
    live = [pi for _, pi in pause_list if pi.is_paused]
    print(f"  key={key}: {len(live)} live pauses out of {len(pause_list)}")
```

### Step 7: Inspect Roots

```python
print("Roots:")
for name, (anchor, typename) in watcher._roots.items():
    print(f"  {name}: typename={typename}, anchor={anchor}")

print("Current roots (DB tracking):")
for name, info in watcher._current_roots.items():
    key = cbor2.loads(info.thing_key_bytes)
    print(f"  {name}: db_id={info.root_db_id}, key={key}")
```

### Step 8: Check the Async Tasks for Widget Receive Loops

```python
# Look for running receive loops
import asyncio
for task in asyncio.all_tasks():
    coro = task.get_coro()
    name = task.get_name()
    print(f"Task: {name}")
    print(f"  coro: {coro}")
    print(f"  done: {task.done()}")
    if task.done() and task.exception():
        print(f"  EXCEPTION: {task.exception()}")
```

### Step 9: Query Database Directly (if you can get the DB engine)

```python
# Get the DB engine URI (useful for external inspection)
print(f"DB engine: {watcher._db._engine}")
print(f"DB path: {watcher._db._db_path}")

# If in-memory, you can run queries via the engine
# But this requires async - might be tricky in pdb
```

### Step 10: Manual Database Query (async)

If you can run async code in the debugger:
```python
# Run this in an async context or use asyncio.run()
async def debug_query():
    async with watcher._db.begin() as conn:
        # Get latest tick
        latest = await watcher._db.get_latest_tick(conn)
        print(f"Latest tick: {latest}")

        # Get all things at latest tick
        things = await watcher._db.get_things_at_tick(conn, latest)
        print(f"\nThings at tick {latest}:")
        for t in things:
            key = cbor2.loads(t.thing_key)
            print(f"  id={t.id} key={key} type={t.thing_type}")
            print(f"    valid_from={t.valid_from} valid_until={t.valid_until}")

        # Get links at latest tick
        links = await watcher._db.get_links_at_tick(conn, latest)
        print(f"\nLinks at tick {latest}:")
        for l in links:
            from_key = cbor2.loads(l.from_thing_key)
            to_key = cbor2.loads(l.to_thing_key)
            print(f"  {from_key} --[{l.link_type}]--> {to_key}")
            print(f"    valid_from={l.valid_from} valid_until={l.valid_until}")

# If you can run async:
import asyncio
asyncio.get_event_loop().run_until_complete(debug_query())
```

---

## Experiment Log

### Experiment 1: [timestamp]
**Goal:** Determine watcher instance and check _current_things

```
watcher
<thingwatcher.watcher.Watcher object at 0x106e4fcb0>
watcher._current_things
{b'\xa1fwidgetealpha': CurrentThingInfo(thingstate_id=1, thing_type='Widget', data_bytes=b'\xa3etitlelAlpha ...hmessages\x80lmessageCount\x00', links={}), b'\xa1fwidgetdbeta': CurrentThingInfo(thingstate_id=3, thing_type='Widget', data_bytes=b'\xa3etitlekBeta W...es\x81fbaaazzlmessageCount\x01', links={})}
watcher._tracked
{4418758240: TrackedObject(typename='__main__.Widget', obj_ref=<weakref at 0x1076439c0; to 'Widget' at 0x10760e660>), 4418072848: TrackedObject(typename='__main__.Widget', obj_ref=<weakref at 0x1076612b0; to 'Widget' at 0x107567110>)}
watcher._paused_anchors
{4418758240: (Widget(id='alpha', title='Alpha Widget', messages=[]), PauseInfo(event=<anyio._backends._asyncio.Event object at 0x107605940>, payload_holde...s_paused=True, typename='__main__.Widget')), 4418072848: (Widget(id='beta', title='Beta Widget', messages=['baaazz']), PauseInfo(event=<anyio._backends._asyncio.Event object at 0x107634050>, payload_holde...s_paused=True, typename='__main__.Widget'))}
watcher._triggerable_at_tick
{b'\xa1fwidgetealpha': [(...)], b'\xa1fwidgetdbeta': [(...)]}
watcher._roots
{'alpha': (Widget(id='alpha', title='Alpha Widget', messages=[]), '__main__.Widget'), 'beta': (Widget(id='beta', title='Beta Widget', messages=['baaazz']), '__main__.Widget')}
watcher._current_roots
{'alpha': CurrentRootInfo(root_db_id=1, thing_key_bytes=b'\xa1fwidgetealpha'), 'beta': CurrentRootInfo(root_db_id=2, thing_key_bytes=b'\xa1fwidgetdbeta')}
watcher._owned_things
{}
watcher._thing_owners
{}
watcher._stateless_keys
{b'\xa1fwidgetdbeta', b'\xa1fwidgetealpha'}
watcher._db._db_path
':memory:'
watcher._db._engine.sync_engine
Engine(sqlite+aiosqlite:///:memory:)
```

**Analysis:**

Watcher in-memory state is **completely healthy** - beta exists everywhere it should:
- `_current_things`: beta has `thingstate_id=3` ✓
- `_tracked`: both widgets have live weakrefs ✓
- `_paused_anchors`: beta is `is_paused=True` with message 'baaazz' ✓
- `_triggerable_at_tick`: beta is triggerable ✓
- `_roots` & `_current_roots`: beta root exists ✓

**The bug is NOT in watcher tracking.** Problem must be:
1. Database has beta invalidated despite `_current_things` saying otherwise
2. GraphQL layer not returning beta
3. Frontend looking at wrong tick

The `sync_engine` is tricky - aiosqlite's sync_engine isn't actually connected to the same in-memory DB (each connection to `:memory:` creates a new DB unless using file URI with cache=shared).

**Next step**: Query DB via async. Can you run this in pdb?
```python
import asyncio
async def q():
    async with watcher._db._engine.connect() as conn:
        r = await conn.execute(__import__('sqlalchemy').text("SELECT id, thing_type, valid_from, valid_until FROM thing_states"))
        for row in r.fetchall(): print(row)
asyncio.get_event_loop().create_task(q())
```
Then step forward to let it run, or try `asyncio.get_event_loop().run_until_complete(q())`

---

### Experiment 2: DB query

```
(1, 'Widget', 1, None)
(2, 'Widget', 1, 4)
```
Frontend shows LIVE/6

**Analysis:**

MAJOR DISCREPANCY FOUND:
- `_current_things` says beta has `thingstate_id=3`
- Database only has rows 1 and 2!
- Row 2 (beta's original state) was invalidated at tick 4
- Row 3 **doesn't exist** in the database

This means beta's updated state after the trigger was **never written to the DB**, but `_current_things` was updated as if it was.

The watcher thinks it wrote thingstate_id=3, but that row never made it to the DB.

**Next: Check ticks table and look for write errors**
```python
async def q2():
    async with watcher._db._engine.connect() as conn:
        r = await conn.execute(__import__('sqlalchemy').text("SELECT tick, level, type, seq FROM ticks"))
        for row in r.fetchall(): print(row)
asyncio.get_event_loop().create_task(q2())
```

Also check `watcher._tick_counter` vs what's in DB - are there ticks that didn't get written?

---

### Experiment 3: Ticks table and writer state

`watcher._tick_counter` = 6

Ticks in DB:
```
(1, 'info', 'api_tick', 1)
(2, 'pause', 'api_tick', 2)
(3, 'pause', 'api_tick', 3)
(4, 'info', 'api_tick', 4)
(5, 'pause', 'api_tick', 5)
(6, 'debug', 'remote_client', 6)
```

Writer task is waiting at line 1305 with `job` = tick 7 (next snapshot).

**Analysis:**

All 6 ticks were written to the ticks table. The writer processed ticks 1-6 and is waiting for tick 7. So the writer didn't crash.

But thing_states only has 2 rows, with row 2 invalidated at tick 4. Row 3 (beta's updated state) was never created.

**Timeline reconstruction:**
1. Tick 1: Create alpha (row 1), beta (row 2) ✓
2. Tick 2-3: pause ticks, no data changes ✓
3. Tick 4: beta triggered, message added → row 2 invalidated at tick 4, but **row 3 never written**
4. Tick 5-6: no data changes detected (watcher thinks row 3 exists via `_current_things`)

**The bug**: During tick 4 processing:
1. `invalidate_thing_state(row 2, tick=4)` succeeded and was committed
2. `write_thing_state()` either failed OR returned lastrowid=3 but transaction rolled back
3. `_current_things` was updated with thingstate_id=3 **inside** the transaction context
4. If transaction rolled back, the DB changes were undone but `_current_things` kept the stale state

**Hypothesis: `_current_things` is mutated before transaction commit**

Looking at watcher.py `_process_thing_state`:
```python
# Inside async with self._db.begin() as conn:
await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick, seq)
new_id = await self._db.write_thing_state(...)  # Returns lastrowid immediately
self._current_things[key_bytes] = CurrentThingInfo(thingstate_id=new_id, ...)
# Transaction commits AFTER this
```

If something fails after `write_thing_state` but before the context manager exits, the transaction rolls back but `_current_things` is already mutated!

**BUT** - wait, that doesn't fully explain it. If the transaction for tick 4 rolled back, the tick record itself (row 4 in ticks table) shouldn't exist either. But it does.

**Alternative hypothesis**: The invalidation and tick write committed, but `write_thing_state` for beta silently failed?

**Next: Check what happens in the transaction after thing_states are written**

---

### Experiment 4: Full thing_states dump and snapshotter check

Run these:

```python
import asyncio

# 1. Full thing_states dump - see all columns
async def q1():
    async with watcher._db._engine.connect() as conn:
        r = await conn.execute(__import__('sqlalchemy').text("SELECT * FROM thing_states ORDER BY id"))
        print("Columns:", r.keys())
        for row in r.fetchall(): print(row)

asyncio.get_event_loop().create_task(q1())
```

```python
# 2. Verify snapshotter is working for both widgets
for obj_id, tracked in watcher._tracked.items():
    obj = tracked.obj_ref()
    if obj:
        snap = watcher.get_snapshotter(tracked.typename)
        result = snap.snapshot(obj)
        print(f"obj={obj.id}, key={result.key}, data={result.thing_data}")
```

```python
# 3. Check roots table too
async def q2():
    async with watcher._db._engine.connect() as conn:
        r = await conn.execute(__import__('sqlalchemy').text("SELECT * FROM roots ORDER BY id"))
        print("Roots columns:", r.keys())
        for row in r.fetchall(): print(row)

asyncio.get_event_loop().create_task(q2())
```

**Results:**
python
```
obj=alpha, key={'widget': 'alpha'}, data={'title': 'Alpha Widget', 'messages': [], 'messageCount': 0}
obj=beta, key={'widget': 'beta'}, data={'title': 'Beta Widget', 'messages': ['baaazz'], 'messageCount': 1}
```

q1 and q2
```
Columns: RMKeyView(['id', 'valid_from', 'valid_until', 'valid_from_seq', 'valid_to_seq', 'thing_key', 'thing_type', 'data', 'data_extends'])
(1, 1, None, 1, None, b'\xa1fwidgetealpha', 'Widget', b'\xa3etitlelAlpha Widgethmessages\x80lmessageCount\x00', None)
(2, 1, 4, 1, 4, b'\xa1fwidgetdbeta', 'Widget', b'\xa3etitlekBeta Widgethmessages\x80lmessageCount\x00', None)
Roots columns: RMKeyView(['id', 'root_name', 'thing_key', 'valid_from', 'valid_until', 'valid_from_seq', 'valid_to_seq'])
(1, 'alpha', b'\xa1fwidgetealpha', 1, None, 1, None)
(2, 'beta', b'\xa1fwidgetdbeta', 1, None, 1, None)
```

**Analysis:**

Confirmed:
- Row 2 has OLD data: `messages\x80` = empty array, `messageCount\x00` = 0
- Row 2 was invalidated at tick 4 (valid_until=4, valid_to_seq=4)
- **Row 3 does not exist anywhere**
- Snapshotter currently returns correct data for beta (messages=['baaazz'])
- Roots table looks fine - both roots still valid

The mystery: `_current_things[beta].thingstate_id = 3`, but row 3 doesn't exist. This means `write_thing_state` returned 3, but the row wasn't persisted.

**New hypothesis: Beta disappeared at tick 4, then reappeared at tick 5**

What if at tick 4, beta wasn't in the snapshot at all?
1. Tick 4 processes, beta not in `snapshot.states`
2. `seen_keys` doesn't include beta
3. `_invalidate_thing(beta)` is called → row 2 invalidated, `_current_things.pop(beta)`
4. Transaction commits (tick 4 exists, row 2 invalidated)
5. Tick 5: beta IS in snapshot, treated as NEW thing
6. `write_thing_state` returns 3, `_current_things[beta] = thingstate_id=3`
7. But something fails AFTER this, transaction rolls back
8. Tick 5 record exists but row 3 doesn't?

Wait, but tick 5 exists in ticks table... If transaction rolled back, tick 5 wouldn't exist.

**Alternative: SQLite autoincrement weirdness**

What if something caused the id sequence to skip? Let me check if any row with id >= 3 exists.

---

### Experiment 5: Check for any rows with id >= 3, and check trigger_records/links

```python
import asyncio

async def q():
    async with watcher._db._engine.connect() as conn:
        # Check max id in thing_states
        r = await conn.execute(__import__('sqlalchemy').text("SELECT MAX(id) FROM thing_states"))
        print("Max thing_states id:", r.fetchone())

        # Check trigger_records
        r = await conn.execute(__import__('sqlalchemy').text("SELECT * FROM trigger_records"))
        print("Trigger records:")
        for row in r.fetchall(): print(" ", row)

        # Check links table
        r = await conn.execute(__import__('sqlalchemy').text("SELECT * FROM links"))
        print("Links:")
        for row in r.fetchall(): print(" ", row)

asyncio.get_event_loop().create_task(q())
```

Also check what's actually in `_current_things` for beta:
```python
import cbor2
beta_key_bytes = cbor2.dumps({"widget": "beta"}, canonical=True)
info = watcher._current_things[beta_key_bytes]
print(f"thingstate_id: {info.thingstate_id}")
print(f"thing_type: {info.thing_type}")
print(f"data_bytes (decoded): {cbor2.loads(info.data_bytes)}")
```

**Results:**
```
thingstate_id: 3
thing_type: Widget
data_bytes (decoded): {'title': 'Beta Widget', 'messages': ['baaazz'], 'messageCount': 1}

Max thing_states id: (2,)
Trigger records:
  (1, b'\xa1fwidgetdbeta', 2, 3, b'\xa1gmessagefbaaazz')
Links: (empty)
```

**Analysis:**

SMOKING GUN:
- `_current_things[beta].data_bytes` has the NEW data ('baaazz')
- `_current_things[beta].thingstate_id = 3`
- But max thing_states id is 2 - **row 3 does not exist**

This confirms: `write_thing_state` was called, returned `lastrowid=3`, and `_current_things` was updated. But the row never made it to the database.

Trigger record confirms timeline:
- `thingstate_id=2, trigger_tick=3` - trigger happened at tick 3, beta had row 2

**The impossible situation:**
- Tick 4 exists in DB → transaction committed
- Row 2 invalidated at tick 4 → UPDATE committed
- Row 3 doesn't exist → INSERT somehow didn't commit

These should ALL be in the same transaction. If one commits, all should commit.

**CRITICAL HYPOTHESIS: Two different database connections**

The in-memory DB uses `file:memdb_{uuid}?cache=shared`. What if pdb queries are hitting a different connection that doesn't see uncommitted writes?

---

### Experiment 6: Verify we're querying the right database

```python
# Check the engine URL
print("Engine URL:", str(watcher._db._engine.url))

# Check if there are any pending writes in the queue
print("Write stream stats:", watcher._write_stream.statistics())
print("Receive stream stats:", watcher._receive_stream.statistics())
```

```python
# Try inserting a test row and see if it persists
import asyncio
async def test_insert():
    async with watcher._db._engine.connect() as conn:
        async with conn.begin():
            await conn.execute(__import__('sqlalchemy').text(
                "INSERT INTO thing_states (valid_from, valid_from_seq, thing_key, thing_type, data) VALUES (999, 999, x'00', 'TEST', x'00')"
            ))
        # Query it back
        r = await conn.execute(__import__('sqlalchemy').text("SELECT id, thing_type FROM thing_states WHERE thing_type='TEST'"))
        print("Test row:", r.fetchone())

asyncio.get_event_loop().create_task(test_insert())
```

**Results:**
```
Engine URL: sqlite+aiosqlite:///:memory:
Write stream stats: MemoryObjectStreamStatistics(current_buffer_used=0, max_buffer_size=inf, open_send_streams=1, open_receive_streams=1, tasks_waiting_send=0, tasks_waiting_receive=1)
Receive stream stats: MemoryObjectStreamStatistics(current_buffer_used=0, max_buffer_size=inf, open_send_streams=1, open_receive_streams=1, tasks_waiting_send=0, tasks_waiting_receive=1)


Test row: (3, 'TEST')
```

then I tried running the same async command again and got
```
Test row: (3, 'TEST')
```
but I guess that's expected because you are just fetching one.

then for good measure

```python
import asyncio

# 1. Full thing_states dump - see all columns
async def q1():
    async with watcher._db._engine.connect() as conn:
        r = await conn.execute(__import__('sqlalchemy').text("SELECT * FROM thing_states ORDER BY id"))
        print("Columns:", r.keys())
        for row in r.fetchall(): print(row)

asyncio.get_event_loop().create_task(q1())
```
and I got
```
Columns: RMKeyView(['id', 'valid_from', 'valid_until', 'valid_from_seq', 'valid_to_seq', 'thing_key', 'thing_type', 'data', 'data_extends'])
(1, 1, None, 1, None, b'\xa1fwidgetealpha', 'Widget', b'\xa3etitlelAlpha Widgethmessages\x80lmessageCount\x00', None)
(2, 1, 4, 1, 4, b'\xa1fwidgetdbeta', 'Widget', b'\xa3etitlekBeta Widgethmessages\x80lmessageCount\x00', None)
(3, 999, None, 999, None, b'\x00', 'TEST', b'\x00', None)
(4, 999, None, 999, None, b'\x00', 'TEST', b'\x00', None)
```

**Analysis:**

Test INSERTs work! Rows 3 and 4 were created. This proves:
- Database connection is correct
- INSERTs persist properly

KEY INSIGHT: The test INSERT got id=3. If beta's write had succeeded at tick 4, it would have gotten id=3. So **beta's write_thing_state was never called** at tick 4.

But `_current_things[beta].thingstate_id = 3`... this must have been set at a later tick when we manually inserted TEST rows (contaminating the autoincrement sequence).

Actually wait - we saw `thingstate_id=3` BEFORE we did the test inserts. So where did id=3 come from?

**Revised theory:** At some tick, `write_thing_state` was called for beta and the autoincrement would have returned 3, but the transaction was rolled back. The autoincrement counter still incremented (SQLite behavior), so our test INSERT got 3 too.

But `_current_things` was updated inside that transaction before rollback!

---

### Experiment 7: Simulate snapshot and check writer task

```python
# Manually create a snapshot to see what would be captured now
snapshot = watcher._take_snapshot("debug", "test snapshot")
print(f"Tick: {snapshot.tick_number}")
print(f"States ({len(snapshot.states)}):")
for s in snapshot.states:
    print(f"  key={s.key}, type={s.thing_type}")
    if hasattr(s, 'thing_data'):
        print(f"    data keys={list(s.thing_data.keys())}")
```

```python
# Check the writer task for exceptions
import asyncio
for t in asyncio.all_tasks():
    if 'db_writer' in t.get_name():
        print(f"Writer task: {t}")
        print(f"  done: {t.done()}")
        if t.done() and not t.cancelled():
            try:
                print(f"  result: {t.result()}")
            except Exception as e:
                print(f"  EXCEPTION: {e}")
```

**Results:**
```
Tick: 14
States (2):
  key={'widget': 'alpha'}, type=Widget
    data keys=['title', 'messages', 'messageCount']
  key={'widget': 'beta'}, type=Widget
    data keys=['title', 'messages', 'messageCount']

Writer task: <Task pending name='thingwatcher.watcher.Watcher._db_writer' coro=<Watcher._db_writer() running at /Users/daniel/fxtr/extras/thingwatcher/thingwatcher/watcher.py:1305> wait_for=<Future pending cb=[Task.task_wakeup()]> cb=[TaskGroup._spawn.<locals>.task_done() at /Users/daniel/fxtr/extras/thingwatcher/.venv/lib/python3.13/site-packages/anyio/_backends/_asyncio.py:805]>
  done: False
```

**Analysis:**

Writer is healthy - not cancelled, no exception. The "impossible" has happened: partial transaction commit in SQLite.

---

### Experiment 8: Verify `_current_things` matches DB

```python
import asyncio
import cbor2

async def verify():
    async with watcher._db._engine.connect() as conn:
        for key_bytes, info in watcher._current_things.items():
            key = cbor2.loads(key_bytes)
            r = await conn.execute(
                __import__('sqlalchemy').text("SELECT id, thing_type FROM thing_states WHERE id = :id"),
                {"id": info.thingstate_id}
            )
            row = r.fetchone()
            if row:
                print(f"OK {key}: id={info.thingstate_id} exists")
            else:
                print(f"MISMATCH {key}: id={info.thingstate_id} NOT IN DB!")

asyncio.get_event_loop().create_task(verify())
```

**Results:**
```
Pool class: StaticPool
Pool size: N/A
Checked out: N/A

OK {'widget': 'alpha'}: id=1 exists
OK {'widget': 'beta'}: id=3 exists
```
(Note: beta shows OK only because our TEST rows took id=3,4)

---

## Deep Code Analysis

### The In-Memory Mutation Bug (CONFIRMED)

Looking at `_process_thing_state` (lines 1428-1493) and `_invalidate_thing` (lines 1754-1765):

```python
async with self._db.begin() as conn:  # Transaction starts
    ...
    for item in snapshot.states:
        key_bytes = await self._process_thing_state(conn, tick, seq, item)
        # Inside _process_thing_state:
        #   await self._db.invalidate_thing_state(...)  # DB op
        #   new_id = await self._db.write_thing_state(...)  # DB op
        #   self._current_things[key_bytes] = ...  # IN-MEMORY MUTATION BEFORE COMMIT!
    ...
    for key_bytes in ...:
        await self._invalidate_thing(...)
        # Inside _invalidate_thing:
        #   info = self._current_things.pop(key_bytes)  # MUTATES BEFORE DB OP!
        #   await self._db.invalidate_thing_state(...)
# Transaction commits HERE
```

**Bug:** `_current_things` and `_current_roots` are modified INSIDE the transaction BEFORE commit. If anything fails, transaction rolls back but Python's in-memory state keeps the stale changes.

### Why This STILL Doesn't Fully Explain the Observation

For the observed state (tick 4 exists, row 2 invalidated at tick 4, row 3 missing), SQLite would need to partially commit - which is impossible. Transactions are atomic.

### Possible Explanations

1. **aiosqlite/SQLAlchemy + StaticPool edge case**
   - With `:memory:`, SQLAlchemy uses StaticPool (single connection reused)
   - If `trigger_pause` (HTTP handler) and `_db_writer` call `begin()` concurrently on same connection, transaction state could get corrupted
   - aiosqlite runs SQLite in background thread - async scheduling could interleave

2. **The in-memory bug caused cascade**
   - Some tick's transaction rolled back but `_current_things` wasn't reset
   - Later ticks saw "unchanged" data and didn't write new rows
   - But this doesn't explain `valid_until=4` specifically on row 2

3. **Unknown aiosqlite behavior**
   - `result.lastrowid` returned 3 but INSERT didn't actually persist?
   - Would be a serious bug in the library

### Recommended Fix

Fix the in-memory mutation bug by deferring updates until after commit:

```python
async def _process_snapshot(self, snapshot):
    pending_updates = {}  # Collect changes
    pending_deletes = set()

    async with self._db.begin() as conn:
        # All DB operations, store intended updates in pending_*
        ...

    # AFTER commit, apply to in-memory state
    self._current_things.update(pending_updates)
    for k in pending_deletes:
        self._current_things.pop(k, None)
```

### Conclusion

**Root cause unclear.** The in-memory mutation bug is real and should be fixed, but doesn't explain the impossible "partial commit". Most likely an edge case in aiosqlite/SQLAlchemy with StaticPool + `:memory:`. Consider:
1. Using file-based SQLite to avoid StaticPool
2. Fixing the in-memory mutation bug regardless
3. Adding logging to catch future occurrences

---

## Fix Applied

Two fixes were implemented:

### 1. Use `inserted_primary_key` instead of `lastrowid` (db.py)

Changed all INSERT operations to use `result.inserted_primary_key[0]` instead of `result.lastrowid`. The SQLAlchemy docs warn that `lastrowid` behavior is not consistent across backends.

Files changed: `db.py` lines 221-222, 256-258, 451-453, 490-492

### 2. Defer in-memory updates until after transaction commit (watcher.py)

The core bug: `_current_things` and `_current_roots` were being modified INSIDE the transaction, before commit. If the transaction failed, the DB would roll back but Python's in-memory state would keep the stale values.

**Changes:**
- `_process_thing_state` now returns `tuple[bytes, CurrentThingInfo]` instead of just `bytes`, and does NOT modify `_current_things`
- `_process_thing_delta` similarly returns the tuple without modifying state
- `_process_roots` now returns `tuple[dict[str, CurrentRootInfo], set[str]]` (pending updates and deletes) without modifying `_current_roots`
- `_invalidate_thing` no longer calls `pop()` on `_current_things`
- `_process_snapshot` and `_process_streaming_update` now:
  1. Collect all pending updates in temporary dicts/sets during the transaction
  2. Apply updates to `_current_things` and `_current_roots` AFTER the `async with self._db.begin()` block exits (i.e., after commit)

This ensures that if a transaction fails for any reason, the in-memory state remains consistent with the database.

All 156 tests pass.
