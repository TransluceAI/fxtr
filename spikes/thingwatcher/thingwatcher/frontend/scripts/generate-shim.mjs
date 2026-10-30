#!/usr/bin/env node
/**
 * Generate a shim module that re-exports from window.__THINGWATCHER_INJECTED__
 *
 * Usage: node generate-shim.mjs <module-name>
 *
 * This script dynamically imports the specified module, enumerates its exports,
 * and generates JavaScript code that re-exports those same names from the
 * global __THINGWATCHER_INJECTED__ object.
 *
 * The generated shim is used via import maps so that esm.sh dependencies
 * (which externalize React) resolve to the same React instance bundled
 * in the main app.
 *
 * The output format matches generateVirtualModuleCode() in esbuildSetup.ts.
 */

const moduleName = process.argv[2];

if (!moduleName) {
  console.error('Usage: node generate-shim.mjs <module-name>');
  process.exit(1);
}

try {
  const mod = await import(moduleName);
  const allExports = Object.keys(mod).filter(k => k !== '__esModule');

  if (allExports.length === 0) {
    console.error(`Module "${moduleName}" has no exports`);
    process.exit(1);
  }

  // Separate default from named exports
  const hasDefault = allExports.includes('default');
  const namedExports = allExports.filter(k => k !== 'default');

  // Generate code matching esbuildSetup.ts pattern
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

  // Named exports via destructuring (like generateVirtualModuleCode)
  if (namedExports.length > 0) {
    code += `export const { ${namedExports.join(', ')} } = __mod;\n`;
  }

  // Default export: use __mod.default if it exists, otherwise export the whole module
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
