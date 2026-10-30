// Library file for StyledThing - exports RefRenderer
// Note: This file is imported by StyledThing.tsx, not loaded directly as a renderer.
// The thingwatcherDialect export is on the main file (StyledThing.tsx).
import styles from './StyledThing.module.css';
import { usePayload } from 'thingwatcher-bindings';
import type { RefRendererProps } from 'thingwatcher-bindings';

export function RefRenderer({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;
  return (
    <div className={styles.ref}>
      <span className={styles.refDot} />
      <span className={styles.refName}>{data?.name}</span>
    </div>
  );
}
