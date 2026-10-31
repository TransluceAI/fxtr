import type { ReactElement, ReactNode } from 'react'
import * as HoverCard from '@radix-ui/react-hover-card'
import { cx } from '../lib/cx'

/** How long the pointer rests on a trigger before its preview opens, and how long after it leaves
 *  the preview closes, in ms. */
const OPEN_DELAY = 250
const CLOSE_DELAY = 80

/**
 * A preview card shown while the pointer rests on `trigger` (one element, which takes the hover
 * card's handlers). The card lets the pointer through (`hover-preview`): it closes as soon as the
 * pointer leaves the trigger, even onto the card, so it never stands in the way of what is behind
 * it. So nothing in it can be clicked, scrolled, or hovered.
 */
export function HoverPreview({ trigger, children, className, side, align, collisionPadding, open, onOpenChange }: {
  trigger: ReactElement
  children: ReactNode
  className?: string
  side: 'top' | 'bottom' | 'right'
  align: 'start' | 'end'
  collisionPadding?: number
  /** Controlled: whether it is open, and what it is told when the pointer would open or close it. */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}) {
  return (
    <HoverCard.Root openDelay={OPEN_DELAY} closeDelay={CLOSE_DELAY}
      {...(open === undefined ? {} : { open })} {...(onOpenChange === undefined ? {} : { onOpenChange })}>
      <HoverCard.Trigger asChild>{trigger}</HoverCard.Trigger>
      <HoverCard.Portal>
        <HoverCard.Content className={cx('hover-preview', className)} side={side} align={align} sideOffset={6}
          {...(collisionPadding === undefined ? {} : { collisionPadding })}>
          {children}
          <HoverCard.Arrow className="popover-arrow" />
        </HoverCard.Content>
      </HoverCard.Portal>
    </HoverCard.Root>
  )
}
