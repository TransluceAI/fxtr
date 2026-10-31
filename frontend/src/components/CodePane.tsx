import { Suspense, useLayoutEffect, useRef, useState } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { Check, ChevronDown, Copy, SquareArrowOutUpRight } from 'lucide-react'
import { useFile, useSource } from '../api/resources'
import { codeLocation, editorHref, filePath, shortCommit } from '../lib/code'
import { cx } from '../lib/cx'
import { EDITORS, setEditor, useEditor } from '../lib/editor'
import type { SourceInfo } from '../types'
import { ErrorBoundary } from './ErrorBoundary'
import styles from './CodePane.module.css'

/**
 * The code pane: the file a step or workflow is implemented in, with its lines marked and scrolled
 * to. When the level's code version records the function (`codeVersion`), the file is the one at
 * the commit the job ran, so it is the code that ran even if the working tree has changed since;
 * otherwise it is the working tree's. Plain text, with line numbers, in a box of its own height
 * that scrolls inside the panel.
 */
export function CodeBody({ name, codeVersion }: { name: string; codeVersion: string | null }) {
  return (
    <ErrorBoundary fallback={(error) => <span className="detail-none">code unavailable: {error.message}</span>}>
      <Suspense fallback={<span className="detail-none">loading code…</span>}>
        <CodeFile name={name} codeVersion={codeVersion} />
      </Suspense>
    </ErrorBoundary>
  )
}

function CodeFile({ name, codeVersion }: { name: string; codeVersion: string | null }) {
  const source = useSource(name, codeVersion)
  if (!source) return <span className="detail-none">source not indexed</span>
  return <Lines source={source} />
}

function Lines({ source }: { source: SourceInfo }) {
  const file = useFile(source.file, source.commit)
  const body = useRef<HTMLDivElement>(null)
  const where = useRef<HTMLDivElement>(null)
  const first = useRef<HTMLDivElement>(null)
  // Scrolls the file's own box alone, not the columns around the panel, to the function's first
  // line under the sticky line naming the file.
  useLayoutEffect(() => {
    if (!body.current || !first.current) return
    body.current.scrollTop = first.current.offsetTop - (where.current?.offsetHeight ?? 0)
  }, [source])
  const lines = file.text.replace(/\n$/, '').split('\n')
  const marked = (n: number) => n >= source.startLine && n <= source.endLine
  return (
    <div className={styles.body} ref={body}>
      <div className={styles.where} ref={where}>
        {codeLocation(source)}
        {source.commit !== null && <span title={source.commit}> at commit {shortCommit(source.commit)}</span>}
        {source.commit === null && <span> in the working tree</span>}
      </div>
      <pre className={styles.code}>
        {lines.map((line, i) => {
          const n = i + 1
          return (
            <div
              key={n}
              ref={n === source.startLine ? first : undefined}
              className={styles.line}
              data-marked={marked(n) || undefined}
            >
              <span className={styles.number}>{n}</span>
              <span className={styles.text}>{line || ' '}</span>
            </div>
          )
        })}
      </pre>
    </div>
  )
}

/**
 * In the code pane's head, at its right: a split button. "Open" opens the function in the editor
 * last chosen; the chevron's menu lists every editor, and choosing one opens the function there
 * and makes it the one "Open" uses (kept in the browser, `lib/editor.ts`). The menu also copies the
 * file's path. Editors open the working tree's file, whose lines may have moved since the job ran.
 */
export function OpenInEditor({ name, codeVersion }: { name: string; codeVersion: string | null }) {
  return (
    <ErrorBoundary fallback={() => null}>
      <Suspense fallback={null}>
        <OpenMenu name={name} codeVersion={codeVersion} />
      </Suspense>
    </ErrorBoundary>
  )
}

function OpenMenu({ name, codeVersion }: { name: string; codeVersion: string | null }) {
  const source = useSource(name, codeVersion)
  const editor = useEditor()
  const [open, setOpen] = useState(false)
  if (!source) return null
  const location = codeLocation(source)
  return (
    <div className={styles.split}>
      <a className={styles.open} href={editorHref(editor, source)} title={`Open ${location} in ${editor.label}`}>
        <SquareArrowOutUpRight size={12} />
        Open
      </a>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button className={styles.more} aria-label="Choose the editor to open in">
            <ChevronDown size={12} />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className={cx('popover', styles.menu)} side="bottom" align="end" sideOffset={4}>
            <ul className={styles.items} aria-label="Open in">
              {EDITORS.map((choice) => (
                <li key={choice.id}>
                  <a
                    className={styles.item}
                    href={editorHref(choice, source)}
                    aria-current={choice.id === editor.id ? 'true' : undefined}
                    onClick={() => {
                      setEditor(choice.id)
                      setOpen(false)
                    }}
                  >
                    <Check size={12} className={styles.check} aria-hidden />
                    {choice.label}
                  </a>
                </li>
              ))}
            </ul>
            <hr className={styles.separator} />
            <button
              className={styles.item}
              onClick={() => {
                void navigator.clipboard?.writeText(filePath(source))
                setOpen(false)
              }}
            >
              <Copy size={12} className={styles.icon} aria-hidden />
              Copy path
            </button>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  )
}
