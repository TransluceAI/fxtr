/** Joins class names, dropping the falsy ones: `cx(styles.card, selected && styles.isSelected)`. */
export function cx(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(' ')
}
