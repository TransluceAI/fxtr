I'm thinking a bit about the efficiency of this approach. ideally I'd like writes to be bottlenecked on sqlite rather than having to wait for O(n) file rewrites every time we write. here's an idea i'd like to discuss and see what you think.

suppose that the store can be in one of three states:
- Git-primary: The default state, the state at rest. This means that the TSV in git is the source of truth for what is in the store. Sqlite state is not trusted.
- Sqlite-primary: A transient state used while writes are committing. This means that the TSV in git is _out of date_ and the sqlite store is the source of truth. In sqlite-primary mode we have a specific git state that we expect the repo to be in, and we also track in Python the set of files that might be out of date in git based on what we have modified (in the current process).

When in git-primary mode, if we read from the database we need to check that our database matches the state of the files in git. if the database does not match, we first update the state of the database to match. In git-primary mode we CANNOT write to the store.

When in sqlite-primary mode, we can directly both read from the database without checking git state, and write to the database to update the state.

The way we manage the state of the store is based on state that we store in a `.fxtr/git_store` directory colocated with the fxtr_store.toml. This contains:
- a directory of sqlite marker files, one per process that wants to be in sqlite-primary mode.
- a lock file, which controls modifications that are made by our Python program to any of {the actual git files, the backup copy, the sqlite markers}

For a reader process that is not writing (does not hold a sqlite-primary lock):
- to read some state, check if there are any sqlite markers; if so, read from database, if not, take lock, sync TSVs into database and also update the `backup` subdir to match that TSV state, release lock, read from database

For a process that is writing or wants to write:
- take lock. check for sqlite markers.
    - if there are any, we are already in sqlite primary mode. add a new one for this process/manager, release lock.
    - if there are not, sync TSVs into database and also update the `backup` subdir, add a sqlite marker, release lock. we are now in sqlite primary mode.
- now, since we have marked the store as being in sqlite primary mode, we can save arbitrary state into the database.
- any time we try to _either read or write_ state from the store while we hold a marker, check if our own sqlite marker still exists in the filesystem. if so, access database as normal. if it has been removed, we can no longer trust that we are in sqlite-primary mode; start over from the point where we take the lock and create our own marker.
- any time we specifically _write_ state to the store while we hold a marker, remember which files we have touched.
- finally, once we want to exit sqlite-primary mode (which maybe we do after some debounce timeout) and flush state to git:
    - while holding the lock:
        - make sure our sqlite marker still exists
        - go through all files whose state we have modified. for each
            - read and dump the current sqlite state for that file to a temporary file.
            - atomically update the actual .fxtr.tsv file to this.
        - remove our sqlite marker

This process ensures that:
- As long as there are any writes to sqlite that have not been saved to disk, some process is holding a sqlite marker
- Due to the lock, there is a sequential ordering of reads and writes, such that the last write to a given file is the up-to-date state in the database.


We can also have a sqlite


Future extension:
- also track a backup copy of all `{path/to/name}.fxtr.tsv` files (in a `backup` subdir) representing the state that we expect the out-of-date git repo files to be in
- if we notice that the states have diverged when we try to exit sqlite-primary mode, produce an error (but what / how?) and set some sort of divergence marker


----

I'm thinking a bit about the efficiency of this approach. ideally I'd like writes to be bottlenecked on sqlite rather than having to wait for O(n) file rewrites every time we write. here's an idea i'd like to discuss and see what you think.

Suppose that each TSV in the store can be in one of two states:
- Clean: The default state, the state at rest. This means that the TSV in git is the source of truth for what is in the store. Sqlite state is not trusted.
- Dirty: A transient state used while we are writing to the sqlite DB. This means that the TSV in git is _out of date_ and the sqlite store is the source of truth. Dirty state is detected by the presence of a `{name}.fxtr.tsv.dirty` directory next to the TSV, which will contain an informative message in DO_NOT_COMMIT.md as well as sentinel files of the form `dirtymark-{i}`.


