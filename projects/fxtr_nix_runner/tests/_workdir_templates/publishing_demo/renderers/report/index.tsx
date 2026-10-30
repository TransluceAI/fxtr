// Minimal renderer for the publishing-serve E2E `demo` preset.
//
// `lib.esbuildRenderer` bundles this to an ESM module whose default export is the
// 2-level map `module[subtype_key][dispatch_key_uuid] -> Component`. The E2E only
// asserts the compiled module bytes are non-empty + loadable (it does not render),
// so this is a trivial no-op component with no `@fxtr-ui/*` imports — the base
// externals set suffices and esbuild bundles a tiny self-contained module.
const ReportRenderer = () => null;

export default {
  report: {
    "11111111-1111-1111-1111-111111111111": ReportRenderer,
  },
};
