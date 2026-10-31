/**
 * The SVG path of a progress ring's arc: `fraction` (strictly between 0 and 1) of the circle of
 * radius `r` about (`x`, `y`), clockwise from the top. Shared by the state glyph and the tab icon.
 */
export function arcPath(x: number, y: number, r: number, fraction: number): string {
  if (!(fraction > 0 && fraction < 1)) throw new Error(`an arc's fraction must be between 0 and 1, not ${fraction}`)
  const angle = 2 * Math.PI * fraction - Math.PI / 2
  const large = fraction > 0.5 ? 1 : 0
  return `M ${x} ${y - r} A ${r} ${r} 0 ${large} 1 ${x + r * Math.cos(angle)} ${y + r * Math.sin(angle)}`
}
