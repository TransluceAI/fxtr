/**
 * The toast a link raises when what it names cannot be shown: no such view, page, or step, a
 * relative location with nothing to start from. The columns open nothing for it (`panes/useColumns.ts`),
 * so this says why. A link names nothing like this only through a renderer's bug, so the toast shows
 * the address it named, for whoever writes the renderer. Repeated clicks update one toast.
 */

import { Link2Off } from 'lucide-react'
import { toast } from 'sonner'

const ID = 'link-problem'
/** How long the toast stays while the page is in view. */
const SHOWN_MS = 8_000

export function showLinkProblem(address: string, problem: string): void {
  toast('this link opens nothing', {
    id: ID,
    icon: <Link2Off size={14} aria-hidden />,
    description: (
      <>
        <code>{address}</code>: {problem}
      </>
    ),
    duration: SHOWN_MS,
  })
}
