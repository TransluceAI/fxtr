import assert from 'node:assert/strict';
import {execFileSync, spawnSync} from 'node:child_process';
import {cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {prepareSkills} from './bundle.mjs';
import {marketplaceKind, syncPlugin} from './plugin.mjs';

const tool = fileURLToPath(new URL('./', import.meta.url));
const fixture = JSON.parse(readFileSync(new URL('fixture.json', import.meta.url), 'utf8'));
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
function write(root, file, text) {
  mkdirSync(path.dirname(path.join(root, file)), {recursive: true});
  writeFileSync(path.join(root, file), text);
}
function setup(t) {
  const root = mkdtempSync(path.join(tmpdir(), 'fxtr-plugin-test-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  const checkout = path.join(root, 'checkout');
  for (const [file, text] of Object.entries(fixture)) write(checkout, file, text);
  const marketplace = path.join(root, 'marketplace');
  write(marketplace, '.claude-plugin/marketplace.json', JSON.stringify({
    name: 'm', plugins: [{name: 'other', version: '2.0.0'}],
  }));
  const plugin = path.join(marketplace, 'plugins/fxtr');
  return {root, checkout, marketplace, plugin, manifest: path.join(plugin, '.claude-plugin/plugin.json')};
}
/** A Codex marketplace checkout beside the Claude one `setup` made, holding another plugin. */
function codexMarketplace(root) {
  const marketplace = path.join(root, 'codex');
  write(marketplace, '.agents/plugins/marketplace.json', JSON.stringify({
    name: 'm',
    interface: {displayName: 'M'},
    plugins: [{name: 'docent', source: {source: 'local', path: './plugins/docent'}}],
  }));
  const plugin = path.join(marketplace, 'plugins/fxtr');
  return {marketplace, plugin, manifest: path.join(plugin, '.codex-plugin/plugin.json')};
}
function snapshot(root, prefix = '') {
  return readdirSync(path.join(root, prefix), {withFileTypes: true}).flatMap(entry => {
    const file = path.join(prefix, entry.name), absolute = path.join(root, file);
    return entry.isDirectory() ? snapshot(root, file)
      : [{file, content: readFileSync(absolute), mtime: statSync(absolute).mtimeMs}];
  });
}

test('first sync, no writes for provenance alone, and a version bump for documentation changes', t => {
  const {root, checkout, marketplace, plugin, manifest} = setup(t);
  const prepared = prepareSkills(checkout, path.join(root, 'prepared'));
  assert.equal(syncPlugin(marketplace, prepared), '0.1.0');
  assert.equal(readJson(manifest).version, '0.1.0');
  assert(existsSync(path.join(plugin, 'skills/fxtr/references/INDEX.md')));
  assert(existsSync(path.join(plugin, 'skills/behaviors/provenance.json')));
  assert(existsSync(path.join(plugin, 'skills/first-experiment/SKILL.md')));
  const before = snapshot(marketplace);
  write(prepared, 'fxtr/provenance.json', '{"revision":"later"}\n');
  assert.equal(syncPlugin(marketplace, prepared), null);
  assert.deepEqual(snapshot(marketplace), before);

  write(checkout, 'docs/fxtr/unlisted.mdx', '# Changed\n');
  const changed = prepareSkills(checkout, path.join(root, 'changed'));
  assert.equal(syncPlugin(marketplace, changed), '0.1.1');
  assert.equal(readFileSync(path.join(plugin, 'skills/fxtr/references/unlisted.mdx'), 'utf8'), '# Changed\n');
  assert.equal(readJson(manifest).version, '0.1.1');
  assert.equal(readJson(path.join(marketplace, '.claude-plugin/marketplace.json')).plugins[1].version, '0.1.1');
});

test('updates retain unrelated files and manifest fields, and remove obsolete skill files', t => {
  const {root, checkout, marketplace, plugin, manifest} = setup(t);
  const prepared = prepareSkills(checkout, path.join(root, 'prepared'));
  syncPlugin(marketplace, prepared);
  writeFileSync(manifest, JSON.stringify({...readJson(manifest), homepage: 'https://example.com'}));
  write(plugin, 'README.md', 'Plugin setup\n');
  write(plugin, 'skills/other/SKILL.md', 'Another skill\n');
  write(plugin, 'skills/fxtr/obsolete.md', 'removed\n');
  assert.equal(syncPlugin(marketplace, prepared), '0.1.1');
  assert.equal(readJson(manifest).homepage, 'https://example.com');
  assert.equal(readFileSync(path.join(plugin, 'README.md'), 'utf8'), 'Plugin setup\n');
  assert.equal(readFileSync(path.join(plugin, 'skills/other/SKILL.md'), 'utf8'), 'Another skill\n');
  assert(!existsSync(path.join(plugin, 'skills/fxtr/obsolete.md')));
  assert.deepEqual(readJson(path.join(marketplace, '.claude-plugin/marketplace.json')).plugins[0],
    {name: 'other', version: '2.0.0'});
});

test('invalid versions and incomplete bundles are refused before any marketplace writes', t => {
  const {root, checkout, marketplace, manifest} = setup(t);
  const prepared = prepareSkills(checkout, path.join(root, 'prepared'));
  syncPlugin(marketplace, prepared);
  writeFileSync(manifest, JSON.stringify({...readJson(manifest), version: '0.2.0-beta'}));
  write(prepared, 'fxtr/new.md', '# New\n');
  const before = snapshot(marketplace);
  assert.throws(() => syncPlugin(marketplace, prepared), /plain major.minor.patch/);
  assert.deepEqual(snapshot(marketplace), before);
  rmSync(path.join(prepared, 'behaviors'), {recursive: true});
  assert.throws(() => syncPlugin(marketplace, prepared), /behaviors\/SKILL.md/);
  assert.deepEqual(snapshot(marketplace), before);
});

test('a Codex marketplace gets the same skills under its own manifests and version', t => {
  const {root, checkout} = setup(t);
  const {marketplace, plugin, manifest} = codexMarketplace(root);
  const marketplacePath = path.join(marketplace, '.agents/plugins/marketplace.json');
  const prepared = prepareSkills(checkout, path.join(root, 'prepared'));
  assert.equal(syncPlugin(marketplace, prepared), '0.1.0');
  assert(existsSync(path.join(plugin, 'skills/fxtr/SKILL.md')));
  assert(existsSync(path.join(plugin, 'skills/behaviors/references/INDEX.md')));
  assert(!existsSync(path.join(plugin, '.claude-plugin')));
  const created = readJson(manifest);
  assert.equal(created.name, 'fxtr');
  assert.equal(created.version, '0.1.0');
  assert.equal(created.skills, './skills/');
  assert.equal(created.interface.displayName, 'fxtr');
  // Codex marketplace entries carry no version: the plugin's manifest is the one place it lives.
  assert.deepEqual(readJson(marketplacePath).plugins, [
    {name: 'docent', source: {source: 'local', path: './plugins/docent'}},
    {name: 'fxtr', source: {source: 'local', path: './plugins/fxtr'},
      policy: {installation: 'AVAILABLE', authentication: 'ON_INSTALL'}, category: 'Productivity'},
  ]);
  assert.deepEqual(readJson(marketplacePath).interface, {displayName: 'M'});

  const before = snapshot(marketplace);
  assert.equal(syncPlugin(marketplace, prepared), null);
  assert.deepEqual(snapshot(marketplace), before);
  write(checkout, 'docs/fxtr/unlisted.mdx', '# Changed\n');
  assert.equal(syncPlugin(marketplace, prepareSkills(checkout, path.join(root, 'changed'))), '0.1.1');
  assert.equal(readJson(manifest).version, '0.1.1');
  assert.equal(readJson(manifest).interface.displayName, 'fxtr');
  assert.equal(readJson(marketplacePath).plugins[1].version, undefined);
});

test('a checkout is one kind of marketplace', t => {
  const {root, marketplace} = setup(t);
  assert.equal(marketplaceKind(marketplace), 'claude');
  assert.equal(marketplaceKind(codexMarketplace(root).marketplace), 'codex');
  assert.throws(() => marketplaceKind(path.join(root, 'checkout')), /has no \.claude-plugin\/marketplace\.json or/);
  write(marketplace, '.agents/plugins/marketplace.json', '{"name": "m", "plugins": []}');
  assert.throws(() => marketplaceKind(marketplace), /has both/);
});

test('sync CLI prepares once and syncs every marketplace given', t => {
  const {root, marketplace} = setup(t);
  const codex = codexMarketplace(root).marketplace;
  const output = execFileSync(process.execPath, [path.join(tool, 'sync.mjs'), marketplace, codex], {
    cwd: root, env: {...process.env, PATH: ''}, encoding: 'utf8',
  });
  assert.match(output, /Claude Code: synced the skills into the fxtr plugin at version 0.1.0/);
  assert.match(output, /Codex: synced the skills into the fxtr plugin at version 0.1.0/);
  // A checkout of neither kind is refused before any marketplace is written.
  const before = snapshot(codex);
  const refused = spawnSync(process.execPath, [path.join(tool, 'sync.mjs'), codex, path.join(root, 'checkout')], {encoding: 'utf8'});
  assert.equal(refused.status, 1);
  assert.match(refused.stderr, /has no/);
  assert.deepEqual(snapshot(codex), before);
});

test('sync CLI works outside the source checkout without Python', t => {
  const {root, marketplace} = setup(t);
  const run = () => execFileSync(process.execPath, [path.join(tool, 'sync.mjs'), marketplace], {
    cwd: root, env: {...process.env, PATH: ''}, encoding: 'utf8',
  });
  assert.match(run(), /Claude Code: .*version 0.1.0/);
  const before = snapshot(marketplace);
  assert.match(run(), /up to date/);
  assert.deepEqual(snapshot(marketplace), before);
});

test('CLIs report usage and missing dependencies with setup instructions', t => {
  const {root} = setup(t);
  const isolated = path.join(root, 'isolated');
  cpSync(tool, isolated, {recursive: true});
  for (const script of ['sync.mjs', 'prepare.mjs']) {
    const usage = spawnSync(process.execPath, [path.join(tool, script)], {encoding: 'utf8'});
    assert.equal(usage.status, 1);
    assert.match(usage.stderr, /usage:/);
    const args = script === 'sync.mjs' ? [root] : [root, path.join(root, 'out')];
    const missing = spawnSync(process.execPath, [path.join(isolated, script), ...args], {encoding: 'utf8'});
    assert.equal(missing.status, 1);
    assert.match(missing.stderr, /Run pnpm install at the fxtr checkout root/);
  }
});
