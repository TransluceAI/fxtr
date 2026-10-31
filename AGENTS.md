For an overview of this repository, see CODE_OVERVIEW.md in this folder. Then read the code overview of the unit you are working in, and any code overview closer to the code you are changing.

## The name

fxtr is pronounced "fixture" and is written in lowercase, even at the start of a sentence. It takes
the article "a", as in "a fxtr project", not "an".

## Docs

Docs come in four kinds, each for one reader. Don't restate one kind in another; link to it.
- `README.md`: someone setting up or running something in this repository: install, commands, configuration, local development, packaging.
- `CODE_OVERVIEW.md`: someone changing the code: what exists, how it fits together, where to make a change.
- The docs site (`docs/`, published at docs.transluce.ai): someone using fxtr or behaviors, a person or a coding agent: what they are, their concepts and public APIs, examples, and setup for the installed packages. Write it for someone who installed fxtr and behaviors from PyPI: no paths into this repository; setup that differs in a checkout goes in `README.md`. Pages link one another by site route: `/fxtr/concepts/overview` is `docs/fxtr/concepts/overview.mdx`.
- Skills (`.agents/skills/`): a coding agent: when the skill applies, what to read for a task, and how an agent carries out and reports its work. The fxtr, behaviors, and first-experiment skills link the docs site's pages by relative path rather than restating them; a rule a person doing the same work needs belongs on the docs site. These three skills ship to projects outside this repository with a copy of the pages they link (`docs/README.md`), so they link nothing else in this repository.

### Code overviews

Code overviews form a tree of `CODE_OVERVIEW.md` files, and each fact lives in exactly one of them: the lowest one whose scope covers it. An overview covers its directory, less any subdirectory with an overview of its own, plus whatever its opening paragraph claims (`fxtr/src/fxtr/core/CODE_OVERVIEW.md` also covers `storage/`).
- The root overview covers the repository at a shallow level: its units, how they depend on and talk to each other, and repository-wide tooling. It lists no files inside a unit.
- Each *unit* has an overview at its root. A unit is a production package, a TypeScript tree, or an example project; the unit table in the root overview is the list. A unit's overview gives its purpose and boundaries, its source tree, its core concepts, an index of the overviews inside it, and where to change things.
- Inside a unit, a subsystem with enough design of its own to need a deeper reference has an overview in its directory.

A parent describes a child in a sentence or two and links to it. It does not repeat the child's content: a directory with its own overview is one line in its parent's file tree.

**Updating.** Whenever you make a substantial change (a new file, a new concept, a restructuring), update the overview that owns the code you changed: the nearest one above it, or the one that claims it. Edit a parent only where what it says has become false: an entry in its file tree was added, removed, or renamed; a subsystem appeared or went away; or how its parts relate changed. The root changes only when a unit is added, removed, or renamed, when units' dependencies or interfaces change, or when repository-wide tooling changes. Update as you go, whenever you finish a subtask worth recording (for a new feature, before you write its tests), rather than all at the end.

**Splitting.** When a subsystem needs more than about a screen of its parent's overview, or an overview grows past roughly 500 lines, move (don't copy) that material into a `CODE_OVERVIEW.md` in the subsystem's directory and leave a one-line pointer. A new unit gets its own overview and a row in the root's unit table.

**Content.** An overview describes the current code for someone new to it: enough that someone asked to make a change could work out what to read and what to change. Keep file trees accurate at the level they list. Group tests by what they cover rather than listing every file, unless there are only a few. A rule that belongs to one module lives in that module's docstring, and the overview points to it.

*Important documentation / comment rule:* Docs should be self-contained and refer to current state.
Commit messages (and plan files, where there are any) are for historical reference. As such, in any code overview,
README, or docstring/comment, do NOT include unqualified references to historical events like "stage 3" or
"part 2" or plan-relative parts like "chunk 5" or "design note 3". The only two cases where
references like this can be kept is if they are:
- Fully qualified references to the _specific file_ where the term is defined, if this is important
  for understanding the design rationale, or
- TODO comments with the temporary TODO specifically referencing future work that is already
  planned, with the requirement that this TODO be removed when implementing that part.

Additionally, these docs should NOT refer to historical changes, or new things replacing old things
that have been removed. (It is OK to reference other parts of the codebase that are still there,
but don't say stuff like "This replaces the old X".) General rule: include exactly the information
that someone _new_ would want to use to understand the _current_ state of the code.

## Code

Default to using `anyio`'s APIs for async instead of `asyncio`.

Default to using `uv run pytest -n auto` to run tests in parallel when running the full test suite.
