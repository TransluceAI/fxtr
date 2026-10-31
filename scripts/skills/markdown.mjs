/** Markdown/MDX syntax belongs to remark. Bundle policy belongs to bundle.mjs. */
import GithubSlugger from 'github-slugger';
import {toString} from 'mdast-util-to-string';
import {remark} from 'remark';
import remarkFrontmatter from 'remark-frontmatter';
import remarkGfm from 'remark-gfm';
import remarkMdx from 'remark-mdx';
import {visit} from 'unist-util-visit';
import {parse as parseYaml} from 'yaml';

export const pageExtensions = new Set(['.md', '.mdx']);

/** Read literal JSX attribute values without evaluating JavaScript. */
function attributeText(attribute) {
  if (typeof attribute.value === 'string') return attribute.value;
  const statement = attribute.value?.data?.estree?.body?.[0];
  const expression = statement?.type === 'ExpressionStatement' && statement.expression;
  return expression?.type === 'Literal' && typeof expression.value === 'string'
    ? expression.value : undefined;
}

/** Parse once; retain the tree for rewriting and its original positions for diagnostics. */
export function parsePage(text, mdx = true) {
  const processor = remark().use(remarkFrontmatter).use(remarkGfm);
  if (mdx) processor.use(remarkMdx);
  const tree = processor.parse(text);
  const frontmatter = tree.children.find(node => node.type === 'yaml');
  const metadata = frontmatter ? parseYaml(frontmatter.value) : {};
  const anchors = new Set();
  const slugger = new GithubSlugger();
  const links = [];
  visit(tree, node => {
    if (node.type === 'heading') anchors.add(slugger.slug(toString(node)));
    if (['link', 'image', 'definition'].includes(node.type)) {
      links.push({url: node.url, line: node.position.start.line, set: url => { node.url = url; }});
    }
    if (node.type === 'mdxJsxFlowElement' || node.type === 'mdxJsxTextElement') {
      for (const attribute of node.attributes) {
        if (attribute.type !== 'mdxJsxAttribute') continue;
        const value = attributeText(attribute);
        if (value === undefined) continue;
        if (attribute.name === 'id') anchors.add(value);
        if (attribute.name === 'href' || attribute.name === 'src') {
          links.push({
            url: value,
            line: attribute.position.start.line,
            set: url => { attribute.value = url; },
          });
        }
      }
    }
  });
  return {tree, metadata, anchors, links, serialize: () => processor.stringify(tree)};
}
