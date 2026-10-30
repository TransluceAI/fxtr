# Immutable Object Stores Demo

This demo showcases the **immutable object stores** feature of thingwatcher.

## What are Immutable Object Stores?

Immutable stores provide lazy access to things that are assumed constant over time (like content-addressed data, git commits, or provenance graphs). Unlike regular snapshot things:

- Things in stores are **not captured during ticks** - they're queried on-demand
- Store lookup happens at **GraphQL query time**
- Snapshot things **override** store things if the same key exists in both
- Stores support **backlinks** for finding what points to a given thing

## Running the Demo

```bash
cd examples/demo_immutable_store
python demo.py
```

Then open http://localhost:8765 in your browser.

## What This Demo Shows

### 1. Binary Tree Store (15 nodes)

A 4-level binary tree where each node links to its children and parent:

```
                    root
                   /    \
                  L      R
                 / \    / \
               LL  LR  RL  RR
              /\ /\ /\ /\
            ... (8 leaf nodes)
```

Each node has:
- Links to left/right children (if not a leaf)
- Link to parent (if not root)
- Backlinks showing what points to it

### 2. Tree Pointers

Active snapshot things that **point into the middle of the tree**:
- `mid_pointer` → points to node "LR"
- `deep_pointer` → points to node "RLL"

Click on a pointer to see its link to the tree, then follow links to explore!

### 3. Override Scenario (Ticks 2-3)

An immutable thing with key `("overridable", "shared_key")` exists in the store. At ticks 2-3, a **snapshot thing with the same key** appears, demonstrating:

- The snapshot **overrides** the store version
- The panel shows "overridden" badge on the store source
- When the snapshot disappears (tick 4+), the store version is visible again

### 4. Ephemeral Thing (Ticks 4-5)

A snapshot thing that only exists for a few ticks. **Try pinning it!**

- Go to tick 4 or 5
- Open the ephemeral thing
- Click the pin button (📌)
- Navigate to tick 6+
- The pinned thing stays visible in the pinned section, even though it doesn't exist at that tick

## Tick Timeline

| Tick | State |
|------|-------|
| 0-1 | Pointers point into tree, no override |
| 2-3 | Override snapshot active (same key as store thing) |
| 4-5 | Override gone, ephemeral thing appears |
| 6+ | Ephemeral gone (unless pinned!) |

## Things to Try

1. **Explore the tree**: Click a TreePointer, follow links through left/right children and parents
2. **Check backlinks**: Open a tree node and look at its backlinks to see what points to it
3. **See override behavior**: Compare tick 1 vs tick 2 for the overridable thing
4. **Pin before it disappears**: At tick 5, pin the ephemeral thing, then go to tick 6
5. **Notice source badges**: Panels show whether data comes from "snapshot" or "store"
