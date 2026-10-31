# Skill preparation — code overview

This repository tooling turns the authored skills that ship outside the checkout (fxtr,
behaviors, and first-experiment) and the documentation they link into portable folders. It uses
the root pnpm workspace's dependencies and runs on Node 22 or newer. The external `fxtr` plugin,
for Claude Code and for Codex, is the distribution path. `pnpm sync:plugin` prepares the skills
once and copies them into local checkouts of the marketplace repositories; the root README covers
setup and publishing.

```
skills/
├── sync.mjs          CLI: prepare this checkout and sync each marketplace checkout given
├── plugin.mjs        marketplace kinds (Claude Code, Codex), plugin file comparison, copying,
│                     manifests, and patch version bumps
├── prepare.mjs       CLI: checkout and new output directory; errors on stderr, nonzero exit
├── bundle.mjs        source discovery, placement, URL resolution, validation, indexes, writing
├── markdown.mjs      remark parsing/serialization, literal links and JSX attributes, anchors
├── provenance.mjs    git state, package versions, per-folder content hashes
├── fixture.json      small source checkout for preparation and plugin tests
└── *.test.mjs        syntax/content preservation, bundle layout, links, metadata, repository docs
```

`prepareSkills` assembles and validates everything in memory before creating the output directory.
`skillNames` in `bundle.mjs` lists the shipped skills, and `documentedSkills` those of them with a
documentation folder. Each skill gets its own files, linked assets, and provenance; a documented
skill also gets all pages from `docs/<skill>/` under `references/` and an index. A skill without
pages of its own, such as first-experiment, links the documented skills' copies. `bundle.mjs`
owns these placement and linking rules. URL resolution retains queries and fragments through
redirects. External URLs remain external; pages outside
the documented skills link to the website. The skills must be distributed together.

`markdown.mjs` uses remark with MDX, GFM, and frontmatter support; plain Markdown skills use the
Markdown parser. The syntax tree identifies links, reference definitions, headings, and literal
`href`, `src`, and `id` JSX attributes. JavaScript expressions are never executed. Generated
formatting is normalized; authored files are never edited. Frontmatter is read with YAML and
heading anchors use github-slugger. Dynamic attributes and MDX imports are not dependency
discovery mechanisms: portable references use literal links to their dependencies.

A marketplace's kind comes from its files: `.claude-plugin/marketplace.json` for Claude Code,
`.agents/plugins/marketplace.json` for Codex (`marketplaceKind`; a checkout with both or neither
is refused before anything is written). Both hold the plugin at `plugins/fxtr/` with identical
prepared skills under `skills/`; `marketplaceKinds` holds what differs: the plugin manifest
(`.claude-plugin/plugin.json`, or `.codex-plugin/plugin.json` with `skills` and an `interface`),
the new marketplace entry, and whether entries carry the version (Claude Code's do, Codex's do
not).

Provenance records the source revision, package versions, and whether source or preparation
tooling differs from the revision; each skill's content hash covers its other generated files.
Plugin sync ignores provenance when comparing content, so a new source commit alone causes
neither a write nor a version bump. Content changes bump the plugin manifest's patch version, and the
marketplace entry's where it has one; first sync creates version 0.1.0. Each marketplace's plugin
keeps its own version. Other plugins and manifest fields are preserved.

The output directory is the boundary between preparation and sync. Every skill is fully
prepared before sync can change the marketplace. Tests use Node's test runner for parsing, bundle
semantics, the repository's authored documentation, CLI diagnostics, and plugin updates.
