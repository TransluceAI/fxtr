import { useId, useState } from 'react'
import { Search, X } from 'lucide-react'
import { cx } from '../lib/cx'
import styles from './JobPicker.module.css'

interface Props {
  query: string
  onQuery: (query: string) => void
  placeholder: string
  workflows: string[]
  /** The workflow the jobs are narrowed to; null for every workflow. */
  workflow: string | null
  onWorkflow: (workflow: string | null) => void
  /** Enter with no workflow highlighted. */
  onEnter: () => void
}

/**
 * The job picker's search box, which also narrows the jobs to one workflow. Clicking it, or typing
 * in it, lists the workflows whose names contain what is typed under it; choosing one narrows the
 * jobs to it, shows it in the box, and clears what was typed. Its `×`, or Backspace in an empty
 * box, widens them again.
 *
 * While the workflows are listed, the up and down arrows highlight one, Enter chooses it, and
 * Escape closes the list (the picker leaves an Escape to an open list: `JobPicker`). Otherwise the
 * arrows reach the picker, which steps from job to job, and Enter calls `onEnter`.
 */
export function JobSearch({ query, onQuery, placeholder, workflows, workflow, onWorkflow, onEnter }: Props) {
  const [open, setOpen] = useState(false)
  /** The workflow highlighted, by index into those listed; -1 for none. */
  const [active, setActive] = useState(-1)
  const id = useId()
  const typed = query.trim().toLowerCase()
  const listed = workflows.filter((name) => name !== workflow && name.toLowerCase().includes(typed))
  const shown = open && listed.length > 0
  const highlighted = shown && active >= 0 && active < listed.length ? active : -1

  const show = () => {
    setOpen(true)
    setActive(-1)
  }
  const choose = (name: string) => {
    onWorkflow(name)
    onQuery('')
    setOpen(false)
  }
  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (shown && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault()
      event.stopPropagation()
      const by = event.key === 'ArrowDown' ? 1 : -1
      setActive(Math.min(listed.length - 1, Math.max(0, highlighted + by)))
    } else if (event.key === 'Enter') {
      if (highlighted >= 0) choose(listed[highlighted])
      else onEnter()
    } else if (event.key === 'Escape' && shown) {
      setOpen(false)
    } else if (event.key === 'Backspace' && query === '' && workflow !== null) {
      onWorkflow(null)
    }
  }

  return (
    <div className={styles.searchBox}>
      <label className={styles.search}>
        <Search size={12} />
        {workflow !== null && (
          <span className={styles.searchWorkflow} title={`Showing only ${workflow}`}>
            <span className={styles.searchWorkflowName}>{workflow}</span>
            <button aria-label={`Show every workflow, not only ${workflow}`} onMouseDown={(event) => event.preventDefault()} onClick={() => onWorkflow(null)}>
              <X size={11} />
            </button>
          </span>
        )}
        <input
          autoFocus
          role="combobox"
          aria-expanded={shown}
          aria-controls={id}
          aria-autocomplete="list"
          aria-activedescendant={highlighted >= 0 ? `${id}-${highlighted}` : undefined}
          value={query}
          placeholder={workflow === null ? placeholder : 'Filter these jobs…'}
          onMouseDown={show}
          onChange={(event) => {
            onQuery(event.target.value)
            show()
          }}
          onBlur={() => setOpen(false)}
          onKeyDown={onKeyDown}
        />
      </label>
      {shown && (
        <div id={id} className={styles.suggestions} role="listbox" aria-label="Workflows">
          <div className={styles.suggestionsTitle}>Workflows</div>
          {listed.map((name, index) => (
            <div
              key={name}
              id={`${id}-${index}`}
              role="option"
              aria-selected={index === highlighted}
              className={cx(styles.suggestion, index === highlighted && styles.isChosen)}
              title={name}
              // Chosen on the press, before the box loses focus and closes the list.
              onMouseDown={(event) => {
                event.preventDefault()
                choose(name)
              }}
              onMouseEnter={() => setActive(index)}
            >
              {name}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
