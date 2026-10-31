import * as Select from '@radix-ui/react-select'
import type { AnyRenderer } from 'fxtr-view'
import { Check, ChevronDown } from 'lucide-react'
import { CUSTOM_VIEW_ICON, KIND_ICON_STROKE } from '../lib/kindIcons'
import { RendererInfo } from '../views/RendererInfo'
import styles from './ViewSelect.module.css'

/** One view a `ViewSelect` offers. */
export interface ViewOption {
  /** Unique among the options, and not empty. */
  key: string
  label: string
  /** A renderer of the project's own (`isProjectRenderer`), listed under "from this project". */
  fromProject: boolean
  /** The renderer the view is, which its row's info icon names (`RendererInfo`). */
  renderer?: AnyRenderer
}

interface Props {
  options: readonly ViewOption[]
  /** The key of the view shown; one of the options'. */
  value: string
  onChange: (key: string) => void
}

/** The dropdown choosing how something is shown. Its trigger reads "views", with a badge counting
 *  the project's own views (behind the custom-view icon) when there are any, over the name of the
 *  view shown. The menu lists the viewer's views first, then the project's under a "from this project" heading carrying the
 *  custom-view icon, the one a step card shows when the step has views of the project's. A
 *  renderer's row ends in an info icon naming the component behind it. */
export function ViewSelect({ options, value, onChange }: Props) {
  const shown = options.find((option) => option.key === value)
  if (!shown) throw new Error(`no view ${value} among ${options.map((option) => option.key).join(', ')}`)
  const builtIn = options.filter((option) => !option.fromProject)
  const fromProject = options.filter((option) => option.fromProject)
  const CustomIcon = CUSTOM_VIEW_ICON
  const badge = fromProject.length > 0 && (
    <span className={styles.badge} title={`${fromProject.length} from this project`}>
      <CustomIcon size={9} strokeWidth={2} aria-hidden />
      {fromProject.length}
    </span>
  )
  return (
    <Select.Root value={value} onValueChange={onChange}>
      <Select.Trigger className={styles.trigger} aria-label="View">
        <span className={styles.stack}>
          <span className={styles.eyebrow}>
            views
            {badge}
          </span>
          <span className={styles.value}>
            <Select.Value>{shown.label}</Select.Value>
          </span>
        </span>
        <Select.Icon className={styles.chevron}>
          <ChevronDown size={11} />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className={styles.content} position="popper" side="bottom" align="end" sideOffset={4}>
          <Select.Viewport>
            {builtIn.length > 0 && <Select.Group>{builtIn.map(item)}</Select.Group>}
            {builtIn.length > 0 && fromProject.length > 0 && <Select.Separator className={styles.separator} />}
            {fromProject.length > 0 && (
              <Select.Group>
                <Select.Label className={styles.label}>
                  <CustomIcon size={11} strokeWidth={KIND_ICON_STROKE} />
                  from this project
                </Select.Label>
                {fromProject.map(item)}
              </Select.Group>
            )}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  )
}

function item(option: ViewOption) {
  return (
    <Select.Item key={option.key} value={option.key} className={styles.item}>
      <Select.ItemIndicator className={styles.indicator}>
        <Check size={11} />
      </Select.ItemIndicator>
      <Select.ItemText>{option.label}</Select.ItemText>
      {option.renderer && <RendererInfo renderer={option.renderer} />}
    </Select.Item>
  )
}
