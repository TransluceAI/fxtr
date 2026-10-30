hey Claude, I'm working on the sqlite_store, trying to make it as efficient as possible. current plan is to do a pretty big redesign aimed at maximizing read performance.

right now, the sqlite database is the source of truth both for the metadata of relationships between entities and namespaces, and ALSO the backing store for the actual blob data. but this means that reads have to always go through sqlite. metadata reads also have to happen by doing pointer-chasing through the sqlite DB which adds delay.

I would like to optimize this by moving to a hybrid sqlite-lmdb approach.

high level design idea:
- sqlite data moves out of sqlite DB into a LMDB backing store `{dbname}.data1.lmdb` (the 1 suffix indicates format version 1)
- this is represented in the sqlite DB as the entity storage location being "data1/1", and the blob data is not actually in the sqlite store anymore
- LMDB format v1 is:
    - sub database called "data1/1" containing all data
    - store keys are a 64-bit repr of the sqlite db entity ID (the internal ID, not the public fxtr ID; this should improve data locality because consecutive keys are written in ascending order)
    - store values are blobs which have a few different data parts, stored as a flat bytearray with a simple offset table:
        - the bytes of the FxtrID for the entity (always a "d" or "n" node because those are the nodes with backing data)
        - the bytes of the canonical CBOR bytes of the entity
        - the bytes of a CBOR encoding of a metadata object for the entity's child relationships, which includes:
            - a deps list of the entity, similar to what be stored in FxtrEntityFlatData, except that it contains internal IDs instead of FxtrID
            - "precomp data" for the entity, based on the current precomp data stored by SQLiteNamespaceArena, which is a map from internal ID to a tuple of (the FxtrID of it, the internal ID it unwraps to or None, the internal ID of its ctx node or None, the internal ID of its step-back-p-ctx if it is a p-ctx or None)
                - where this map is "closed", in the sense that it includes this entity, every entity in its deps list, and every entity that appears as a value inside the map
            - (the idea here is that decoding this metadata object will give the SQLiteNamespaceArena everything it needs to (a) construct the FxtrEntityFlatData, (b) track all deps, (c) update its own `_precomp_*` dicts to maintain its invariants based on the new deps it tracked, while only having to do a single read from the DB)
- sqlite DB is the source of truth for the contents of the store, and is updated in a two-step procedure when adding new entity data:
    - first, writer claims an entity ID by inserting a row where public_id is NULL (changing schema to allow this) and storage_location is "pending", and commits the transaction. this ensures nobody else will use this entity ID
    - next, writer computes and writes the blob to the LMDB store, and commits the LMDB transaction
    - finally, writer opens a new sqlite transaction, updates the public_id to the FxtrID of the entity, and updates storage location to "data1/1".
        - at this point, the write is fully committed, and the entity is considered visible to consumers of the sqlite DB
        - if this fails due to a uniqueness constraint violation on public FxtrID, this means somebody raced us to write; unwind our work:
            - delete our copy of the LMDB data
            - then after this commits, unclaim our now-unused original entity ID in sqlite
            - then, return
- once a reader finds an LMDB node they can load it and load its children without touching sqlite at all: SQLiteNamespaceArena can simply read directly from the LMDB store
- because LMDB is memory mapped, this means we will be bottlenecked by Python overhead, so we can get maximum performance by actually doing the load _synchronously_ rather than doing any async work in `load` or `load_flat` inside SQLiteNamespaceArena. (operations remain `async def` for compatibility with the abstract interface, and sqlite namespace lookups remain async, but the actual load-data path occurs synchronously)
- our existing ConnectionWorkerPool can be modified to also have a LMDB Environment and Database that it holds. for safety we should add a comment saying that opening two ConnectionWorkerPools to the same LMDB can cause undefined behavior and pointing people to the utility in sqlite_namespace which ensures that we only have a single ConnectionWorkerPool per database.
- this change is going to be very difficult to migrate but luckily this system is not really under production use at the moment. for now we can add a fake "database migration" that just errors out
    - however lets actually make a copy of the old version of all of sqlite_store into fxtr2/data/deprecated/sqlite_store_dbv3/ which can be the canonical way to load v3-formatted data, and the migration can just tell you to use that to load the data and copy it in using the high-level fxtgr APIs
    - and for consistency we can move the existing sqlite_arena and sqlite_arena_migrations from fxtr2/data/deprecated/ to fxtr2/data/deprecated/sqlite_store_dbv2
    - we can copy the existing tests of sqlite_store to tests/deprecated also. but we should also keep the tests and make sure they still pass with the new implementation where applicable
    - (you shouldn't need to read the actual existing deprecated code while thinking through and planning this, it isn't really relevant)

can you please read over the @CODE_OVERVIEW.md and the files in @fxtr2/data/sqlite_store to get an understanding of the relevant parts of the codebase, then discuss with me anything that needs to be clarified, any uncertainties you have about how this will work, or any flaws with this design that need to be addressed? then after we are done discussing I'd like you to write a plan for how to implement this in plans/phase2/stage16_lmdb_sqlite_hybrid.md
