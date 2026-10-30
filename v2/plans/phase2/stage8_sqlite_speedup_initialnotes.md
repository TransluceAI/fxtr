hey Claude! I've got a refactor that I'd like your help with.

right now the file @fxtr2/data/sqlite_arena.py uses sqlalchemy core with an async engine to manage its state. however, I'm noticing a lot of overhead due to the async ops and the query translation. I'd like to refactor it to be closer to bare sqlite in an attempt to improve performance.

the key technical changes I want to make:
- remove sqlalchemy core, and replace it with apsw
- convert all typed operations to bare constant SQL execution strings, and make sure they are all constant so that we can use caching, with placeholders as needed for parameters
- try to combine as many consecutive operations into single SELECT or INSERT statements as possible, so that we don't have back-and-forth overheads
- use apsw with a _sync_ interface, but use `anyio.to_thread.run_sync` to run it in a thread pool
    - instead of having an async engine managed by sqlalchemy core, we'll maintain two anyio `CapacityLimiter`s, one for writes (max capacity 1) and one for reads (default max capacity 5). when we dispatch a new op we'll use the appropriate limiter to invoke `run_sync`
    - alongside those capacity limiters we'll maintain connection pools, which we will put connections into when we finish using them in a sync call, and then pull them out again instead of recreating them when needed
    - each cap limiter, connection data (necessary to start a new connection), and connection pool can be attached to some basic ConnectionPool dataclass that just holds onto them and maintains a "closed" flag; the dataclass can have a method close() that closes every idle connection and sets the flag, a method `get_connection` that gets a connection from the freelist or creates one (but errors if closed), and a method `return_connection` that returns it to the freelist (if not closed) or closes the connection (if the ConnectionPool is closed).
        - it can also have a convenience async method `async def run_sync_with_connection[**P, R](self, fn: Callable[Concatenate[Connection, P], R], /, *args: P.args, **kwargs: P.kwargs) -> R` which will use its, and dispatch to anyio's `run_sync` with an inner function that grabs a connection from the pool, runs the fn, and returns it to the pool in a finally block.
        - we will generally have one ConnectionPool for reads and one ConnectionPool for writes
        - ConnectionPool can be used as a context manager, which just closes it at exit

structurally, I want it to be organized like this:
- create a new folder fxtr2/data/sqlite_store that will contain the new implementation (without deleting old one yet)
- within that, create a few files:
    - sqlite_util.py which contains the apsw connection pool helper dataclass
    - sqlite_ops.py which will contain all sqlite statements and operations needed by the namespace
        - ideally, each transaction from sqlite_namespace could become a single sync function here, which could then be called by the `run_sync_with_connection` helper
        - also includes setup logic for setting up the database with the right tables etc, and returning a pair of connection pools for reads and writes
        - (for now, no need to handle migrations, we can error out if we get a version < 2 and say to use the old impl instead)
        - the sync functions can assume they are being called inside `anyio.to_thread.run_sync`, and should do a `anyio.from_thread.check_cancelled()` before they exit each transaction so that if the task was cancelled the transaction doesn't commit
    - sqlite_namespace.py file which will contain the "public" types currently exposed by sqlite_arena.py, which should create the connection pool object and then dispatch to it using the ops in sqlite_ops.py; the flow can basically match the existing sqlite_arena.py logic

in terms of implementation steps, I'd like to:
- plan out the whole implementation and try to anticipate things that will go wrong, and also at this point collect a list of type signatures of every operation we will need in sqlite_ops.py (we shouldn't actually write out the new sql yet, but we should know what all the functions we will need are and which parts of sqlite_arena.py they correspond to)
- write and test the sqlite_util.py helper
- write sqlite_ops.py following the plan
- write sqlite_namespace.py following the plan
- duplicate the existing test_sqlite_arena.py but rebind it to our new ops and namespace implementation, and make sure that it passes the existing tests when rewritten
- pause to regroup and replan about next steps (which will likely involve (a) writing a benchmark to see if we have sped things up, (b) finding instances of the old sqlite arena and swapping them to use the new implementation; we don't have to plan these out yet though, we can wait until we finish the above)

could you please read over the files and the CODE_OVERVIEW to get a sense of what needs to be done, then ask me questions and interview me about what I want until you fully understand what needs to be done and don't have to make any guesses? then, please write out a plan to plans/phase2/stage8_sqlite_speedup.md describing how to do this and including all the clarifications.

---

hey claude, I am working on @plans/phase2/stage8_sqlite_speedup.md . right now the plan says to do all of the sqlite_ops and public API in two blocks before testing any of it. however, I've realized that the old ops and the new ops should be interchangeable at a transaction level. so I think I'd like to adjust the plan to instead follow this rough structure:

* set up the new ops file with the setup logic
* _copy_ the old sqlite_arena.py as-is with the bash tool into sqlite_store/sqlite_namespace.py, then make minor tweaks to the imports section so that it still works
* _copy_ the old test_sqlite_arena.py tests, and tweak their imports so that they test the various values imported from the copy, and make sure it works

* add the ops for initializing the database to `sqlite_ops.py`

* prepare the new sqlite_namespace.py to prepare for refactoring:
    - for constants or helper functions that we anticipate removing entirely (e.g. table metadata definitions or engine construction helpers), temporarily replace them with imports of sqlite_arena.py
    - edit the setup logic to first set up the database and initialize one of our new ConnectionPoolGroups using `sqlite_ops.py`, but then _also_ keep around the existing async sqlalchemy core engine, and continue using that existing engine for all the non-setup transactions
    - make sure all tests pass and all functionality continues to be tested

* iteratively, one op at a time:
    - write the new op in `sqlite_ops.py`
    - edit `sqlite_namespace.py` to use that op in the appropriate method instead of dispatching to the old sqlalchemy engine
    - make sure the tests for `sqlite_namespace` still pass

* finally, once all ops are converted, and there are no more transactions that use the old sqlite engine:
    - remove the async engine
    - remove all imports to sqlite_arena
    - make sure the tests for `sqlite_namespace` still pass
    - update CODE_OVERVIEW / LOCAL_CODE_OVERVIEW

can you read over the plan and let me know if this revised structure makes sense to you or if you have any questions about what I mean? then please edit the plan to reflect this new order. the idea is to make sure that after every operation, we are able to confirm whether we have implemented that operation correctly, instead of waiting to the end.