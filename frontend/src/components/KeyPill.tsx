import styles from './KeyPill.module.css'

/** How narrow a pill may get, in characters, when the line it sits in is short of room. */
const MIN_CH = 4

/**
 * The card's own invocation key for `dim`, drawn inline as its mapped key table header is drawn,
 * so text elsewhere on the card can say "the key this invocation was spawned with". Short of room, the pill narrows down to `MIN_CH` characters before anything around it wraps (a grid track
 * whose minimum is that width is what lets it), its label cut off under a short fade.
 */
export function KeyPill({ dim }: { dim: string }) {
  return (
    <span
      className={styles.pill}
      style={{ gridTemplateColumns: dim.length > MIN_CH ? `minmax(${MIN_CH}ch, max-content)` : 'max-content' }}
    >
      <span className={styles.label}>{dim}</span>
    </span>
  )
}
