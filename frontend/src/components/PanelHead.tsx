import * as HoverCard from '@radix-ui/react-hover-card'
import { ArrowLeft, ArrowRight, FileCode2, Pin, X, type LucideIcon } from 'lucide-react'
import { OpenInEditor } from './CodePane'
import { StepControls, isCustomView } from './StepPanel'
import { setPanelDrag } from '../panes/drag'
import { useRegistry } from 'fxtr-view-shell-util'
import { addressParts, formatRel, formatValue, sharedParts, type Address, type AddressPart, type Params } from '../panes/address'
import { isRoot, type PanelInstance } from '../panes/columns'
import { SLICE, type ColumnsState, type PanelState } from '../panes/useColumns'
import { isPage, type Target } from '../panes/resolve'
import type { NodeKind } from '../types'
import { isReference } from '../model/schema'
import { EntityLabel } from '../views/EntityLabel'
import { CUSTOM_VIEW_ICON, ENTITY_ICON, INVOCATION_ICON, KIND_ICONS, KIND_ICON_STROKE } from '../lib/kindIcons'
import { isProjectRendererId } from '../views/registry'
import { cx } from '../lib/cx'
import styles from './PanelHead.module.css'

interface Props {
  panel: PanelInstance
  columns: ColumnsState
  /** Show the panel's step through another view. */
  onView: (view: string) => void
}

/**
 * A panel's head: one line that is the panel's address, typeset (`PanelPath`), then its params,
 * then its controls. Dragged, it moves the panel; Option-clicked (Alt), it collapses the panel to
 * the head or expands it again. A plain click does nothing.
 *
 * - The **path** draws each part of the resolved address (`addressParts`). The parts a browsing
 *   panel shares with the address of the panel before it in the sequence are dimmed and give way
 *   first when the column is narrow. The icon before a part says what that part names: the
 *   custom-view icon for a view of the project's own (a page, a renderer, a view within one), the
 *   step's kind for a node, the receipt for an invocation, the cube for an entity.
 * - The **params** the address uses: the panel's own as chips after the path (all of them on a
 *   line of their own under it when they do not fit beside it), and the ones from the level's
 *   slice only counted (`slice n`), since the slice control shows them. The ones it inherits from
 *   the panel before it are left out: that panel's head shows them.
 * - A pinned panel's head has a faint violet wash; a browsing panel's is the panel's own light
 *   grey. The pin button, first of the controls, pins the panel where it is. It shows on a pinned
 *   panel (filled violet, and no longer a button), and on a browsing one only while the pointer is
 *   over the panel or the button has keyboard focus.
 *
 * Hovering the path or the count opens the address card (`AddressCard`), which has the rest.
 */
export function PanelHead({ panel, columns, onView }: Props) {
  const state = columns.stateOf(panel.id)
  const { address, resolution } = state
  const target = resolution.target
  const { pinned } = panel
  const parentAddress = pinned || panel.prevId === null ? null : columns.stateOf(panel.prevId).address
  const registry = useRegistry()
  const page = (candidate: Address) => isPage(registry, candidate.view)
  return (
    <div
      className={cx('detail-head', styles.head, pinned && styles.isPinned)}
      draggable
      onDragStart={(event) => setPanelDrag(event.dataTransfer, panel.id)}
      onClick={(event) => {
        if (onHeadItself(event) && event.altKey && event.detail === 1) columns.toggleCollapsed(panel.id)
      }}
    >
      <span className={styles.main}>
        <AddressCard panel={panel} columns={columns} state={state}>
          <span className={styles.lead}>
            {address === null
              ? <span className={styles.unresolved}>{columns.nameOf(panel.id)}</span>
              : <PanelPath address={address} target={target} parentAddress={parentAddress} isPage={page} />}
            <SliceCount state={state} />
          </span>
        </AddressCard>
        <OwnParams state={state} />
      </span>
      <span className={styles.controls}>
        <button className={cx('icon-button', styles.pin, pinned && styles.isPinned)} aria-label="Pin" aria-pressed={pinned}
          disabled={pinned}
          title={pinned ? 'pinned: it changes only when you change it' : 'pin: keep this panel as it is; the panels after it follow it'}
          onClick={() => columns.pin(panel.id)}>
          <Pin size={13} fill={pinned ? 'currentColor' : 'none'} />
        </button>
        <button className="icon-button" aria-label="Back" title={backTitle(panel.back.at(-1))}
          disabled={panel.back.length === 0} onClick={() => columns.history(panel.id, -1)}>
          <ArrowLeft size={13} />
        </button>
        <button className="icon-button" aria-label="Forward" title={backTitle(panel.forward[0])}
          disabled={panel.forward.length === 0} onClick={() => columns.history(panel.id, 1)}>
          <ArrowRight size={13} />
        </button>
        {target?.kind === 'node' && <StepControls step={target.step} view={target.view} onView={onView} />}
        {target?.kind === 'code' && <OpenInEditor name={target.name} codeVersion={target.codeVersion} />}
        <button className="icon-button" aria-label="Close" title="close (Alt: close every panel)"
          onClick={(event) => (event.altKey ? columns.reset() : columns.close(panel.id))}>
          <X size={13} />
        </button>
      </span>
    </div>
  )
}

