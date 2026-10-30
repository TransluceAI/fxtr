# Phase 2 Step 5: Backlinks

Add backlinks display to DetailsPanel and ThingViewPanel, showing other things that point to the current thing at the current tick.

## Overview

**Backlinks** are the reverse of the existing "links" field - they show which things point TO this thing, rather than which things this thing points to. They appear at the bottom of the details view, outside custom renderers, and are formatted similarly to forward links but with reversed direction.

### Display Format

Forward link: `linkType [linkKey] → #shortId (thingType)`
Backlink: `#shortId (thingType) → linkType [linkKey]`

### Behavior
- Clicking a backlink navigates to the source thing (same as forward links)
- Right-click shows "Open in panel" context menu (same as forward links)
- No limit on display count - just scrolls if many
- Section is always visible (not collapsible)
- Not accessible to custom renderers

## Design Decision: Unified Bidirectional Link

Rather than creating a separate `Backlink` type, we extend the existing `Link` type to be bidirectional by adding a `fromThing` field:

```graphql
type Link {
  linkType: String!
  linkKey: String!
  linkKeyJSON: JSON
  fromThing: Thing!  # NEW - the source thing
  toThing: Thing!    # existing - the target thing
}
```

This means:
- `thing.links` returns outgoing links → frontend queries `toThing`
- `thing.backlinks` returns incoming links → frontend queries `fromThing`
- Both return the same `Link` type, just viewed from different perspectives
- GraphQL only resolves requested fields, so no performance cost

Benefits:
- Single type = less schema/resolver/TypeScript duplication
- A link is conceptually the same object viewed from either end
- More flexible for future queries

## Implementation

### 1. Backend: Database Layer

Add a new query method to `db.py` for fetching backlinks:

```python
async def get_backlinks_for_thing(
    self, conn: AsyncConnection, tick: int, to_key_bytes: bytes
) -> list[Row]:
    """Get all links pointing TO a specific thing at a tick."""
    query = select(links).where(
        links.c.to_thing_key == to_key_bytes,
        links.c.valid_from <= tick,
        (links.c.valid_until.is_(None)) | (links.c.valid_until > tick),
    )
    return (await conn.execute(query)).fetchall()
```

Also add an index on `to_thing_key` for efficient backlink queries:

```python
Index("idx_links_to_key", links.c.to_thing_key)
```

### 2. Backend: GraphQL Schema

Update `Link` type to add `fromThing`, and add `backlinks` field to `Thing` in `schema.graphql`:

```graphql
type Link {
  linkType: String!
  linkKey: String!        # Base64-encoded CBOR link key
  linkKeyJSON: JSON       # Decoded link key as JSON, null if not JSON-compatible
  fromThing: Thing!       # The source thing (NEW)
  toThing: Thing!         # The target thing (existing)
}

type Thing {
  # ... existing fields ...
  links: [Link!]!
  backlinks: [Link!]!     # NEW - links pointing TO this thing
}
```

### 3. Backend: GraphQL Resolvers

Update existing `resolve_thing_links` to include `fromThing` data, and add new resolver for `backlinks` in `server.py`:

```python
@thing_type.field("links")
async def resolve_thing_links(thing, info) -> list[dict]:
    watcher = thing["_watcher"]
    key_bytes = thing["_key_bytes"]
    tick = thing["_tick"]

    async with watcher._db.begin() as conn:
        links = await watcher._db.get_links_for_thing(conn, tick, key_bytes)
        return [
            {
                "linkType": lnk.link_type,
                "linkKey": encode_key(lnk.link_key),
                "_link_key_bytes": lnk.link_key,
                "_from_thing_key_bytes": lnk.from_thing_key,  # NEW
                "_to_thing_key_bytes": lnk.to_thing_key,
                "_tick": tick,
                "_watcher": watcher,
            }
            for lnk in links
        ]

@thing_type.field("backlinks")
async def resolve_thing_backlinks(thing, info) -> list[dict]:
    watcher = thing["_watcher"]
    key_bytes = thing["_key_bytes"]
    tick = thing["_tick"]

    async with watcher._db.begin() as conn:
        backlinks = await watcher._db.get_backlinks_for_thing(conn, tick, key_bytes)
        return [
            {
                "linkType": lnk.link_type,
                "linkKey": encode_key(lnk.link_key),
                "_link_key_bytes": lnk.link_key,
                "_from_thing_key_bytes": lnk.from_thing_key,
                "_to_thing_key_bytes": lnk.to_thing_key,
                "_tick": tick,
                "_watcher": watcher,
            }
            for lnk in backlinks
        ]

@link_type.field("fromThing")
async def resolve_link_from_thing(link, info) -> dict:
    """Resolve the source thing for this link."""
    watcher = link["_watcher"]
    from_key_bytes = link["_from_thing_key_bytes"]
    tick = link["_tick"]

    async with watcher._db.begin() as conn:
        thing = await watcher._db.get_thing_data(conn, tick, from_key_bytes)
        short_id = await watcher._db.get_short_id(conn, from_key_bytes) or 0
        latest_tick = await watcher._db.get_latest_tick(conn)

        if thing is None:
            return {
                "shortId": short_id,
                "key": encode_key(from_key_bytes),
                "thingType": "unknown",
                "triggerable": False,
                "_key_bytes": from_key_bytes,
                "_tick": tick,
                "_watcher": watcher,
            }

        triggerable = False
        if tick == latest_tick:
            triggerable_things = watcher.get_triggerable_things()
            triggerable = from_key_bytes in {k for k, _ in triggerable_things}

        return {
            "shortId": short_id,
            "key": encode_key(thing.thing_key),
            "thingType": thing.thing_type,
            "triggerable": triggerable,
            "_key_bytes": from_key_bytes,
            "_tick": tick,
            "_watcher": watcher,
        }
```

