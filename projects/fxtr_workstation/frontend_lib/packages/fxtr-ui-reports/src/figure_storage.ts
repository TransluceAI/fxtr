/**
 * Project a report's panel-level `ownStorage` atom into per-figure
 * storage slots, keyed by the `figureKey` strings the MDX builder
 * assigns to each `RenderFigure` node.
 *
 * Used by `RootReport` / `SubReport` `FullPanel` renderers:
 *
 *     // panel's ownStorage is FxtrSessionAtom<unknown>, narrowed
 *     // by the renderer to RootReportPanelStorage:
 *     const figureStorage = makeFigureStorage(figuresAtom);
 *
 *     <ReportTreeRenderer document={contents} figureStorage={figureStorage} />
 *
 * The returned function memoizes per-key so multiple calls with the
 * same `figureKey` return the same sub-atom instance (identity-stable
 * across renders, as long as the caller keeps the helper itself
 * stable via `useMemo`).
 */

import type { FxtrSessionAtom } from "@fxtr-ui/core";
import { subAtom } from "@fxtr-ui/state-util";

/** The shape the report panel's storage is narrowed to. */
export type FigureStorageMap = Record<string, unknown>;

/** Function returning a sub-atom for a given figure key. */
export type FigureStorageLookup = (figureKey: string) => FxtrSessionAtom<unknown>;

/**
 * Build a `FigureStorageLookup` over a `Record<string, unknown>` atom.
 *
 * Callers typically derive the input atom via
 * `atomWithDefault(panelOwnStorage as FxtrSessionAtom<X | null>, {})`
 * where `X = FigureStorageMap`, then wrap in `useMemo` so the
 * returned lookup stays identity-stable.
 */
export function makeFigureStorage(
	figuresAtom: FxtrSessionAtom<FigureStorageMap>,
): FigureStorageLookup {
	const cache = new Map<string, FxtrSessionAtom<unknown>>();
	return (figureKey: string) => {
		const cached = cache.get(figureKey);
		if (cached !== undefined) return cached;
		const child = subAtom<FigureStorageMap, string>(figuresAtom, figureKey);
		cache.set(figureKey, child);
		return child;
	};
}