When _reading_, each git namespace checks if there is a `{name}.fxtr.tsv.dirty` directory present. If so, it reads directly from the sqlite db instead of checking the filesystem. If not, it does the mtime check to see if it needs to do a sync.

Additionally, each git namespace object in Python maintains a mapping of files that it needs to update, but has not yet updated. When pinning or unpinning, the process first claims a dirty marker: a new sentinel file `dirtymark-{pid}-{i}` in the dirty directory for some unused i within the process (to avoid conflicts). If the dirty dir doesn't exist yet, it first does a full sync of that file into the sqlite db (bypassing mtime check, always reading the file), and then creates it, marking sqlite as the new source of truth. Then, while it has a dirty marker, the process then simply writes the pin into the worktree namespace in the sqlite store. Each time it does such a write, it makes sure that its own dirty mark is still present; if not it goes through the whole process again to re-create it.

At some later point, possibly at some regular cadence or possibly at a cleanup step, the namespace object may want to do a flush of the data to disk. To do this, the namespace object grabs the TSV lock described below, and checks if it has the only dirty mark in the dir. If there is another dirty mark, it just removes its dirty mark and releases the lock. If this is the last dirty mark, it releases the lock, dumps the state corresponding to this TSV from the sqlite DB back to the TSV location, then re-acquires the lock and removes its own dirty mark. If it is still the _last_ dirty mark, it removes the whole `{name}.fxtr.tsv.dirty` directory; the TSV is now clean. Perhaps the Python manager will auto-flush on close, and also can be configured to flush after K seconds since the last write to a given TSV (in a background async worker)

Since all of these operations are on a per-TSV file level, we can protect them with a single `dirty.lock` file, either inside the `{name}.fxtr.tsv.dirty` directory or as a sibling `{name}.fxtr.tsv.dirty.lock`.

Finally, as a cleanup helper in the CLI, we can have a `flush-to-tsv` helper. The `flush-to-tsv` helper walks the dir looking for `.fxtr.tsv.dirty` dirs, and for each one it finds, it grabs the lock, flushes the sqlite state to disk, then removes all existing dirtymarks and removes the `{name}.fxtr.tsv.dirty` directory. Any existing manager that expects to be holding a dirty mark will realize the next time it tries to write that it no longer has dirty permissions, and will have to go recreate the dirty marks for itself. But if it does not make any more writes, it will not need to do this. Additionally if a process dies before cleaning up its dirty marks this `flush-to-tsv` helper will put it back into a consistent state.

There's a small chance there would be concurrent non-fxtr writes to a .fxtr.tsv while we are holding dirty marks, but we can just overwrite in this case. There's also a small chance someone will try to commit the dirty marks, but if we make the DO_NOT_COMMIT.md contain an informative message saying not to commit this then this is less likely.

----

actually I think this flow is not even quite right. there's an edge case, where if some other process grabs a dirty mark, makes a change to sqlite, and then removes its dirty mark _while_ we are dumping, the dump will be stale. so I think we need some way to track staleness, which maybe the dirty mark manager should also own. what if there is also a file in the dirty directory in some cases that is just called dirtymark_flag. and every time anyone claims a dirty mark, they also make this file. the presence of the file indicates that someone claimed a new dirty mark and might have touched the database. then in the non-forced flush flow, when we prepare flush, we remove dirtymark_flag with the lock held, then dump to TSV. then in finalize_flush, we check if _both_ we are the last dirty mark _and_ there is still no dirtymark_flag. if there is a new dirtymark_flag, our flush is possibly already stale; start over with flush flow from prepare step. if there is no dirtymark_flag, there should also be no new dirty marks; if there are we should log an error but then still go back to prepare step. if there is no dirtymark_flag and we are still the last dirty mark, we have a clean flush; we can remove the dirty dir. note that checking if the TSV is dirty should still check for actual dirty marks, not just the flag, since the flag gets removed before the sync completes.

