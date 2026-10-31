/**
 * The last segment of a dotted name, a type's (`Array` for `org.transluce.fxtr.core.Array`) or a
 * renderer's (`Elicitations` for `pander-score.Elicitations`), with the segment before it when the
 * last is a version (`Report.v2` for `org.example.Report.v2`).
 */
export function shortName(name: string): string {
  const segments = name.split(".");
  return segments.slice(/^v\d+$/.test(segments.at(-1) ?? "") ? -2 : -1).join(".");
}
