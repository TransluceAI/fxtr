# Transluce platform docs

The Mintlify site served at `docs.transluce.ai`. `docs.json` holds the site configuration and
navigation; each `.mdx` file is a page, addressed by its path without the extension
(`fxtr/installation.mdx` is `/fxtr/installation`), and an `index.mdx` by its folder
(`fxtr/index.mdx`, the fxtr introduction, is `/fxtr`). A new page must be added to `navigation`
in `docs.json` to appear in the sidebar. `.mintignore` keeps this README off the site.
`style.css` holds site-wide styles; Mintlify loads every `.css` file in this directory on every
page, and its component selectors (such as `tabs`) are documented at
https://mintlify.com/docs/customize/custom-scripts.
`integrations` in `docs.json` sends page analytics to PostHog (US Cloud); the key there is
PostHog's public project key. Mintlify likewise loads every `.js` file here on every page, and
`posthog-local-opt-out.js` opts browsers on `localhost` out of PostHog, so local previews send no
events; it names the same key, so change both together.

The public docs cover fxtr and behaviors in separate navigation tabs. They cover what the
released code does, not internal plans or design discussion, and they are the one reference for
it: the coding-agent skills in `.agents/skills/fxtr/`, `.agents/skills/behaviors/`, and
`.agents/skills/first-experiment/` send agents to these pages instead of restating them, and the
skills as distributed carry a copy of `fxtr/` and `behaviors/` (see the root `README.md`).
`fxtr/first-experiment.mdx` builds, by hand, the example experiment that the first-experiment
skill's walkthrough follows, in the same order, so a change to that walkthrough's steps or its
example changes the page too. The code is the source of truth, so when an API or
behavior changes, update the matching pages; a skill changes only when a task needs another page,
a page it links moves, or one of its best practices names what changed.

Pages link one another by site route, as Mintlify needs: `/fxtr/concepts/overview` is
`fxtr/concepts/overview.mdx` here, and `/fxtr` is `fxtr/index.mdx`. Every page under `fxtr/` and
`behaviors/` is copied into the prepared skills with its links rewritten to the copies, so each
link must name a page or file that exists, and its fragment a heading of that page.
`pnpm test:skills` at the repository root checks the prepared references and their content, and
plugin sync refuses a broken one. A link to a page outside `fxtr/` and `behaviors/` becomes a link
to the site.
When a page moves, keep its old URL working with a redirect in `docs.json`. To inspect the skills
as plugin users receive them, run `node scripts/skills/prepare.mjs . /tmp/fxtr-skills-preview`
from the repository root with a new output directory, after the root README's JavaScript setup.
Preparation uses remark with MDX, GFM, and frontmatter support. It normalizes the generated
copies' formatting and leaves the authored pages untouched. Use literal links and `href`/`src`
attributes for bundled dependencies: JavaScript expressions are not evaluated, and MDX imports
do not cause imported files to be bundled.

## Preview locally

From this directory, with an LTS release of Node (20.17 or newer; the Mintlify CLI refuses
Node 25):

```bash
npx mint dev            # serves the site at http://localhost:3000
npx mint broken-links   # checks internal links
npx mint validate       # checks docs.json and the pages build
```

If your default Node is too new, run the CLI under Node 22 with
`npx -p node@22 -p mint mint dev` (and likewise for the other commands).
