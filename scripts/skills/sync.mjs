/** CLI: prepare this checkout's skills once and sync them into each marketplace checkout given,
 *  Claude Code's or Codex's (see plugin.mjs). */
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

let scratch;
try {
  const marketplaces = process.argv.slice(2);
  if (!marketplaces.length) {
    throw new Error('usage: pnpm sync:plugin PATH/TO/claude-code-plugins [PATH/TO/codex-plugins ...]');
  }
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('plugin sync requires Node 22 or newer');
  const {prepareSkills} = await import('./bundle.mjs');
  const {marketplaceKind, marketplaceKinds, syncPlugin} = await import('./plugin.mjs');
  // Every checkout's kind is known before any is written.
  const kinds = marketplaces.map(marketplace => marketplaceKinds[marketplaceKind(path.resolve(marketplace))]);
  scratch = mkdtempSync(path.join(tmpdir(), 'fxtr-plugin-'));
  const prepared = prepareSkills(fileURLToPath(new URL('../../', import.meta.url)), path.join(scratch, 'skills'));
  marketplaces.forEach((marketplace, i) => {
    const version = syncPlugin(path.resolve(marketplace), prepared);
    console.log(version === null ? `${kinds[i].label}: the plugin's skills are up to date`
      : `${kinds[i].label}: synced the skills into the fxtr plugin at version ${version}`);
  });
} catch (error) {
  console.error(error.code === 'ERR_MODULE_NOT_FOUND'
    ? `${error.message}\nRun pnpm install at the fxtr checkout root to install skill preparation dependencies.`
    : error.message);
  process.exitCode = 1;
} finally {
  if (scratch) rmSync(scratch, {recursive: true, force: true});
}
