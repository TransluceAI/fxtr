import { Suspense } from 'react'
import { BookText } from 'lucide-react'
import { useExperimentReadme } from '../api/resources'
import { cx } from '../lib/cx'
import { setLinkDrag } from '../panes/drag'
import { RAIL } from '../panes/columns'
import { VIEWS } from '../panes/address'
import { pageTarget } from '../lib/linkBase'
import { ErrorBoundary } from './ErrorBoundary'
import { Markdown } from './Markdown'
import styles from './ExperimentReadme.module.css'

/**
 * The project's EXPERIMENT.md, fetched from the backend: the file at the project root.
 *
 * A project need not have one, so both the link and the pane body are written to disappear rather
 * than to complain: EXPERIMENT.md is context about the experiment, and its absence is not an error
 * the person viewing a job needs to hear about. Each loads on its own, under its own Suspense, so the rail and the
 * detail view render before the route answers.
 *
 * The link behaves like an entity or invocation chip: clicking opens the experiment in the detail
 * view's columns, dragging carries it into them, and it is highlighted while a panel shows it.
 */
export function ExperimentReadmeLink({ onOpen, active }: { onOpen: () => void; active: boolean }) {
  return (
    <ErrorBoundary fallback={() => null}>
      <Suspense fallback={null}>
        <ExperimentReadmeLinkBody onOpen={onOpen} active={active} />
      </Suspense>
    </ErrorBoundary>
  )
}

function ExperimentReadmeLinkBody({ onOpen, active }: { onOpen: () => void; active: boolean }) {
  const experiment = useExperimentReadme()
  if (!experiment) return null
  return (
    <button
      className={cx(styles.link, active && styles.isOpen)}
      onClick={onOpen}
      title={experiment.path}
      draggable
      onDragStart={(event) => setLinkDrag(event.dataTransfer, pageTarget(VIEWS.experimentReadme), RAIL)}
    >
      <BookText size={12} />
      <span>experiment</span>
    </button>
  )
}

/** The experiment pane: the markdown, rendered. */
export function ExperimentReadmeBody() {
  return (
    <ErrorBoundary fallback={(error) => <span className="detail-none">experiment unavailable: {error.message}</span>}>
      <Suspense fallback={<span className="detail-none">loading experiment…</span>}>
        <ExperimentReadmeMarkdown />
      </Suspense>
    </ErrorBoundary>
  )
}

function ExperimentReadmeMarkdown() {
  const experiment = useExperimentReadme()
  if (!experiment) return <span className="detail-none">no experiment configured</span>
  return (
    <div className={styles.body}>
      <Markdown source={experiment.markdown} empty="the experiment is empty" />
      <p className={styles.path}>{experiment.path}</p>
    </div>
  )
}