function backTitle(stored: PanelInstance['back'][number] | undefined): string {
  return stored ? `show ${formatRel(stored.rel, stored.own)}` : 'nothing to go to'
}

/** Whether a click landed on the head itself, not on a control in it, nor in a popover or menu
 *  opened from it (those are portalled elsewhere in the page, but their events bubble here). */
function onHeadItself(event: React.MouseEvent): boolean {
  const target = event.target as HTMLElement
  return event.currentTarget.contains(target) && !target.closest('button, input, select, a, [role="combobox"]')
}

/** How big the icon before a path's part is, in px. */
const PART_ICON_SIZE = 12
/** The longest a param's value is drawn in a chip, in characters; a longer one loses its middle. */
const VALUE_CHARS = 28

/**
 * The resolved address, part by part (`addressParts`), with `/` between. The parts also in the
 * parent's address (`sharedParts`) are dim, and all parts but the last shrink, ending in an
 * ellipsis, before the last one does. The field naming the part of an invocation's record (its
 * output, its inputs) is drawn as a facet.
 */
function PanelPath({ address, target, parentAddress, isPage }: {
  address: Address
  target: Target | null
  parentAddress: Address | null
  isPage: (address: Address) => boolean
}) {
  const parts = addressParts(address, isPage(address))
  const shared = parentAddress === null ? null : sharedParts(parts, addressParts(parentAddress, isPage(parentAddress)))
  const icon = partIcon(address, parts, target, isPage(address))
  const facet = address.location?.root === 'step' ? parts.findIndex((part) => part.kind === 'field') : -1
  const draw = (part: AddressPart, index: number) => (
    <span key={index} className={styles.part}>
      {index > 0 && <span className={styles.separator}>/</span>}
      {icon?.at === index && (
        <icon.Icon size={PART_ICON_SIZE} strokeWidth={KIND_ICON_STROKE} className={styles.partIcon} aria-label={icon.label} role="img" />
      )}
      <span className={cx(index === facet ? styles.facet : styles.name, shared !== null && index < shared && styles.isShared)}>
        {part.kind === 'entity' ? <EntityLabel cid={part.name} /> : part.name}
      </span>
    </span>
  )
  return (
    <span className={styles.path}>
      {parts.length > 1 && <span className={styles.leading}>{parts.slice(0, -1).map(draw)}</span>}
      <span className={styles.last}>{draw(parts.at(-1)!, parts.length - 1)}</span>
    </span>
  )
}

/** The icon before the part of a path naming what the panel shows, and which part that is: the
 *  last part of a view of the project's own (a page, a node, its output or the run through a
 *  renderer of the project's, or a view within one), the custom-view icon; else the last step of a
 *  node (its kind's icon, or the receipt for its invocation) or the run (the receipt for its
 *  invocation), the last part of a value whose schema is an entity reference, an entity shown
 *  whole, a function's code (the file). Nothing else has one. */
