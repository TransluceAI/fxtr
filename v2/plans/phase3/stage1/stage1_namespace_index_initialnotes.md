# Goal: Add "index" functionality to namespaces

Currently namespaces hold str -> Entity mappings. Goal: make a fast way to map back from fields of an entity to the key of it.

Principles: Needs to be top-down and compatible with content-based addressing.

High level idea: Express queries as datalog-style variable unification of path traversals, e.g.
"where turns[$t].metrics[$m].name == 'foo' and turns[$t].metrics[$m].score >= 3, return turns[$t].content"

Need to be forwards-compatible with some changes to the "$fxtr.type" field and its meaning in the future.

How it should broadly work:
- user defines an index on a namespace object by providing:
    - an "indexer": a special fxtr entity defining how to index the values
    - a key prefix (optionally the empty prefix to index the whole store)
- an indexer object must effectively be able to walk a data object and:
    - take a data object and deterministically produce a list of tuples (key, value) where `key` is a fixed-length tuple of primitives and `value` is a primitive, where allowable primitives are int, str, bytes, float, datetime, or FxtrID
    - list the expected types of each entry of `key` and the type of `value`
- indices live in the sqlite database
    - there is a table of what indices are attached in each namespace, and at what prefix; indices are entities so they are represented as pointers to entities in the store. index entities are considered valid namespace roots for namespace membership (they are included in namespace ownership table, but don't appear pinned under any name)
        - each (indexer, prefix) pair is a row in this table and is also how registration happens
        - each registration can also be in one of three states: BUILDING, INDEXED, GARBAGE
            - when we register an indexer, we add it and set its state to BUILDING, then after all entries are correctly indexed for the given key prefix we set it to INDEXED. the index is only useable if it is in INDEXED state for the requested key prefix
            - when we unregister an indexer we set it to GARBAGE. eventually we can clean up the index table described below, for now we don't need to
    - also, for every index that is attached to any namespace, there is a sqlite table called `NS_INDEX__{INDEXER_INTERNAL_ID}` where `INDEXER_INTERNAL_ID` is the internal ID of the indexer object in the store
        - this sqlite table's primary key is a composite key on (namespace_id, internal_entity_id, *key_columns_from_indexer) where the indexer columns are named as IXKEY_0, IXKEY_1, ...
        - it then also has a single VALUE field that has its own sqlite index
    - there is also a table `NS_INDEX_PROCESSED__{INDEXER_INTERNAL_ID}` which has entries (namespace_id, internal_entity_id) for all entities that have been indexed (which is needed in case there is an indexer that produces an empty set of matches for a given entity)
        - this table must always have a direct correspondence with `NS_INDEX__` in the sense that entries are added or removed in the same transaction; this means you can check `NS_INDEX_PROCESSED__` as the source of truth for whether `NS_INDEX__` either contains the results of the indexer or is empty
- index queries are translated dynamically into sqlite queries
    - user will make a query like `ns.iter_matches(first: IndexerConstraint, *rest: IndexerConstraint, key_prefix:str="", where:BindingConstraint|None=None)`
        - BindingConstraint is a dataclass `BindingConstraint(cond: str)` where `cond` is a SQLite conditional expression that can refer to the binding variables like `$t` and `$m`
        - IndexerConstraint is a dataclass `IndexerConstraint(indexer, key_variables: tuple[str, ...], condition: str)`, and `condition` is a SQLite conditional expression like `value > 3` that can refer the value of the indexer expression as `value` (and can also refer to any binding variable like $t and $m)
        - convenience functions to parse expressions:
            - e.g. TypedPathValueIndexer.parse(MyType, "turns[$t: int].metrics[$m: str].name: str", 'value == "foo"') becomes an IndexerConstraint
            - user can use this to build queries easily
    - sqlite conditions are validated by parsing them as expressions via `sqlglot`, then rewritten so that `$` is replaced by `_VAR_`
        - we also filter at this point to ensure that the only referenced columns are the actual variables that are in scope from the IndexerExpression
        - or, for the first BindingCond, it can refer to variables that are in scope from any IndexerExpression
    - we take the collection of conditions, look up the indexers to find the table they should map to, and turn them into a big sql query with subqueries for each piece, where we filter each part with the provided condition, join on the binding variables `_VAR_x`, and (if provided) do a single global restriction on the binding variables
- queries return (key, FxtrID, bindings, arena) similar to `retrieve`, and also add the ID to the namespace

- namespace operations need to maintain the invariants that:
    - for every pinned entity in the namespace, for every index that matches that key prefix whose state is INDEXED, indexer entries for that entity exist in the corresponding `NS_INDEX__` and `NS_INDEX_PROCESSED__` table pair
    - for every entry in an `NS_INDEX__`/`NS_INDEX_PROCESSED__` table pair, if it contains any entities in a namespace, there is at least one entry in the table-of-indices that references that namespace (although it might be listed as garbage)
    - in practice this means:
        - in the write transaction where we set the index to INDEXED, we must also verify that EVERY pinned entity matching that key prefix has an entry in `NS_INDEX_PROCESSED__` for it
        - in any write transaction where we pin an entity, we must also verify that EVERY `INDEXED` indexer matching that key prefix has an entry in `NS_INDEX_PROCESSED__` for it
- although technically perhaps not required, for efficiency we can also try to maintain the stronger invariant that entries _only_ exist if they satisfy some existing registered key prefix
    - when unpinning or replacing a previously pinned entity, we can immediately remove its entries in all indexers for this namespace
    - in the GC loop, we can check if there are any "GARBAGE" indexes, and if so, do a query for all entities reachable from that index that do NOT match any live key prefix in the namespace, and if so, bulk delete all such rows along with the garbage index entries

- an IndexablePinningBackend only needs to support a fixed set of possible indexer types for now, although we can grow this later by using the full fxtr storage system. it should always store the indexer it receives and then load it back as the expected type to make sure that the indexing logic is actually owned by the implementation, not the user.

- the data/fxtr_context_api.py can have a class FxtrQueryableNamespace which extends FxtrNamespace with a new method `iter_matches`, which is a high-level wrapper around the internal `iter_matches`: it takes `type, into_scope, copy_into_scope` like `retrieve`, and yields either a FxtrID or an EntityRef depending on whether this was provided, and also automatially attaches the returned values into the namespace scope rather than exposing the arena

---

# later full sql API

hey Claude, I've just finished implementing a feature described in @plans/phase3/stage1_namespace_index_overview.md . now I am building it out to make it more powerful.

the current system works great for exposing a tabular view of the store and querying for entities that match a particular condition. however, it does not allow you to do arbitrary analytical queries. this is somewhat unfortunate because the database already has the tables to allow it to let you do fancy stuff like this, it just doesn't expose it.

i'm thinking it would be cool to expose a lower-level function on FxtrQueryableNamespace, which would work like

```
ns: FxtrQueryableNamespace

data = await ns.sql(
    """
    SELECT turns.m, turns.turn_source, AVG(turn_metric_score)
    FROM turns
    JOIN metric_info ON metric_info.pin_name == ("metrics/" + turns.m)
    GROUP BY turns.m, turns.turn_source
    """,
    views=dict(
        turns=QueryView(
            "turns/", # <- prefix
            turn_metric_score=TypedPathValueIndexer.query("!.turns[$t: int].metrics[$m: str].score: float"),
            turn_source=TypedPathValueIndexer.query("!.turns[$t: int].source: str"),
        ),
        metric_info=QueryView(
            "metrics/", # <- prefix
            metric_desc=TypedPathValueIndexer.query("!.description: str")
        )
    ),
    dialect="sqlite",
)
```

(this is sort of pseudo-sql but hopefully you know what I mean)

so basically:
- ns.sql gives you the ability to run a bare sql query
- this sql query executes in a context where there are a number of "views" in scope as tables
- the columns of each table are (a) the binding variables of the indexer keys, (b) the values of the indexer outputs, (c) meta info `pin_name` and also `entity_id`

how I'm imagining we'd implement this:
- every call to sql starts a new in-memory database connection in a new thread (unrelated to the internal pool), then attaches the namespace store read-only, runs a generated query, and cleans up
- the generated query contains essentially the existing generate_iter_matches_query in fxtr2/data/store_indexing/query_gen.py but
    - for each view
        - makes sure to also select the value of the indexer (maybe we can refactor to add this where needed)
        - does name remapping so that the variables have the expected names
        - additionally does a LEFT JOIN with the entity table so that we can grab the entity_id
            - but is careful to structure itself to admit the "Omit OUTER JOIN Optimization" in sqlite, where we do something like `SELECT v1, v3 FROM t1 LEFT JOIN t2 ON (t1.ipk=t2.ipk)` which means the query planner can drop the entity id where not used, if possible
        - wraps the result in a common table expression with the requested view name
    - then substitutes in the user's top-level SQL expression
- we'd just pass the raw sqlite3.Row rows to the user and let them deal with it, we wouldn't try to do FxtrID resolution or create any arenas or move anything into a scope. we can use a row factory though for convenience.
- for now we only implement dialect="sqlite". we can parse the user's expression with sqlglot to make sure it is well-formed just to prevent really weird errors later, but we won't try to escape it or anything because it lives in its own read-only connection anyway so it really only gives the user the same amount of power they would have had if they created a sqlite3 connection themselves

does this make sense? please think through the design and lets discuss anything that seems underspecified or ambiguous or anything you have questions about. then we'll write a plan at plans/phase3/stage1post2_direct_sql.md for how to implement it.
