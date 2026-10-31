// Numbers as the viewer writes them: a true minus sign (U+2212), thousands grouped, a fixed number
// of decimals so a column of them lines up (with tabular figures, `layout.num`), and an em dash
// for a value that is missing.

/** What a missing value reads as. */
export const MISSING = "—";

const MINUS = "−";

function missing(value: number | null | undefined): value is null | undefined {
  return value === null || value === undefined || Number.isNaN(value);
}

function grouped(value: number, digits: number): string {
  const text = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  // A value that rounds to zero has no sign.
  return value < 0 && Number(text.replaceAll(",", "")) !== 0 ? `${MINUS}${text}` : text;
}

/** `value` with `digits` decimals; by default none for a whole number and two otherwise. */
export function formatNumber(value: number | null | undefined, digits?: number): string {
  if (missing(value)) return MISSING;
  return grouped(value, digits ?? (Number.isInteger(value) ? 0 : 2));
}

/** A share (0 to 1) as a percentage with `digits` decimals: `0.425` is `43%`. */
export function formatPercent(share: number | null | undefined, digits = 0): string {
  if (missing(share)) return MISSING;
  return `${grouped(share * 100, digits)}%`;
}

/** A difference, signed either way: `+0.12`, `−0.12`, and `0.00` for none. */
export function formatSigned(value: number | null | undefined, digits = 2): string {
  if (missing(value)) return MISSING;
  const text = grouped(value, digits);
  return value > 0 && Number(text.replaceAll(",", "")) !== 0 ? `+${text}` : text;
}

/** A count with its noun: `1 greeting`, `3 greetings`, `1,204 replies` (given the plural). */
export function countLabel(n: number, singular: string, plural = `${singular}s`): string {
  return `${grouped(n, 0)} ${n === 1 ? singular : plural}`;
}
