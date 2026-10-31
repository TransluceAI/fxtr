import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useLiveViews } from '../views/liveViews'
import styles from './ViewsNotice.module.css'

/** How long "updated" shows after the project's renderers were replaced. */
const UPDATED_MS = 2500

/** What is going on with the project's renderers, in the rail (see views/registry.ts and
 *  views/liveViews.ts): why they are missing, when the app runs on the viewer's defaults alone;
 *  that `fxtr view` is rebuilding them, or why its last build failed (the last good
 *  build still shown); and, briefly, that they were just replaced. Nothing when they loaded and
 *  are current, or when the project has none. */
export function ViewsNotice({ problem }: { problem: string | null }) {
  const { views, build, updates } = useLiveViews()
  const updated = useRecentlyChanged(updates)
  return (
    <>
      {problem !== null && <Notice title="project renderers not loaded" message={problem} error />}
      {build.state === 'failed' && build.error !== null && (
        <Notice
          title="project renderers did not build"
          message={views.version === null ? build.error : `the last build is still shown\n\n${build.error}`}
          output
          error
        />
      )}
      {build.state === 'building' && <div className={styles.status}>rebuilding project renderers…</div>}
      {build.state === 'ready' && updated && <div className={styles.status}>project renderers updated</div>}
    </>
  )
}

/** Whether `count` changed in the last `UPDATED_MS` (never for its first value). */
function useRecentlyChanged(count: number): boolean {
  const [shownFor, setShownFor] = useState<number | null>(null)
  const [first] = useState(count)
  useEffect(() => {
    if (count === first) return
    setShownFor(count)
    const timer = window.setTimeout(() => setShownFor(null), UPDATED_MS)
    return () => window.clearTimeout(timer)
  }, [count, first])
  return shownFor === count
}

/** A notice; `output` is a build's output, shown scrolled to its end, where the error is. */
function Notice({ title, message, output = false, error = false }: { title: string; message: string; output?: boolean; error?: boolean }) {
  const text = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    if (output && text.current) text.current.scrollTop = text.current.scrollHeight
  }, [output, message])
  return (
    <div className={styles.notice} role={error ? 'alert' : 'status'}>
      <div className={styles.title}>{title}</div>
      <div ref={text} className={output ? styles.output : styles.message}>{message}</div>
    </div>
  )
}