for the force flow where we flush everything, we should have our own dirty mark, then remove everyone else's dirty mark while we hold ours; removing someone else's dirty mark is "free" and can be done any time you hold your own dirty mark, because processes need to check their dirty mark before writing. then we do a normal prepare and flush cycle, but this time, if we detected a dirtymark_flag, we restart our force-flush op, which means we also go back and remove dirty marks again during the next flush.

I think this could be added into the dirty mark manager by having it read and check for the dirtymark_flag, and also giving it the ability to wipe other people's dirty marks if we have a dirty mark. we should also make sure that we verify holding our own dirty mark before modifying other people's dirty marks or modifying the flag; if somebody has already removed it during a normal flush we can just exit out and assume somebody already did a full force flush, and if we are ourselves doing a full flush we need to reclaim a dirty mark before dropping other people's.

also one more edge case: let's make sure at most one process is trying to flush at a time. we can create a file lock on a new temporary file `flushing` in the dirty dir or maybe as a sibling to it. if a process decides it needs to flush during a prepare flush op, it first acquires this, then checks if the condition still holds. it should then hold this throughout the flush period, despite _not_ holding the overall dirtymark state lock. then after the flush-to-tsv completes, and after the process either finalizes and removes the dirty dir or fails back to the prepare step, then it releases this. this way, if two processes try to do a full flush of the same dir at the same time, they don't fight and remove each other's dirty marks, but instead the second one waits for the first one to finish flushing, and then correctly sees that the first one already did the right thing.


---

I actually was thinking about the design a bit more and think we could make it slightly cleaner from a FS perspective by moving the locks into the dirty directory. the key insight is that
- when acquiring, you can create the dir if not exists, and then create the lock file if not exists, and then acquire it, and then make sure that the file you acquired is still at that location before continuing. this ensures that, when you continue, you are holding the active lock
    - (and, when you continue, you can first check to make sure the dir is in an initialized state by checking for a sentinel file, perhaps the DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS file is a good sentinel)
- when removing the dir, you could atomically rename the dir to some temporary new name while holding the lock, which then implies that whoever else is trying to acquire the lock will discover it is out of date due to being moved. you could then release the lock and unlink the files and directory to clean up. anybody who tries to acquire the lock before you moved the dir will retry after; anybody who tries to acquire it after you moved it will create a new lock and acquire that, but this doesn't race with you because you're removing the directory as part of the process of releasing the lock

I've added some notes from a side conversation with another opus 4.5 instance to @plans/phase2/stage1_git_namespace_side_convo_lockdir.md which might be useful context!

not 100% sure but I think basically we only have to do this dance with the dirty mark state lock, the flushing lock could just live in the dir? I guess when cleaning it up we might want to be holding both locks for safety but I think we will already be doing this because we hold the flushing lock during finalization anyway


---

1. ah I mistyped a bit. i meant tsv->sqlite. we have a dirty flushing lock that governs the reverse direction, but nothing preventing two processes from trying to sync the tsv to sqlite simultaneously and possibly interleaving their updates in a bad way. I'd like to add a simple {name}.fxtr.tsv.sync_lock that is flock-ed while we are syncing from tsv->sqlite, either due to reading and missing an mtime check or due to doing the initial dirty sync. during reads, if we just grabbed the sync lock, we should also check if at this point the dirty dir exists and switch to trusting sqlite at that point even if the dir didn't exist at the beginning of the operation. does that make sense?

2. yeah there are a lot of manual flock invocations in the current design that must be acquired and freed. I'd like to wrap this in a (exclusive) async contextmanager utility that maybe we should write as part of phase 1. so basically, a global async context manager function which maintains global state of what FS locks we have along with our own Python async locks for each, and then we always acquire the python one before trying to acquire the FS lock. this can also handle the inode detection to make sure that the lock we acquire is still at the right path when we acquire it. if the same async event loop tries to acquire the same lock twice the second one should block in python until the first one is done.

