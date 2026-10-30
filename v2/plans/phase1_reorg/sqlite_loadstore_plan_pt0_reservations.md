# Plan: Entity Reservations Table

## Purpose

Add an `entity_reservations` table that allows a session to temporarily "reserve" entities. While reserved, an entity won't be cleaned up even if it's not in any namespace. This protects entities during the window between saving to entity tables and adding to namespace.

## Schema

```sql
CREATE TABLE entity_reservations (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, entity_id)
);
```

The `ON DELETE CASCADE` on session_id means when a session is garbage collected, its reservations are automatically cleaned up.

## Operations

**Add reservation:**
```sql
INSERT INTO entity_reservations (session_id, entity_id, count)
VALUES (?, ?, 1)
ON CONFLICT (session_id, entity_id) DO UPDATE SET count = count + 1
```

**Release reservation:**
```sql
UPDATE entity_reservations
SET count = count - 1
WHERE session_id = ? AND entity_id = ?;

DELETE FROM entity_reservations
WHERE session_id = ? AND entity_id = ? AND count <= 0;
```

(Or combine into a single statement if SQLite supports it.)

## Integration with Garbage Collection

The existing session garbage collection already deletes dead sessions. With `ON DELETE CASCADE`, reservations for dead sessions are automatically removed. No additional cleanup logic needed.

## Integration with Entity Cleanup

When considering whether to delete an entity from entity tables, check that it has no reservations (in addition to checking namespace membership). An entity can be deleted only if:
1. It's not in any namespace's ownership table
2. It has no reservations

This prevents cleanup from racing with in-progress copy operations.

## Implementation Location

Add to `sqlite_arena.py`:
1. Table definition in the schema section (near other table definitions)
