/**
 * Public surface of `@fxtr-ui/react-dispatch-dynamic-shim`.
 *
 * One function and one type: the host calls `installFxtrExternals`
 * at app startup with the module instances it wants the dynamic
 * dispatcher's renderer bundles to share. See `install_externals.ts`
 * for the design context and behaviour.
 */
export {
	buildShimSource,
	installFxtrExternals,
	type FxtrExternalsInstallArtifacts,
	type InstallFxtrExternalsOptions,
} from "./install_externals";
