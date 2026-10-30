
the pieces:
- core fxtr which defines how entities work and allows storing them in git, over HTTP, and in layers built by nix @v2/CODE_OVERVIEW.md
- the nix helpers in @nix/CODE_OVERVIEW.md and @projects/fxtr_nix_runner/CODE_OVERVIEW.md
- a typescript package for representing a subset of fxtr objects in @v2/ts/CODE_OVERVIEW.md and conversion of MDX to entities in @v2/ts/packages/fxtr-mdx-transform/LOCAL_CODE_OVERVIEW.md , currently used primarily from deprecated code e.g. @spikes/old_typescript/example-mdx-rendering/LOCAL_CODE_OVERVIEW.md
- a demo of an interactive workstation built with this at @examples/fxtr_workstation_demo

the thing I want:
- a new example workdir under examples/ which is similar to @examples/fxtr_workstation_demo but intended as a proxy for _publishing_ a report as a nix flake (what a workstation project might graduate to)
    - contents would include:
        - a nix flake configuring everything
        - a python script which loads some entities from a remote store and does some postprocessing
        - the pins for a fxtr git namespace, saved into a subdir of the flake dir
            - which the flake exposes as layered entities, possibly by just duplicating/rewriting the tsvs into the layered entity form and pointing them at the same remote store
        - a typescript processing step that compiles some mdx files using (a possibly tweaked version of) the mdx transform system, and _loads_ entities from a combination of the pins in the git namespace + the layered entities built by the python script, and then exports them back as a layered entity
    - this would then reference a sqlite store that just tracks blobs, likely populated by the git namespace backend but maybe also directly written to
    - running nix on it should then run all the preprocessing code and end up with a layered file store that points to the remote storage
- a new python serving script which exposes the union of (a) the layered entities built by nix + (b) the set of all entities in the git store "index namespace" (the backing blob store with all the entities pinned by ID)
    - this would trigger the build, providing the git store as the backing store during nix builds
    - then it would open and crawl (?) the layered file store from its roots, maybe storing those entities back into its backing store
    - then it would serve that over the existing experimental HTTP interface

some things that need to be built for this:
- a way for nix to parse and interpret git store data and turn it into layered file store entities
- a way for the typescript system in v2/ts to save entities in a way nix can recognize (maybe duplicating nix layer reader/writer functionality there, maybe as a separate package since it depends on filesystem ops)
- the system for actually routing a remote store through all of this and building it together
- the actual demo package and entry point scripts that demonstrate using all of this

---

## additional thoughts 2026-06-13

there are a few design changes I want to revisit here, specifically regarding where this rehosting actually lives and how it is configured. I'm thinking the cleanest design is for fxtr_nix_runner to "own" everything about the fxtr nix project, including that it should manage the rehosting and know where the local blob storage is. this means fxtr_nix_runner would grow a fxtr2 dep which I think is still acceptable, we are sort of promoting fxtr_nix_runner to be a manager of the whole nix flake system and not just a basic runner.

for thinking through this I think we should also think through how the git repo semantics should work. I think basically it would make sense for the fxtr_nix_runner to share the same index namespace that the git store does. in fact we might want to expand fxtr_nix_runner's functionality so that you can also _read_ entities from it, similar to the HTTP read namespace, where this would build them in the flake and then provide them (basically following similar patterns to how the MCP does this currently). it could essentially just adopt/take over this functionality from the MCP (and possibly the workstation dispatcher if applicable), and become the one-stop-shop for pointing something at a nix flake repo and exposing the structured view of the output attrset for all of the consumers.
