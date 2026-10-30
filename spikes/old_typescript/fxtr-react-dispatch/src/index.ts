// @fxtr/react-dispatch — Type-driven dispatch of fxtr entities to React renderers.

export { type DispatchKey, type ExtractDispatchProps, declareDispatchKey } from "./types.js";
export type { DispatchResolution, DispatchHandler } from "./types.js";

export { DispatchProvider } from "./context.js";

export { Render, type RenderProps } from "./Render.js";

export {
	createDispatchRegistry,
	entry,
	type RegistryEntry,
	type RegistryOptions,
} from "./registry.js";
