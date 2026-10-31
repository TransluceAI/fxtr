/** CLI boundary: checkout and output paths in, a prepared directory out, diagnostics on stderr. */
import path from 'node:path';

try {
  const [checkout, target, ...extra] = process.argv.slice(2);
  if (!checkout || !target || extra.length) throw new Error('usage: node scripts/skills/prepare.mjs CHECKOUT OUTPUT');
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('skill preparation requires Node 22 or newer');
  // A dynamic import lets missing dependencies produce a setup instruction without a stack trace.
  const {prepareSkills} = await import('./bundle.mjs');
  prepareSkills(path.resolve(checkout), path.resolve(target));
} catch (error) {
  console.error(error.code === 'ERR_MODULE_NOT_FOUND'
    ? `${error.message}\nRun pnpm install at the fxtr checkout root to install skill preparation dependencies.`
    : error.message);
  process.exitCode = 1;
}
