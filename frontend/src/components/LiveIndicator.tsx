import { useLiveStatus } from '../api/live'
import styles from './LiveIndicator.module.css'

/**
 * Under the job picker after a failed poll of the job: `reconnecting…`, since what is on screen
 * is then the last good data. Nothing otherwise.
 */
export function LiveIndicator() {
  const { reconnecting } = useLiveStatus()
  return reconnecting ? <div className={styles.reconnecting}>reconnecting…</div> : null
}
