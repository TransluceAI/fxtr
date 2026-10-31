import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import test from 'node:test';
import {documentedSkills, prepareSkills, SkillPackagingError, skillNames, siteUrl} from './bundle.mjs';
import {parsePage} from './markdown.mjs';

const repo = fileURLToPath(new URL('../../', import.meta.url));
const fixture = JSON.parse(readFileSync(new URL('fixture.json', import.meta.url), 'utf8'));
function write(root, file, text) {
  mkdirSync(path.dirname(path.join(root, file)), {recursive: true});
  writeFileSync(path.join(root, file), text);
}
function setup(t) {
  const root = mkdtempSync(path.join(tmpdir(), 'fxtr-skill-test-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  const checkout = path.join(root, 'checkout');
  for (const [file, text] of Object.entries(fixture)) write(checkout, file, text);
  return {root, checkout, out: path.join(root, 'out')};
}
const read = (root, file) => readFileSync(path.join(root, file), 'utf8');
const record = (root, name = 'fxtr') => JSON.parse(read(root, `${name}/provenance.json`));
const urls = text => parsePage(text).links.map(link => link.url);
function files(root, prefix = '') {
  return readdirSync(path.join(root, prefix), {withFileTypes: true}).flatMap(entry => {
    const file = path.posix.join(prefix, entry.name);
    return entry.isDirectory() ? files(root, file) : [file];
  }).sort();
}

test('copies pages, skill files and modes; generates navigation and provenance', t => {
  const {checkout, out} = setup(t);
  write(checkout, '.agents/skills/fxtr/scripts/check.sh', '#!/bin/sh\n');
  chmodSync(path.join(checkout, '.agents/skills/fxtr/scripts/check.sh'), 0o755);
  write(checkout, '.agents/skills/fxtr/__pycache__/x.pyc', 'ignored');
  write(checkout, 'docs/fxtr/.DS_Store', 'ignored');
  prepareSkills(checkout, out);
  assert.equal(statSync(path.join(out, 'fxtr/scripts/check.sh')).mode & 0o777, 0o755);
  assert(!files(out).some(file => file.includes('__pycache__') || file.includes('.DS_Store')));
  for (const name of skillNames) {
    assert(existsSync(path.join(out, name, 'SKILL.md')));
    assert.equal(existsSync(path.join(out, name, 'references/INDEX.md')), documentedSkills.includes(name));
  }
  const index = read(out, 'fxtr/references/INDEX.md');
  assert.deepEqual(urls(index), [
    `${siteUrl}/fxtr`, 'index.mdx', 'concepts/caching.mdx', 'concepts/arrays.mdx', 'unlisted.mdx',
  ]);
  assert(index.includes('Reusing results.'));
  assert(index.includes('## Concepts'));
  assert(index.includes('## Other pages'));
  assert.deepEqual(record(out).packages, {fxtr: '1.2.3', behaviors: '4.5.6'});
  assert.equal(record(out).revision, null);
  assert.equal(record(out).modified, null);
  assert.throws(() => prepareSkills(checkout, out), /exists/);
});

test('rewrites source links, site routes, reference definitions and literal JSX attributes', t => {
  const {checkout, out} = setup(t);
  write(checkout, '.agents/skills/fxtr/SKILL.md',
    '[intro](../../../docs/fxtr/) [behaviors](../behaviors/SKILL.md)\n');
  write(checkout, 'docs/fxtr/unlisted.mdx', `## Here

[intro](/fxtr/) [old](/fxtr/old-name) [setup](/behaviors#set-up) [here](#here)

<Card href={'/fxtr/concepts/caching'} />

[ref]: /fxtr/concepts/arrays
`);
  prepareSkills(checkout, out);
  assert.deepEqual(urls(read(out, 'fxtr/SKILL.md')), ['references/index.mdx', '../behaviors/SKILL.md']);
  assert.deepEqual(urls(read(out, 'fxtr/references/unlisted.mdx')), [
    'index.mdx', 'concepts/caching.mdx', '../../behaviors/references/index.mdx#set-up', '#here',
    'concepts/caching.mdx', 'concepts/arrays.mdx',
  ]);
});

test('a skill without pages of its own links the documented skills\' copies', t => {
  const {checkout, out} = setup(t);
  write(checkout, 'docs/images/plot.svg', '<svg/>');
  write(checkout, '.agents/skills/first-experiment/SKILL.md', `## Steps

[caching](../../../docs/fxtr/concepts/caching.mdx#resolving-a-jobs-conflicts)
[setup](/behaviors#set-up) [fxtr](../fxtr/SKILL.md) [steps](#steps) ![plot](/images/plot.svg)
`);
  prepareSkills(checkout, out);
  assert.deepEqual(urls(read(out, 'first-experiment/SKILL.md')), [
    '../fxtr/references/concepts/caching.mdx#resolving-a-jobs-conflicts',
    '../behaviors/references/index.mdx#set-up', '../fxtr/SKILL.md', '#steps', 'assets/images/plot.svg',
  ]);
  assert.deepEqual(files(path.join(out, 'first-experiment')),
    ['SKILL.md', 'assets/images/plot.svg', 'provenance.json']);
  assert.equal(record(out, 'first-experiment').documentation, null);
  assert.equal(record(out).documentation, `${siteUrl}/fxtr`);
});

test('preserves queries and effective fragments across chained and external redirects', t => {
  const {checkout, out} = setup(t);
  write(checkout, 'docs/docs.json', JSON.stringify({redirects: [
    {source: '/old', destination: '/next'},
    {source: '/next', destination: '/fxtr?source=old#how-it-works'},
    {source: '/external', destination: 'https://example.com/help?q=x'},
  ]}));
  write(checkout, 'docs/fxtr/unlisted.mdx', `## Café

[redirect](/old) [query](/fxtr?source=guide#how-it-works)
[external](/external#part) [self](?q=x#caf%C3%A9)
`);
  prepareSkills(checkout, out);
  assert.deepEqual(urls(read(out, 'fxtr/references/unlisted.mdx')), [
    'index.mdx?source=old#how-it-works', 'index.mdx?source=guide#how-it-works',
    'https://example.com/help?q=x#part', '?q=x#caf%C3%A9',
  ]);
});

test('copies linked assets, including parentheses and spaces; leaves other pages on the site', t => {
  const {checkout, out} = setup(t);
  write(checkout, 'docs/images/plot(1).svg', '<svg/>');
  write(checkout, 'docs/fxtr/images/local plot.svg', '<svg/>');
  write(checkout, 'docs/fxtr/images/unused.svg', '<svg/>');
  write(checkout, 'docs/fxtr/unlisted.mdx',
    '![plot](/images/plot(1).svg) <img src="/fxtr/images/local%20plot.svg" />\n\n[home](/)\n');
  write(checkout, 'docs/behaviors/index.mdx', '![plot](/images/plot(1).svg)\n');
  prepareSkills(checkout, out);
  assert.deepEqual(urls(read(out, 'fxtr/references/unlisted.mdx')), [
    '../assets/images/plot(1).svg', 'images/local%20plot.svg', `${siteUrl}/`,
  ]);
  assert.equal(read(out, 'fxtr/assets/images/plot(1).svg'), '<svg/>');
  assert.equal(read(out, 'behaviors/assets/images/plot(1).svg'), '<svg/>');
  assert.equal(read(out, 'fxtr/references/images/local plot.svg'), '<svg/>');
  assert(!existsSync(path.join(out, 'fxtr/references/images/unused.svg')));
});

test('reports all broken links and redirect cycles before writing', t => {
  const {checkout, out} = setup(t);
  write(checkout, 'README.md', 'checkout only');
  write(checkout, '.agents/skills/fxtr/SKILL.md', '[readme](../../../README.md)\n');
  write(checkout, 'docs/docs.json', JSON.stringify({redirects: [
    {source: '/loop-a', destination: '/loop-b'}, {source: '/loop-b', destination: '/loop-a'},
  ]}));
  write(checkout, 'docs/fxtr/unlisted.mdx',
    '[missing](/fxtr/gone)\n[anchor](/fxtr#gone)\n[loop](/loop-a)\n');
  assert.throws(() => prepareSkills(checkout, out), error => {
    assert(error instanceof SkillPackagingError);
    assert.equal(error.problems.length, 4);
    assert(error.problems.some(p => p.includes('docs/fxtr/unlisted.mdx:2:')));
    assert(error.problems.some(p => p.includes('outside docs/')));
    return true;
  });
  assert(!existsSync(out));
});

test('refuses missing inputs, invalid MDX, and collisions with generated files', t => {
  const {checkout, out} = setup(t);
  rmSync(path.join(checkout, '.agents/skills/behaviors/SKILL.md'));
  rmSync(path.join(checkout, 'docs/docs.json'));
  write(checkout, '.agents/skills/fxtr/references/notes.md', 'reserved');
  write(checkout, 'docs/fxtr/INDEX.md', '# Reserved');
  write(checkout, 'docs/fxtr/unlisted.mdx', '<Card unclosed');
  assert.throws(() => prepareSkills(checkout, out), error => {
    assert.equal(error.problems.length, 5);
    assert(error.problems.some(p => p.includes('SKILL.md is missing')));
    assert(error.problems.some(p => p.includes('docs/fxtr/unlisted.mdx:')));
    return true;
  });
  assert(!existsSync(out));
});

test('hashes generated content reproducibly and scopes hashes to each skill', t => {
  const {checkout, root, out} = setup(t);
  prepareSkills(checkout, out);
  prepareSkills(checkout, path.join(root, 'again'));
  assert.deepEqual(record(out), record(path.join(root, 'again')));
  const digest = createHash('sha256');
  for (const file of files(path.join(out, 'fxtr')).filter(file => file !== 'provenance.json')) {
    digest.update(file + '\0');
    digest.update(createHash('sha256').update(readFileSync(path.join(out, 'fxtr', file))).digest());
  }
  assert.equal(record(out).content_sha256, digest.digest('hex'));
  write(checkout, 'docs/fxtr/unlisted.mdx', '# Changed\n');
  prepareSkills(checkout, path.join(root, 'changed'));
  assert.notEqual(record(out).content_sha256, record(path.join(root, 'changed')).content_sha256);
  assert.deepEqual(record(out, 'behaviors'), record(path.join(root, 'changed'), 'behaviors'));
});

const git = (root, ...args) => execFileSync('git', [
  '-C', root, '-c', 'user.name=t', '-c', 'user.email=t@t', ...args,
], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();

test('provenance distinguishes committed, untracked, edited and ignored sources', t => {
  const {checkout, root, out} = setup(t);
  write(checkout, '.gitignore', '*.log\n');
  git(checkout, 'init', '-q');
  git(checkout, 'add', '.');
  git(checkout, 'commit', '-qm', 'docs');
  write(checkout, 'unrelated.txt', 'not bundled');
  prepareSkills(checkout, out);
  assert.equal(record(out).revision, git(checkout, 'rev-parse', 'HEAD'));
  assert.equal(record(out).modified, false);
  for (const [label, file] of [
    ['untracked', 'docs/fxtr/new.mdx'], ['ignored', '.agents/skills/fxtr/notes.log'],
    ['edited', '.agents/skills/behaviors/SKILL.md'],
  ]) {
    write(checkout, file, '# Changed\n');
    prepareSkills(checkout, path.join(root, label));
    assert.equal(record(path.join(root, label)).modified, true);
    if (label !== 'edited') rmSync(path.join(checkout, file));
  }
});

test('a fixture inside another git repository records no revision', t => {
  const {checkout, root, out} = setup(t);
  git(root, 'init', '-q');
  git(root, 'add', '.');
  git(root, 'commit', '-qm', 'outer');
  prepareSkills(checkout, out);
  assert.equal(record(out).revision, null);
  assert.equal(record(out).modified, null);
});

function semanticTree(page) {
  // Link destinations are the only semantic change; canonicalize them before comparing trees.
  for (const link of page.links) link.set('https://example.com/target');
  return JSON.parse(JSON.stringify(page.tree, (key, value) => {
    if (key === 'position' || key === 'data') return undefined;
    // MDX serialization indents wrapped inline spans with their component. Compare their
    // visible text across soft line wraps; fenced code values and other spaces stay exact.
    if (value?.type === 'inlineCode') return {...value, value: value.value.replace(/\r?\n[ \t]*/g, ' ')};
    return value;
  }));
}

test('the real bundle preserves prose, code, metadata and MDX structure; every local link resolves', t => {
  const {out} = setup(t);
  prepareSkills(repo, out);
  for (const name of documentedSkills) {
    for (const relative of files(path.join(repo, 'docs', name)).filter(file => file.endsWith('.mdx'))) {
      const original = parsePage(read(repo, `docs/${name}/${relative}`));
      const prepared = parsePage(read(out, `${name}/references/${relative}`));
      assert.deepEqual(semanticTree(prepared), semanticTree(original), relative);
      assert.deepEqual(prepared.metadata, original.metadata, relative);
    }
  }
  for (const name of skillNames) {
    const original = parsePage(read(repo, `.agents/skills/${name}/SKILL.md`), false);
    const prepared = parsePage(read(out, `${name}/SKILL.md`), false);
    assert.deepEqual(semanticTree(prepared), semanticTree(original), name);
  }
  for (const file of files(out).filter(file => /\.mdx?$/.test(file))) {
    const page = parsePage(read(out, file), file.endsWith('.mdx'));
    for (const link of page.links) {
      const url = new URL(link.url, pathToFileURL(path.join(out, file)));
      if (url.protocol !== 'file:') continue;
      const target = fileURLToPath(url);
      assert(target.startsWith(out + path.sep), `${file}: ${link.url}`);
      assert(existsSync(target), `${file}: ${link.url}`);
      if (url.hash && /\.mdx?$/.test(target)) {
        assert(parsePage(readFileSync(target, 'utf8'), target.endsWith('.mdx')).anchors
          .has(decodeURIComponent(url.hash.slice(1))), `${file}: ${link.url}`);
      }
    }
  }
});
