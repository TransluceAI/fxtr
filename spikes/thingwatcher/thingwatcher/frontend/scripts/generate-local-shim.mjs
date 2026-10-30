#!/usr/bin/env node
/**
 * Generate a shim module for a local TypeScript file.
 *
 * Usage: node generate-local-shim.mjs <file-path> <module-name>
 *
 * This script compiles the TypeScript file with esbuild and uses the metafile
 * to enumerate its exports, then generates shim code that re-exports from
 * window.__THINGWATCHER_INJECTED__[moduleName].
 *
 * Used for thingwatcher-bindings which is our own module (not an npm package).
 */

import * as esbuild from 'esbuild';

const filePath = process.argv[2];
const moduleName = process.argv[3];

if (!filePath || !moduleName) {
  console.error('Usage: node generate-local-shim.mjs <file-path> <module-name>');
  process.exit(1);
}

try {
  // Compile the TypeScript file with esbuild and get metafile for exports
  const result = await esbuild.build({
    entryPoints: [filePath],
    bundle: true,
    format: 'esm',
    write: false,  // Don't write to disk
    metafile: true,
    packages: 'external',  // Externalize all npm packages
    plugins: [{
      name: 'stub-css',
      setup(build) {
        // CSS modules - return empty object (we only need export structure)
        build.onLoad({ filter: /\.css$/ }, () => ({
          contents: 'export default {}',
          loader: 'js',
        }));
      },
    }],
  });

  // Get exports from metafile
  const outputs = Object.values(result.metafile.outputs);
  if (outputs.length === 0) {
    console.error(`No outputs from esbuild for "${moduleName}"`);
    process.exit(1);
  }

  const allExports = outputs[0].exports || [];

  if (allExports.length === 0) {
    console.error(`Module "${moduleName}" has no exports`);
    process.exit(1);
  }

  // Separate default from named exports
  const hasDefault = allExports.includes('default');
  const namedExports = allExports.filter(k => k !== 'default');

  // Generate shim code
  let code = `// Auto-generated shim for "${moduleName}"
// Re-exports from window.__THINGWATCHER_INJECTED__["${moduleName}"]

const __mod = window.__THINGWATCHER_INJECTED__?.["${moduleName}"];
if (!__mod) {
  throw new Error(
    'Module "${moduleName}" not found in window.__THINGWATCHER_INJECTED__. ' +
    'Ensure main.js has loaded and populated the global before importing this shim.'
  );
}

`;

  // Named exports via destructuring
  if (namedExports.length > 0) {
    code += `export const { ${namedExports.join(', ')} } = __mod;\n`;
  }

  // Default export
  if (hasDefault) {
    code += `export default __mod.default;\n`;
  } else {
    code += `export default __mod;\n`;
  }

  process.stdout.write(code);
} catch (err) {
  console.error(`Failed to generate shim for "${moduleName}": ${err.message}`);
  process.exit(1);
}