function partIcon(address: Address, parts: readonly AddressPart[], target: Target | null, page: boolean): { at: number; Icon: LucideIcon; label: string } | null {
  const custom = { at: parts.length - 1, Icon: CUSTOM_VIEW_ICON, label: 'custom view' }
  if (page) return custom
  if (target?.kind === 'code') return { at: parts.length - 1, Icon: FileCode2, label: 'code' }
  if ((target?.kind === 'node' || target?.kind === 'value') && isCustomView(target.view)) return custom
  if (target?.kind === 'run' && target.renderer !== null && isProjectRendererId(target.renderer)) return custom
  const steps = parts.flatMap((part, index) => (part.kind === 'step' || part.kind === 'run' ? [index] : []))
  const lastStep = steps.at(-1) ?? -1
  if (target?.kind === 'node' && lastStep >= 0) {
    const invocation = target.view.kind === 'invocation'
    return { at: lastStep, Icon: invocation ? INVOCATION_ICON : KIND_ICONS[target.step.kind], label: invocation ? 'invocation' : NODE_WORDS[target.step.kind] }
  }
  if (target?.kind === 'run' && lastStep >= 0) return { at: lastStep, Icon: INVOCATION_ICON, label: 'invocation' }
  if (target?.kind === 'value' && target.schema !== null && isReference(target.schema))
    return { at: parts.length - 1, Icon: ENTITY_ICON, label: 'entity' }
  if (address.location?.root === 'entity' && parts.length === 1) return { at: 0, Icon: ENTITY_ICON, label: 'entity' }
  return null
}

/** What a node's kind icon in a head means, in the address card's words. */
const NODE_WORDS: Record<NodeKind, string> = { step: 'step', workflow: 'workflow', source: 'source data' }

/** The params the address uses that the panel binds itself, and those from the level's slice. */
function usedBySource(state: PanelState): { own: string[]; slice: string[] } {
  const names = Object.keys(state.resolution.params).sort()
  const source = (name: string) => state.sources[name] ?? 'own'
  return {
    own: names.filter((name) => source(name) === 'own'),
    slice: names.filter((name) => source(name) === SLICE),
  }
}

/** How many params the address uses come from the level's slice (`slice n`). It has no native
 *  tooltip: the address card it opens lists them. */
function SliceCount({ state }: { state: PanelState }) {
  const { slice } = usedBySource(state)
  if (slice.length === 0) return null
  return (
    <span className={cx(styles.chip, styles.isSlice, styles.isFolded)} aria-label={`from the level's slice: ${slice.join(', ')}`}>
      slice {slice.length}
    </span>
  )
}

/** The panel's own params that its address uses, one chip each: beside the path when they all fit,
 *  else on a line of their own under it. */
function OwnParams({ state }: { state: PanelState }) {
  const { own } = usedBySource(state)
  if (own.length === 0) return null
  return (
    <span className={styles.own}>
      {own.map((name) => <ParamChip key={name} name={name} value={state.resolution.params[name]!} className={styles.isOwn} title={`${name}: this panel's own`} />)}
    </span>
  )
}

function ParamChip({ name, value, className, title, onMouseEnter, onMouseLeave, children }: {
  name: string
  value: Params[string]
  className?: string | false
  title: string
  onMouseEnter?: () => void
  onMouseLeave?: () => void
  children?: React.ReactNode
}) {
  const text = formatValue(value)
  return (
    <span className={cx(styles.chip, className)} title={`${title}\n${name}=${text}`} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave}>
      <span className={styles.chipName}>{name}</span>
      {elide(text, VALUE_CHARS)}
      {children}
    </span>
  )
}

/** `text`, its middle left out when longer than `chars`: the end usually tells values apart. */
function elide(text: string, chars: number): string {
  if (text.length <= chars) return text
  const tail = Math.floor((chars - 1) / 3)
  return `${text.slice(0, chars - 1 - tail)}…${text.slice(-tail)}`
}

/** Outline a panel (null: nothing) while something naming it is hovered. */
function outline(columns: ColumnsState, id: string | null) {
  columns.highlight.set(id === null || isRoot(id) ? null : { kind: 'existing', panelId: id })
}

/** How long the pointer rests on a path before its address card opens, and how long after it
 *  leaves (the trigger and the card) the card closes, in ms. */
const CARD_OPEN_DELAY = 450
const CARD_CLOSE_DELAY = 150

/**
 * The address card, opened by hovering `children` (the path and the slice count): what the panel
 * shows and where its params come from, a row each, rows with nothing in them left out.
 *
 * - **shows**: the icon before its path, and what it means (a step, a workflow, source data, an
 *   invocation, an entity, a custom view), when its path has one;
 * - **path**: its address as stored (a browsing panel's relative to the panel before it), and its
 *   own params (each one dropped with its ×: a browsing panel inherits it again, a pinned one
 *   leaves it unbound);
 * - **inherits** and **slice**: every binding from the panel before it (hovering outlines the
 *   panel that binds it) or the level's slice, dim when its address does not use it but passes it
 *   on to the panels following it.
 *
 * It stays open while the pointer is on it, so its buttons can be used.
 */