then, we should change the rest of the logic to work with this for all the places where currently we might return from a method while holding the lock, we should instead make those methods async context managers which yield/produce their return value as a `with do_whatever(...) as do_whatever_result`. then any work that has to be done while the lock is held should happen in that context manager. for instance, if we are holding the syncing lock, the sync logic should be inside that. if we are holding the flushing lock, the flushing logic should be inside that. when we try to claim a dirty mark, that should be a context manager that handles the initialization of the dir; if the claim succeeds we can exit the block and use the claim, but if it turns out we need a sync then we should do the sync inside the context manager before continuing. this also means that some of the finalization methods that currently explicitly release locks may need to just let their parent do it.

if this can be implemented in a simple way that would be great. if it turns out we need to temporarily release a lock and reacquire it inside some method, we might need to expand the defn of the global async context manager function a bit.


---

one other thing I've realized, actually, which we should come back to after you make the above adjustments: right now it's possible for one process to mess up another by removing its dirty mark while that process is actively writing. I think can fix this by having each dirtymark be itself a lock, which is held while the namespace is writing to sqlite. then, when we remove all dirty marks, we have to first lock them and then delete them while holding the lock. that way, we won't remove the dirty mark until the claimant is not actively writing anymore. (after the claimant finishes writing, they leave their dirtymark there, unlocked, as a declaration that they might want to write more later or that they are responsible for flushing it back if necessary when they exit, but at this point it being unlocked indicates that it is safe for somebody else to break their claim)

----

1. good question. maybe it should take the GitStoreConfig from `config.py`, plus the TSV file pathlib path? would be nice to be able to test it during phase 3.
2. the way that this works is that the sqlite database internally deduplicates storage, but also maintains a separate table of what belongs to each namespace. the way this is exposed to the user is via a "copy" method, which we can use. internally however all it needs to copy is metadata that (re)associates the given entities with the namespace. (the pin method implicitly already "copies" data as needed, so we don't need to do a separate copy.) does that make sense? you can read abstract_arena.py if you haven't already which might have a bit of context.
3. oh this is kind of awkward currently isn't it, at the time of originally planning this I don't think we realized that dirty mark manager needed to know about sync locks. maybe the best solution is actually to move the sync lock logic into DirtyMarkManager, and have it expose a contextmanager that grabs the sync lock for the duration of the context. it can just be a light wrapper around AsyncFileLock.acquire. then the sync.py module doesn't have to worry about it.
4. good question also. maybe mtime logic should also move into DirtyMarkManager, actually, and it can hold the state? lets discuss how this might work. I'm imagining that the dirty mark manager basically grows to be responsible for tracking everything to do with the state of the TSVs. so it could have a method `ensure_synced(self, sync_fn: Callable[[], Awaitable[None]]) -> bool` that runs the sync if needed, or skips it if mtime detection indicates it is safe OR the TSV is dirty, where the invariant is that at the point where it returns, the sqlite database is in a state that incorporates any changes from TSV, and a read transaction would pick up any changes that had happened before it was called. this would also give us the opportunity to update the mtime when we flush the TSV ourselves so that we trust our own state. (although actually now I wonder about whether mtime check might miss writes that happen in the exact same instant as our last mtime update. i'm ok with slightly stale reads but I don't want them to be stale forever. maybe we also store a checksum, and we do ~the thing git does, where right before we sync a TSV file we touch the sync file while holding the sync lock, and then when we check for changes, if the mtime of the tsv is the same as the mtime of the sync lock, we don't trust the mtime check. if mtime changed or if we decided not to trust it, we would do a checksum check while holding the sync lock, and if the checksum check passes, then we touch the sync lock to mark it up to date and release sync lock, and if not, we touch the sync lock and then do a full sqlite sync and then release sync lock. I guess this is sort of a separate thing from your question 4 but does this seem like a reasonable thing to add?)
5. hm good question. I think maybe check_consistent should just be something that we handle in sync_tsv_to_sqlite rather than being a separate method, and it should just raise an exception if it can't sync consistently? what do you think?
6. ah I think full_sync can be a method in sync.py which takes a dirty mark manager and the GitStoreConfig, and finds all non-dirty TSVs and syncs them in? I suppose it should also remove old pins from the database that don't exist in any TSV also. there's maybe a funny thing to think through here which is, what happens when somebody deletes a TSV? I guess we can notice that we had pins with that prefix, and then as a safety measure, take the sync lock for where the TSV used to be (in case somebody is concurrently recreating it or doing something else like that), and do a sync of it (which should remove everything due to it being missing)? wonder about cleanup though.

