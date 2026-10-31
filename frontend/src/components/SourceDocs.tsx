import { Suspense } from 'react'
import { FileCode2 } from 'lucide-react'
import { useSource } from '../api/resources'
import { codeLocation, shortCommit } from '../lib/code'
import { codeTarget } from '../lib/linkBase'
import { dragLink, useEntityOpener } from '../lib/openEntity'
import { ErrorBoundary } from './ErrorBoundary'
import { Markdown } from './Markdown'
import { cx } from '../lib/cx'
import styles from './SourceDocs.module.css'

/**
 * A function's docstring and where it is implemented, from the backend: as the code version the
 * node's job ran records it (`codeVersion`), so a function written after the viewer started still
 * has its docs and code. With `codeAt`, the step path of the node (none: the root workflow), its
 * "implemented" chip is a link to the node's code (`code(…)`, shown by `CodeBody`): a click opens it
 * in a panel, as an entity chip opens its entity, a drag carries it into the columns, and it is lit
 * while open. Loads on its own so a card renders before the backend answers; a node with no
 * function shows nothing.
 */
export function SourceDocs({
  method,
  codeVersion,
  codeAt,
}: {
  method: string | null
  codeVersion: string | null
  codeAt?: readonly string[]
}) {
  if (!method) return <span className="detail-none">none</span>
  return (
    <ErrorBoundary fallback={(error) => <span className="detail-none">docs unavailable: {error.message}</span>}>
      <Suspense fallback={<span className="detail-none">loading docs…</span>}>
        <SourceBody method={method} codeVersion={codeVersion} codeAt={codeAt} />
      </Suspense>
    </ErrorBoundary>
  )
}

function SourceBody({ method, codeVersion, codeAt }: { method: string; codeVersion: string | null; codeAt: readonly string[] | undefined }) {
  const source = useSource(method, codeVersion)
  if (!source) return <span className="detail-none">source not indexed</span>
  return (
    <>
      <Markdown source={source.docstring} empty="no docs" />
      {codeAt && (
        <CodeChip
          steps={codeAt}
          location={codeLocation(source)}
          file={source.file}
          commit={source.commit}
        />
      )}
    </>
  )
}

/** The chip: the file and lines, then the commit they are from (none for the working tree's). */
function CodeChip({
  steps,
  location,
  file,
  commit,
}: {
  steps: readonly string[]
  location: string
  file: string
  commit: string | null
}) {
  const opener = useEntityOpener()
  const target = codeTarget(steps)
  const state = opener.linkState(target)
  // The directory truncates with an ellipsis in a narrow card; the file name and lines never do.
  const cut = Math.max(0, file.lastIndexOf('/'))
  return (
    <div className={styles.code}>
      <div className={styles.reference}>
        <span className={styles.kind}>implemented</span>
        <button
          className={cx('chip', styles.ref, state.selected ? styles.isOpen : state.open && styles.isPinned)}
          title={`${location} ${commit === null ? 'in the working tree' : `at commit ${commit}`}: show the code`}
          onClick={(event) => {
            event.stopPropagation()
            opener.follow(target, { pin: event.altKey })
          }}
          onMouseEnter={() => opener.hover(target)}
          onMouseLeave={() => opener.hover(null)}
          draggable
          onDragStart={(event) => dragLink(opener, event, target)}
        >
          <FileCode2 size={13} />
          <span className={styles.location}>
            <span className={styles.dir}>{location.slice(0, cut)}</span>
            <span className={styles.file}>{location.slice(cut)}</span>
            {commit !== null && <span className={styles.commit}> · {shortCommit(commit)}</span>}
          </span>
        </button>
      </div>
    </div>
  )
}
