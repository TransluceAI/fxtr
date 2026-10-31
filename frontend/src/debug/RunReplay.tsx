/**
 * The run replay: a rail section, on the Vite dev server only, that replays the job on screen as
 * if it were running, for working on how progress is shown. `replay.ts` invents the timeline and
 * `replayData.ts` answers the app's requests from it; this module holds the controls and the
 * clock.
 *
 * The live store polls the replayed job as it would a real run, so the trace moves at the live
 * poll's pace; a seek, a restart, a failure, or the end polls at once (`pokeLive`).
 */

import { useEffect } from 'react'
import { History, Pause, Play, TriangleAlert, X } from 'lucide-react'
import { setRequestMiddleware } from '../api/graphql'
import { pokeLive } from '../api/live'
import { useStoreSelector } from '../lib/store'
import { clearReplayData, progressAcross, replay, replayMiddleware, type Replay } from './replayData'
import styles from './RunReplay.module.css'

/** How long a whole replay plays for at 1×. */
const REPLAY_MS = 60_000
const SPEEDS: readonly { speed: number; label: string }[] = [
  { speed: 0.25, label: '¼×' },
  { speed: 1, label: '1×' },
  { speed: 4, label: '4×' },
]
/** The slider's steps from start to end. */
const SLIDER_STEPS = 1000

let frame: number | null = null
let lastFrame = 0

function advance(at: number) {
  frame = null
  const current = replay.get()
  if (!current?.playing) return
  const progress = Math.min(1, current.progress + ((at - lastFrame) * current.speed) / REPLAY_MS)
  lastFrame = at
  replay.set({ ...current, progress, playing: progress < 1 })
  if (progress < 1) frame = requestAnimationFrame(advance)
  else pokeLive()
}

function change(next: Partial<Replay>) {
  const current = replay.get()
  if (current === null) throw new Error('no replay to change')
  replay.set({ ...current, ...next })
}

function start(jobId: string) {
  setRequestMiddleware(replayMiddleware)
  replay.set({ jobId, progress: 0, playing: false, speed: 1, failAt: null })
  pokeLive()
}

function stop() {
  pause()
  replay.set(null)
  setRequestMiddleware(null)
  clearReplayData()
  pokeLive()
}

function pause() {
  if (frame !== null) cancelAnimationFrame(frame)
  frame = null
  if (replay.get()?.playing) change({ playing: false })
}

function seek(progress: number) {
  change({ progress })
  pokeLive()
}

function togglePlaying() {
  const current = replay.get()
  if (current === null) throw new Error('no replay to play')
  if (current.playing) {
    pause()
    return
  }
  // Play at the end starts over.
  if (current.progress >= 1) seek(0)
  change({ playing: true })
  lastFrame = performance.now()
  frame = requestAnimationFrame(advance)
}

/** Fail the busiest run from this point on, or take the failure back; the replay stays at the
 *  same moment either way. */
function toggleFailure() {
  const current = replay.get()
  if (current === null) throw new Error('no replay to fail')
  const failAt = current.failAt === null ? current.progress : null
  change({ failAt, progress: progressAcross(current, failAt) })
  pokeLive()
}

/** The rail's replay section for the job on screen. */
export default function RunReplay({ jobId }: { jobId: string }) {
  const current = useStoreSelector(replay, (value) => value)
  const active = current?.jobId === jobId ? current : null
  useEffect(
    () => () => {
      if (replay.get()?.jobId === jobId) stop()
    },
    [jobId],
  )
  if (!active)
    return (
      <section className={styles.replay}>
        <button className={styles.head} title="Replay this job as if it were running" onClick={() => start(jobId)}>
          <Label />
          <History size={12} />
        </button>
      </section>
    )
  return (
    <section className={styles.replay}>
      <div className={styles.head}>
        <Label />
        <button className="icon-button" aria-label="Stop replaying" title="Stop replaying" onClick={stop}>
          <X size={12} />
        </button>
      </div>
      <div className={styles.body}>
        <div className={styles.transport}>
          <button
            className="icon-button"
            aria-label={active.playing ? 'Pause' : 'Play'}
            title={active.playing ? 'Pause' : 'Play'}
            onClick={togglePlaying}
          >
            {active.playing ? <Pause size={12} /> : <Play size={12} />}
          </button>
          <input
            type="range"
            className={styles.slider}
            aria-label="Replay progress"
            min={0}
            max={SLIDER_STEPS}
            value={Math.round(active.progress * SLIDER_STEPS)}
            onChange={(event) => seek(Number(event.target.value) / SLIDER_STEPS)}
          />
          <span className={styles.percent}>{Math.round(active.progress * 100)}%</span>
        </div>
        <div className={styles.options}>
          <div className={styles.speeds} role="group" aria-label="Replay speed" title="1× plays the run in a minute">
            {SPEEDS.map(({ speed, label }) => (
              <button
                key={speed}
                className={styles.option}
                aria-pressed={active.speed === speed}
                onClick={() => change({ speed })}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            className={styles.fail}
            aria-pressed={active.failAt !== null}
            title={
              active.failAt === null
                ? 'Fail the busiest step from here on'
                : 'Take the failure back'
            }
            onClick={toggleFailure}
          >
            <TriangleAlert size={11} />
            fail
          </button>
        </div>
      </div>
    </section>
  )
}

function Label() {
  return (
    <span>
      replay <span className={styles.dev}>dev</span>
    </span>
  )
}