ok so this seems to motivate some fairly substantial changes to phase 3:
A: we should move the TSV state management into DirtyMarkManager, and also move the sync lock logic into DirtyMarkManager
B: we should have checksumming and mtime edge case handling into DirtyMarkManager also
C: during sync, we should treat missing/removed files roughly the same way we treat modified files, and still use .sync_lock and .dirty markers for them even if the .tsv file is missing. DirtyMarkManager needs to know what to do with new files that appear or known files that disappear.
D: for cleanup, maybe we extend DirtyMarkManager so that, if it is doing a sync for a file that doesn't actually exist on disk, it removes the .sync_lock file right before releasing it (because we don't want old sync locks to hang around, and if somebody recreates the file they will get a new sync lock automatically)
E: functions in sync.py could generally assume they are given a DirtyMarkManager and a GitStoreConfig, and should use it to manage the state consistently. so e.g. the sync_tsv_to_sqlite could instead be something like ensure_sqlite_synced(tsv_path: Path, config, dirty_manager) or ensure_sqlite_full_synced(config, dirty_manager) which uses the manager to ensure that the sqlite database incorporates changes from the TSV, syncing them in as needed. similarly there can be a flush_sqlite_to_tsv_if_needed which wraps the manager's flush method and actually pulls from the database?

then we can add more tests to DirtyMarkManager about mtime checking, and then we can do self-contained tests for sync.py that don't implement a full GitNamespaceBackend but just manage copying between a sqlite namespace and a TSV file managed by a DirtyMarkManager? then in phase 4 we add support for the actual sqlite db modifications and the timing of all these syncs and flushes.

do you think these changes make sense? can you think through this and see if it would solve the problems? let's discuss a bit, then the next step can be to update the plan doc to incorporate this. I realize this is sort of a bunch of scattered thoughts.

----

for the mtime check, lets also save the sync lock mtime in memory, rather than checking it dynamically. because if somebody _else_ touched the sync lock, there's no guarantee they actually synced, so we shouldn't trust the new mtime, just the mtime that the sync lock had right after we last touched it.

for "Delete the .sync_lock file before releasing the lock", lets put this responsibility on DirtyMarkManager, it can be the one that removes the .sync_lock file because it "owns" that file and already is checking if the tsv got removed. the specific logic for removing pins would belong to sync_tsv_to_sqlite but it could probably fall out naturally of the existing logic in `tsv_file` where we already can read a missing TSV as being empty.

for "When we flush SQLite → TSV, we write the TSV file": hm, well we are holding the flushing lock during this time, so everyone accessing it should follow the "dirty" path and skip syncing, I think? people will only try the sync path once we release the flushing lock. so I think we are free to touch the sync_lock right after we atomically update the TSV file. although maybe just to make it easier to reason about, we can also hold onto the sync lock while we are updating the TSV file, and also touch the TSV at this point?

for "For detecting new TSV files, should DirtyMarkManager also track directory mtimes?": yeah good idea. maybe DirtyMarkManager can also be responsible for discovering the list of updated TSVs on disk, and ensure_full_sync can use that to determine which TSVs to sync and then sync each?

does that all make sense? if so, yes please update the plan.
