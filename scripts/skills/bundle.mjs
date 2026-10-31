/** Prepare the shipped skills as one portable bundle. All validation precedes writing the target.
 *
 * Sources are .agents/skills/<name> for each shipped skill, and every page under docs/<name> for
 * each documented one. A documented skill's pages become its references/; linked assets elsewhere
 * under docs/ become assets/ of the skill that links them. The skills are siblings, so
 * cross-skill links, including links to another skill's pages, stay local. Other site pages
 * retain a website URL. remark normalizes generated formatting; authored files are never written.
 */
import {
  chmodSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync,
} from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {parsePage, pageExtensions} from './markdown.mjs';
import {contentHash, provenanceFile, sourceIdentity} from './provenance.mjs';

/** Every shipped skill. Each documented skill also carries the pages of docs/<name>/. */
export const skillNames = ['fxtr', 'behaviors', 'first-experiment'];
export const documentedSkills = ['fxtr', 'behaviors'];
export const siteUrl = 'https://docs.transluce.ai';
const isPage = file => pageExtensions.has(path.extname(file));
const posix = file => file.split(path.sep).join('/');
const external = url => /^[A-Za-z][A-Za-z0-9+.-]*:/.test(url) || url.startsWith('//');
const cleanRoute = route => '/' + route.replace(/^\/+|\/+$/g, '');
const isFile = file => existsSync(file) && statSync(file).isFile();
const isDirectory = file => existsSync(file) && statSync(file).isDirectory();
const within = (root, file) => {
  const relative = path.relative(root, file);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
};

export class SkillPackagingError extends Error {
  constructor(checkout, problems) {
    super(`the skills cannot be prepared from ${checkout}:\n${problems.map(p => `  ${p}`).join('\n')}`);
    this.problems = problems;
  }
}

function filesUnder(directory) {
  return readdirSync(directory, {withFileTypes: true})
    .filter(entry => !entry.name.startsWith('.') && entry.name !== '__pycache__')
    .flatMap(entry => {
      const file = path.join(directory, entry.name);
      return entry.isDirectory() ? filesUnder(file) : isFile(file) ? [file] : [];
    }).sort();
}

function existing(file) {
  return [file, `${file}.mdx`, `${file}.md`, path.join(file, 'index.mdx'), path.join(file, 'index.md')]
    .find(isFile);
}

function routeOf(file) {
  const route = file.slice(0, -path.posix.extname(file).length);
  return '/' + (path.posix.basename(route) === 'index' ? path.posix.dirname(route) : route)
    .replace(/^\.$/, '');
}

/** Resolve redirects without dropping their query or fragment. */
function resolveRoute(docs, redirects, url, followed = new Set()) {
  const route = cleanRoute(decodeURIComponent(url.pathname));
  const file = path.resolve(docs, `.${route}`);
  const found = within(docs, file) && existing(file);
  if (found) return {kind: 'file', file: found, search: url.search, hash: url.hash};
  const destination = redirects.get(route);
  if (!destination || followed.has(route)) throw new Error('which names no file');
  const redirected = new URL(destination, url);
  if (!destination.includes('#')) redirected.hash = url.hash;
  if (external(destination)) return {kind: 'external', url: redirected.href};
  return resolveRoute(docs, redirects, redirected, new Set([...followed, route]));
}

function navigationGroups(site, name) {
  const routes = pages => (pages ?? []).flatMap(page => typeof page === 'string'
    ? [`/${page}`] : routes(page.pages));
  const tab = site.navigation?.tabs?.find(tab => tab.tab === name);
  return (tab?.groups ?? []).map(group => ({title: group.group, routes: routes(group.pages)}));
}

class Bundle {
  constructor(checkout) {
    this.checkout = checkout;
    this.docs = path.join(checkout, 'docs');
    this.problems = [];
    this.destinations = new Map(); // Source path -> bundle-relative POSIX path.
    this.pages = new Map(); // Source path -> parsed page; also caches pages outside the bundle.
    this.files = new Map(); // Bundle-relative POSIX path -> {source, content}.
    try {
      this.site = JSON.parse(readFileSync(path.join(this.docs, 'docs.json'), 'utf8'));
    } catch (error) {
      this.problems.push(`docs/docs.json: ${error.message}`);
      this.site = {};
    }
    this.redirects = new Map((this.site.redirects ?? [])
      .map(entry => [cleanRoute(entry.source), entry.destination]));
  }

  show(file) { return posix(path.relative(this.checkout, file)); }

  placeSources() {
    for (const name of skillNames) {
      const skill = path.join(this.checkout, '.agents/skills', name);
      if (!isFile(path.join(skill, 'SKILL.md'))) {
        this.problems.push(`.agents/skills/${name}/SKILL.md is missing`);
      } else {
        for (const source of filesUnder(skill)) {
          const relative = posix(path.relative(skill, source));
          if (['references', 'assets', provenanceFile].includes(relative.split('/')[0])) {
            this.problems.push(`${this.show(source)}: ${relative.split('/')[0]} is reserved for generated files`);
          }
          this.destinations.set(source, `${name}/${relative}`);
        }
      }
      if (!documentedSkills.includes(name)) continue;
      const pages = path.join(this.docs, name);
      if (!isDirectory(pages)) {
        this.problems.push(`docs/${name}/ is missing`);
        continue;
      }
      for (const source of filesUnder(pages).filter(isPage)) {
        const relative = posix(path.relative(pages, source));
        if (relative === 'INDEX.md') this.problems.push(`${this.show(source)}: INDEX.md is generated; rename it`);
        this.destinations.set(source, `${name}/references/${relative}`);
      }
    }
  }

