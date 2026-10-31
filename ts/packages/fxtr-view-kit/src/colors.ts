// Chart colours as CSS values over the shell's chart tokens (`--chart-*`), for inline styles and
// SVG fills: the categorical series in their fixed order, and points along the sequential and
// diverging scales with the ink that stays legible on them. Every value is a `var()` or a
// `color-mix()` of tokens, so it follows the shell's theme.

/** The categorical series colours, in the order series take them. */
export const CHART_SERIES = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--chart-8)",
] as const;

/** The colour of everything past the last series, folded into one "other" series. */
export const CHART_OTHER = "var(--fg-faint)";

/** The colour of series `index` (0-based); past the last, `CHART_OTHER`: the palette is never
 *  cycled, since two series sharing a hue would read as one. A series keeps its index however the
 *  view is filtered, so it keeps its colour. */
export function seriesColor(index: number): string {
  return CHART_SERIES[index] ?? CHART_OTHER;
}

function clamp(t: number, low: number, high: number): number {
  return Number.isNaN(t) ? low : Math.min(high, Math.max(low, t));
}

function percent(t: number): string {
  return `${Math.round(t * 1000) / 10}%`;
}

/** Ink for text over a fill whose strength is `t` (0 to 1), as `light-dark()`: the page's ink up to
 *  each theme's flip point, the page's background past it. */
function inkAt(t: number, flipLight: number, flipDark: number): string {
  const light = t > flipLight ? "var(--bg)" : "var(--fg)";
  const dark = t > flipDark ? "var(--bg)" : "var(--fg)";
  return light === dark ? light : `light-dark(${light}, ${dark})`;
}

/** The sequential scale at `t`, from 0 (near nothing, close to the surface) to 1 (the most):
 *  for magnitude, as in a heatmap's cells. */
export function sequentialFill(t: number): string {
  return `color-mix(in oklab, var(--chart-seq-1) ${percent(clamp(t, 0, 1))}, var(--chart-seq-0))`;
}

/** Text over `sequentialFill(t)`. */
export function sequentialInk(t: number): string {
  return inkAt(clamp(t, 0, 1), 0.65, 0.55);
}

/** The diverging scale at `t`, from -1 (`--chart-div-neg`) through 0 (the grey midpoint) to 1
 *  (`--chart-div-pos`): for values on either side of a baseline. */
export function divergingFill(t: number): string {
  const at = clamp(t, -1, 1);
  const pole = at < 0 ? "var(--chart-div-neg)" : "var(--chart-div-pos)";
  return `color-mix(in oklab, ${pole} ${percent(Math.abs(at))}, var(--chart-div-mid))`;
}

/** Text over `divergingFill(t)`. */
export function divergingInk(t: number): string {
  return inkAt(Math.abs(clamp(t, -1, 1)), 0.7, 0.45);
}
