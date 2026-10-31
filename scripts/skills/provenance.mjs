/** Snapshot identity for the authored inputs and each generated skill folder. */
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, readFileSync, realpathSync} from 'node:fs';
import path from 'node:path';
import {parse as parseToml} from 'smol-toml';

export const provenanceFile = 'provenance.json';
const packageProjects = {fxtr: 'fxtr/pyproject.toml', behaviors: 'behaviors/pyproject.toml'};

export function sourceIdentity(checkout, sources, names, documented) {
  const packages = {};
  for (const [name, project] of Object.entries(packageProjects)) {
    try {
      const data = parseToml(readFileSync(path.join(checkout, project), 'utf8'));
      packages[name] = data.project?.version == null ? null : String(data.project.version);
    } catch {
      packages[name] = null;
    }
  }
  let revision = null;
  let modified = null;
  const git = (...args) => execFileSync('git', ['-C', checkout, ...args], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
  try {
    if (realpathSync(git('rev-parse', '--show-toplevel')) === checkout) {
      const directories = ['scripts/skills', ...names.map(name => `.agents/skills/${name}`),
        ...documented.map(name => `docs/${name}`)];
      const files = [...new Set([
        ...sources.map(source => path.relative(checkout, source)),
        'docs/docs.json', 'package.json', 'pnpm-lock.yaml', ...Object.values(packageProjects),
      ])].filter(file => existsSync(path.join(checkout, file))).sort();
      const commit = git('rev-parse', '--verify', 'HEAD');
      const status = ['status', '--porcelain', '--untracked-files=all'];
      const changes = git(...status, '--', ...directories, ...files)
        + git(...status, '--ignored', '--', ...files);
      revision = commit;
      modified = Boolean(changes);
    }
  } catch {
    // Source archives and checkouts without git still have a content identity.
  }
  return {packages, revision, modified};
}

export function contentHash(name, files) {
  const hash = createHash('sha256');
  for (const destination of [...files.keys()].sort()) {
    if (!destination.startsWith(`${name}/`) || destination === `${name}/${provenanceFile}`) continue;
    hash.update(destination.slice(name.length + 1));
    hash.update('\0');
    hash.update(createHash('sha256').update(files.get(destination).content).digest());
  }
  return hash.digest('hex');
}
