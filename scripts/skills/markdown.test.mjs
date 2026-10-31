import assert from 'node:assert/strict';
import test from 'node:test';
import {parsePage} from './markdown.mjs';

test('recognizes actual links, not escaped syntax, code, comments or JSX attribute text', () => {
  const text = [
    String.raw`\[escaped](/missing)`, '',
    'Inline `[example](/missing)`.', '',
    '```md', '[example](/missing)', '```', '',
    '{/* [comment](/missing) */}', '',
    '<Card title="Example [x](/not-a-link)" href={"/fxtr"} />', '',
    '[balanced](/images/plot(1).svg)', '',
  ].join('\n');
  const page = parsePage(text);
  assert.deepEqual(page.links.map(link => link.url), ['/fxtr', '/images/plot(1).svg']);
  assert.equal(parsePage('<!-- [x](/missing) -->', false).links.length, 0);
});

test('literal JSX strings are decoded; dynamic expressions are never evaluated', () => {
  const page = parsePage('<img src={"/images/plot.svg"} />\n\n<Card href={throwIfCalled()} />');
  assert.deepEqual(page.links.map(link => link.url), ['/images/plot.svg']);
  page.links[0].set('../assets/plot.svg');
  assert(page.serialize().includes('src="../assets/plot.svg"'));
  assert(page.serialize().includes('href={throwIfCalled()}'));
});

test('uses parsed heading text and github-slugger collision rules', () => {
  const page = parsePage(`---
title: Demo
description: >-
  A description
  over two lines.
---

## Foo
## Foo
## Foo-1
## _Optional_ arguments of \`run_step\`
## Foo <code>bar</code>
## Café
<div id="custom" />
`);
  assert.deepEqual([...page.anchors], [
    'foo', 'foo-1', 'foo-1-1', 'optional-arguments-of-run_step', 'foo-bar', 'café', 'custom',
  ]);
  assert.equal(page.metadata.description, 'A description over two lines.');
});
