/** Sync prepared skills into an external plugin marketplace checkout: Claude Code's or Codex's.
 *
 * A marketplace's kind is read from its files: `.claude-plugin/marketplace.json` is Claude Code's
 * (TransluceAI/claude-code-plugins), `.agents/plugins/marketplace.json` is Codex's
 * (TransluceAI/codex-plugins). Both carry the same `fxtr` plugin at `plugins/fxtr/`, holding the
 * same prepared skills under `skills/`; they differ only in their manifests (`marketplaceKinds`).
 *
 * Every shipped skill belongs to one plugin, so their sibling links stay local. A changed skill or
 * documentation page bumps the plugin's patch version (in both manifests, for Claude Code, whose
 * marketplace entries carry versions). Provenance alone does not count as a change: unchanged
 * content writes nothing, even from a later commit. Each marketplace's plugin keeps its own
 * version. Major/minor releases are chosen in the marketplace repository. Sync only writes local
 * files; publishing the result is a pull request in that repository.
 */
import {cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {skillNames} from './bundle.mjs';
import {provenanceFile} from './provenance.mjs';

const plugin = 'fxtr';
const initialVersion = '0.1.0';
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
const isDirectory = file => existsSync(file) && statSync(file).isDirectory();

function sameTree(a, b) {
  if (!isDirectory(a) || !isDirectory(b)) return false;
  const entries = directory => readdirSync(directory)
    .filter(name => name !== '__pycache__' && name !== provenanceFile).sort();
  const left = entries(a), right = entries(b);
  return left.length === right.length && left.every((name, i) => {
    if (name !== right[i]) return false;
    const source = path.join(a, name), target = path.join(b, name);
    const sourceStat = statSync(source), targetStat = statSync(target);
    if (sourceStat.isDirectory()) return sameTree(source, target);
    return sourceStat.isFile() && targetStat.isFile()
      && readFileSync(source).equals(readFileSync(target));
  });
}

function bumpPatch(version) {
  if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
    throw new Error(`the plugin's version ${JSON.stringify(version)} is not a plain major.minor.patch`);
  }
  const [major, minor, patch] = version.split('.').map(BigInt);
  return `${major}.${minor}.${patch + 1n}`;
}

function writeJson(file, value) {
  // Keep the marketplace's scalar arrays (such as tags) on one line.
  const text = JSON.stringify(value, null, 2).replace(
    /\[\s*((?:"(?:\\.|[^"\\])*"|[\d.]+)(?:,\s*(?:"(?:\\.|[^"\\])*"|[\d.]+))*)\s*\]/g,
    (_, values) => `[${values.replace(/,\s*\n\s*/g, ', ')}]`,
  );
  mkdirSync(path.dirname(file), {recursive: true});
  writeFileSync(file, text + '\n');
}

const description = 'Skills for writing, running, and viewing fxtr experiments, and for calling models from them with behaviors';

/** What differs between the marketplaces: where their manifests are, and what a new entry and a
 *  new plugin manifest hold. */
export const marketplaceKinds = {
  claude: {
    label: 'Claude Code',
    marketplace: '.claude-plugin/marketplace.json',
    manifest: '.claude-plugin/plugin.json',
    newManifest: version => ({
      name: plugin,
      description: 'Write, run, and view fxtr experiments',
      author: {name: 'TransluceAI'},
      version,
    }),
    newEntry: version => ({
      name: plugin,
      source: `./plugins/${plugin}`,
      description,
      version,
      author: {name: 'TransluceAI'},
      tags: ['experiments', 'skills'],
    }),
    entryVersions: true,
  },
  codex: {
    label: 'Codex',
    marketplace: '.agents/plugins/marketplace.json',
    manifest: '.codex-plugin/plugin.json',
    newManifest: version => ({
      name: plugin,
      version,
      description: 'Write, run, and view fxtr experiments, and call models from them with behaviors.',
      author: {name: 'Transluce', url: 'https://transluce.org'},
      homepage: 'https://docs.transluce.ai/fxtr',
      license: 'Apache-2.0',
      keywords: ['fxtr', 'behaviors', 'experiments', 'skills'],
      skills: './skills/',
      interface: {
        displayName: 'fxtr',
        shortDescription: 'Write, run, and view fxtr experiments.',
        longDescription: description + '.',
        developerName: 'Transluce',
        category: 'Productivity',
        capabilities: ['Read', 'Write'],
        websiteURL: 'https://docs.transluce.ai/fxtr',
        defaultPrompt: [
          'Use fxtr to set up a new experiment project.',
          'Use fxtr to sweep this prompt over several models and judge the replies.',
        ],
      },
    }),
    newEntry: () => ({
      name: plugin,
      source: {source: 'local', path: `./plugins/${plugin}`},
      policy: {installation: 'AVAILABLE', authentication: 'ON_INSTALL'},
      category: 'Productivity',
    }),
    entryVersions: false,
  },
};

/** Which marketplace a checkout is, by the manifest it has; an error for neither or both. */
export function marketplaceKind(marketplaceRoot) {
  const found = Object.entries(marketplaceKinds)
    .filter(([, kind]) => existsSync(path.join(marketplaceRoot, kind.marketplace)));
  if (found.length === 1) return found[0][0];
  const files = Object.values(marketplaceKinds).map(kind => kind.marketplace).join(' or ');
  throw new Error(found.length === 0
    ? `${marketplaceRoot} has no ${files}`
    : `${marketplaceRoot} has both ${files}; sync one marketplace per checkout`);
}

/** Return the new plugin version, or null when no files changed. */
export function syncPlugin(marketplaceRoot, prepared) {
  const kind = marketplaceKinds[marketplaceKind(marketplaceRoot)];
  const marketplacePath = path.join(marketplaceRoot, kind.marketplace);
  const marketplace = readJson(marketplacePath);
  const entries = marketplace.plugins.filter(entry => entry.name === plugin);
  const pluginRoot = path.join(marketplaceRoot, 'plugins', plugin);
  const manifestPath = path.join(pluginRoot, kind.manifest);
  for (const name of skillNames) {
    if (!existsSync(path.join(prepared, name, 'SKILL.md'))) {
      throw new Error(`${prepared} has no ${name}/SKILL.md`);
    }
  }
  const changed = skillNames.filter(name => !sameTree(
    path.join(prepared, name), path.join(pluginRoot, 'skills', name),
  ));
  if (!changed.length && entries.length && existsSync(manifestPath)) return null;

  let manifest, version;
  if (entries.length && existsSync(manifestPath)) {
    manifest = readJson(manifestPath);
    version = bumpPatch(manifest.version);
    manifest.version = version;
  } else {
    version = initialVersion;
    manifest = kind.newManifest(version);
  }
  if (!entries.length) marketplace.plugins.push(kind.newEntry(version));
  else if (kind.entryVersions) entries[0].version = version;

  // Validate the manifests and complete bundle before replacing any plugin files.
  for (const name of changed) {
    const target = path.join(pluginRoot, 'skills', name);
    rmSync(target, {recursive: true, force: true});
    cpSync(path.join(prepared, name), target, {recursive: true});
  }
  writeJson(manifestPath, manifest);
  writeJson(marketplacePath, marketplace);
  return version;
}