function AddressCard({ panel, columns, state, children }: { panel: PanelInstance; columns: ColumnsState; state: PanelState; children: React.ReactElement }) {
  return (
    <HoverCard.Root openDelay={CARD_OPEN_DELAY} closeDelay={CARD_CLOSE_DELAY}>
      <HoverCard.Trigger asChild>{children}</HoverCard.Trigger>
      <HoverCard.Portal>
        <HoverCard.Content className={cx('popover', styles.card)} side="bottom" align="start" sideOffset={6} collisionPadding={8}
          onDragStart={(event) => event.stopPropagation()}>
          <AddressRows panel={panel} columns={columns} state={state} />
          <HoverCard.Arrow className="popover-arrow" />
        </HoverCard.Content>
      </HoverCard.Portal>
    </HoverCard.Root>
  )
}

function AddressRows({ panel, columns, state }: { panel: PanelInstance; columns: ColumnsState; state: PanelState }) {
  const { resolution, sources } = state
  const env = resolution.env
  const used = new Set(Object.keys(resolution.params))
  const names = Object.keys(env).sort()
  const inherited = names.filter((name) => (sources[name] ?? 'own') !== 'own' && sources[name] !== SLICE)
  const slice = names.filter((name) => sources[name] === SLICE)
  const own = Object.keys(panel.own).sort()
  const passedOn = (name: string) => (used.has(name) ? '' : '\nits address does not use it; the panels following it inherit it')
  const registry = useRegistry()
  const page = state.address !== null && isPage(registry, state.address.view)
  const icon = state.address === null ? null : partIcon(state.address, addressParts(state.address, page), resolution.target, page)
  return (
    <div className={styles.rows}>
      {icon && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>shows</span>
          <span className={styles.rowValue}>
            <span className={styles.kind}>
              <icon.Icon size={PART_ICON_SIZE} strokeWidth={KIND_ICON_STROKE} className={styles.partIcon} aria-hidden />
              {icon.label}
            </span>
          </span>
          <span />
        </div>
      )}
      <div className={styles.row}>
        <span className={styles.rowLabel}>path</span>
        <span className={styles.rowValue}>
          <code className={styles.code}>{formatRel(panel.rel)}</code>
          {own.map((name) => (
            <ParamChip key={name} name={name} value={panel.own[name]!} className={cx(styles.isOwn, !used.has(name) && styles.isPassedOn)}
              title={`${name}: this panel's own${passedOn(name)}`}>
              <button className={styles.drop} aria-label={`Drop ${name}`}
                title={panel.pinned ? 'drop it: leave this param unbound' : 'drop it: inherit this param again'}
                onClick={() => {
                  const { [name]: _dropped, ...rest } = panel.own
                  columns.setOwn(panel.id, rest)
                }}>
                <X size={9} />
              </button>
            </ParamChip>
          ))}
        </span>
      </div>
      {inherited.length > 0 && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>inherits</span>
          <span className={styles.rowValue}>
            {inherited.map((name) => (
              <ParamChip key={name} name={name} value={env[name]!} className={cx(styles.isInherited, !used.has(name) && styles.isPassedOn)}
                title={`${name}: inherited from ${columns.nameOf(sources[name]!)}${passedOn(name)}`}
                onMouseEnter={() => outline(columns, sources[name]!)} onMouseLeave={() => outline(columns, null)} />
            ))}
          </span>
        </div>
      )}
      {slice.length > 0 && (
        <div className={styles.row}>
          <span className={styles.rowLabel}>slice</span>
          <span className={styles.rowValue}>
            {slice.map((name) => (
              <ParamChip key={name} name={name} value={env[name]!} className={cx(styles.isSlice, !used.has(name) && styles.isPassedOn)}
                title={`${name}: from the level's slice${passedOn(name)}`} />
            ))}
          </span>
        </div>
      )}
      {resolution.problem !== null && <p className={styles.problem}>{resolution.problem}</p>}
    </div>
  )
}