  page(source) {
    if (!this.pages.has(source)) {
      this.pages.set(source, parsePage(readFileSync(source, 'utf8'), path.extname(source) === '.mdx'));
    }
    return this.pages.get(source);
  }

  destination(source, from) {
    if (this.destinations.has(source)) return this.destinations.get(source);
    if (!within(this.docs, source)) throw new Error('outside docs/ and the shipped skills');
    const relative = posix(path.relative(this.docs, source));
    if (isPage(source)) return siteUrl + routeOf(relative);
    const [owner, ...parts] = relative.split('/');
    const destination = documentedSkills.includes(owner)
      ? `${owner}/references/${parts.join('/')}` : `${from.split('/')[0]}/assets/${relative}`;
    this.files.set(destination, {source, content: readFileSync(source)});
    return destination;
  }

  rewrite(link, source, placed) {
    if (external(link)) return link;
    let resolved;
    if (link.startsWith('/')) {
      resolved = resolveRoute(this.docs, this.redirects, new URL(link, siteUrl));
    } else {
      const url = new URL(link, pathToFileURL(source));
      const file = existing(fileURLToPath(url));
      if (!file) throw new Error('which names no file');
      resolved = {kind: 'file', file, search: url.search, hash: url.hash};
    }
    if (resolved.kind === 'external') return resolved.url;
    const {file, search, hash} = resolved;
    if (hash && isPage(file) && !this.page(file).anchors.has(decodeURIComponent(hash.slice(1)))) {
      throw new Error(`but ${this.show(file)} has no heading with the anchor ${hash}`);
    }
    const suffix = search + hash;
    if (file === source && (!link || link.startsWith('#') || link.startsWith('?'))) return suffix;
    const destination = this.destination(file, placed);
    if (external(destination)) return destination + suffix;
    return path.posix.relative(path.posix.dirname(placed), destination).split('/')
      .map(encodeURIComponent).join('/') + suffix;
  }

  index(name) {
    const prefix = `${name}/references/`;
    const pages = [...this.destinations].filter(([, destination]) => destination.startsWith(prefix))
      .map(([source]) => source);
    const listed = new Set();
    const sections = [];
    for (const group of navigationGroups(this.site, name)) {
      const section = [];
      for (const route of group.routes) {
        try {
          const resolved = resolveRoute(this.docs, this.redirects, new URL(route, siteUrl));
          if (resolved.kind === 'file' && pages.includes(resolved.file) && !listed.has(resolved.file)) {
            section.push(resolved.file);
            listed.add(resolved.file);
          }
        } catch {
          // Navigation is site-wide; only this skill's pages belong in its index.
        }
      }
      if (section.length) sections.push([group.title, section]);
    }
    const rest = pages.filter(page => !listed.has(page));
    if (rest.length) sections.push([sections.length ? 'Other pages' : 'Pages', rest]);
    const lines = [
      `# The ${name} documentation`, '',
      `The pages of ${siteUrl}/${name}, copied with this skill: \`provenance.json\`,`,
      'beside its `SKILL.md`, says from which version. Each is listed with what it covers,',
      "in the order of the site's navigation.",
    ];
    for (const [title, section] of sections) {
      lines.push('', `## ${title}`, '');
      for (const page of section) {
        const fields = this.page(page).metadata;
        const link = this.destinations.get(page).slice(prefix.length).split('/').map(encodeURIComponent).join('/');
        const label = fields?.title ?? path.basename(page, path.extname(page));
        lines.push(`- [${label}](${link})${fields?.description ? `: ${fields.description}` : ''}`);
      }
    }
    return lines.join('\n') + '\n';
  }

  assemble() {
    this.placeSources();
    // Parse all pages before changing any tree, so fragment checks see authored headings.
    for (const source of this.destinations.keys()) {
      if (!isPage(source)) continue;
      try { this.page(source); }
      catch (error) { this.problems.push(`${this.show(source)}: ${error.message}`); }
    }
    for (const [source, destination] of this.destinations) {
      if (isPage(source)) {
        const page = this.pages.get(source);
        if (!page) continue;
        for (const link of page.links) {
          try { link.set(this.rewrite(link.url, source, destination)); }
          catch (error) {
            this.problems.push(`${this.show(source)}:${link.line}: links to ${link.url}, ${error.message}`);
          }
        }
        this.files.set(destination, {source, content: Buffer.from(page.serialize())});
      } else {
        this.files.set(destination, {source, content: readFileSync(source)});
      }
    }
    if (this.problems.length) throw new SkillPackagingError(this.checkout, this.problems);
    for (const name of documentedSkills) {
      this.files.set(`${name}/references/INDEX.md`, {source: null, content: Buffer.from(this.index(name))});
    }
    const sources = [...this.files.values()].map(file => file.source).filter(Boolean);
    const identity = sourceIdentity(this.checkout, sources, skillNames, documentedSkills);
    for (const name of skillNames) {
      const record = {
        skill: name, documentation: documentedSkills.includes(name) ? `${siteUrl}/${name}` : null,
        ...identity,
        content_sha256: contentHash(name, this.files),
      };
      this.files.set(`${name}/${provenanceFile}`, {
        source: null, content: Buffer.from(JSON.stringify(record, null, 2) + '\n'),
      });
    }
    return this.files;
  }
}

export function prepareSkills(checkout, target) {
  if (existsSync(target)) throw new Error(`${target} exists; prepare skills in a new directory`);
  const files = new Bundle(realpathSync(checkout)).assemble();
  mkdirSync(target, {recursive: true});
  for (const [destination, {source, content}] of files) {
    const file = path.join(target, destination);
    mkdirSync(path.dirname(file), {recursive: true});
    writeFileSync(file, content);
    if (source) chmodSync(file, statSync(source).mode);
  }
  return target;
}