### 4. Frontend: TypeScript Types

Update `Link` interface in `types/index.ts` to add `fromThing`:

```typescript
export interface Link {
  linkType: string;
  linkKey: string;
  linkKeyJSON: unknown | null;
  fromThing: {
    shortId: number;
    key: string;
    keyJSON: unknown | null;
    thingType: string;
  };
  toThing: {
    shortId: number;
    key: string;
    keyJSON: unknown | null;
    thingType: string;
  };
}
```

Add `backlinks` to the `Thing` interface:

```typescript
export interface Thing {
  // ... existing fields ...
  links: Link[];
  backlinks?: Link[];  // Optional since not all queries fetch it
}
```

### 5. Frontend: GraphQL Queries

Update `GET_THING_DETAILS` in `graphql/queries.ts` to include backlinks:

```graphql
query GetThingDetails($shortId: Int!, $tick: Int!) {
  thing(shortId: $shortId, atTick: $tick) {
    # ... existing fields ...
    links {
      linkType
      linkKey
      linkKeyJSON
      toThing {
        shortId
        key
        keyJSON
        thingType
      }
    }
    backlinks {
      linkType
      linkKey
      linkKeyJSON
      fromThing {
        shortId
        key
        keyJSON
        thingType
      }
    }
  }
}
```

Note: For forward links we query `toThing`, for backlinks we query `fromThing`.

### 6. Frontend: DetailsPanel Component

Add backlinks section at the bottom of `DetailsPanel.tsx`, after the links section (in both the custom renderer fallback and the default view):

```tsx
{/* Backlinks section - always shown, after links */}
{thing.backlinks && thing.backlinks.length > 0 && (
  <div className={styles.section}>
    <h4 className={styles.sectionTitle}>Backlinks ({thing.backlinks.length})</h4>
    <div className={styles.backlinks}>
      {thing.backlinks.map((link: Link, i: number) => (
        <div
          key={i}
          className={styles.backlink}
          onClick={() => onSelectThing(link.fromThing.shortId)}
          onContextMenu={(e) => {
            if (onLinkContextMenu) {
              e.preventDefault();
              onLinkContextMenu({ clientX: e.clientX, clientY: e.clientY }, link.fromThing.shortId);
            }
          }}
        >
          <span className={styles.backlinkSource}>
            #{link.fromThing.shortId}
            <span className={styles.backlinkSourceType}> ({link.fromThing.thingType})</span>
          </span>
          <span className={styles.linkArrow}>→</span>
          <span className={styles.linkType}>{link.linkType}</span>
          <span className={styles.linkKey} title={link.linkKey}>
            [{link.linkKeyJSON !== null && link.linkKeyJSON !== undefined
              ? JSON.stringify(link.linkKeyJSON)
              : link.linkKey.length > 20 ? `${link.linkKey.slice(0, 20)}...` : link.linkKey}]
          </span>
        </div>
      ))}
    </div>
  </div>
)}
```

Key points:
- Section appears after links (and after custom renderer content when applicable)
- Uses existing styling patterns (linkType, linkKey, etc.)
- Supports click to navigate and right-click for context menu
- Shows "Backlinks (N)" as section header

### 7. Frontend: CSS

Add styles for backlinks in `DetailsPanel.module.css`:

```css
.backlinks {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.backlink {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  background: #2a2a2a;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}

.backlink:hover {
  background: #3a3a3a;
}

.backlinkSource {
  color: #88c0ff;
}

.backlinkSourceType {
  color: #888;
}
```

Reuses existing `.linkArrow`, `.linkType`, `.linkKey` styles.

## Implementation Steps

- [x] 1. Add `idx_links_to_key` index to `db.py`
- [x] 2. Add `get_backlinks_for_thing` method to `Database` class
- [x] 3. Add `fromThing` field to `Link` type in `schema.graphql`
- [x] 4. Add `backlinks` field to `Thing` type in `schema.graphql`
- [x] 5. Update `resolve_thing_links` to include `_from_thing_key_bytes` in `server.py`
- [x] 6. Add `resolve_link_from_thing` resolver in `server.py`
- [x] 7. Add `resolve_thing_backlinks` resolver in `server.py`
- [x] 8. Update `Link` interface in `types/index.ts` to add `fromThing`
- [x] 9. Add `backlinks` to `Thing` interface in `types/index.ts`
- [x] 10. Update `GET_THING_DETAILS` query to include backlinks
- [x] 11. Add backlinks section to `DetailsPanel.tsx` (default view)
- [x] 12. Add backlinks section to `DetailsPanel.tsx` (custom renderer fallback)
- [x] 13. Add CSS styles for backlinks
- [ ] 14. Test: verify backlinks appear correctly
- [ ] 15. Test: verify click navigation works
- [ ] 16. Test: verify right-click context menu works
- [x] 17. Update CODE_OVERVIEW.md

## Testing Notes

- Use demo_server.py or create a test scenario with bidirectional links
- Verify backlinks appear for things that are targeted by links from other things
- Verify backlinks update correctly when scrubbing through ticks
- Verify the context menu works for opening backlink sources in new panels
