// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { EntityID, declarePage, page, useEntityLinks, useLink } from 'fxtr-view'
import { dispatchRegistry } from 'fxtr-view-shell-util'
import { FrontendViewShellProvider } from '../src/views/FrontendViewShellProvider'
import { OpenEntityContext, type EntityOpener } from '../src/lib/openEntity'
import type { LinkTarget } from '../src/panes/drag'

function Mark({ cid }: { cid: string }) {
  const links = useEntityLinks()
  const id = new EntityID(cid)
  return <button aria-pressed={links.isOpen(id)} onClick={() => links.open(id)}>{cid}</button>
}

it("opens a renderer's marks through the nearest opener, and says which one a pane shows", () => {
  const opened: string[] = []
  const cidOf = (target: LinkTarget) => {
    const location = target.rel.location
    if (location.kind !== 'absolute' || location.location?.root !== 'entity') throw new Error('not an entity link')
    return location.location.cid
  }
  const opener: EntityOpener = {
    originId: 'trace', viewId: null, relative: false,
    follow: (target) => opened.push(cidOf(target)),
    hover: () => {},
    linkState: (target) => ({ open: cidOf(target) === 'shown', selected: false }),
  }
  render(
    <OpenEntityContext.Provider value={opener}>
      <FrontendViewShellProvider registry={dispatchRegistry([])}>
        <Mark cid="shown" />
        <Mark cid="other" />
      </FrontendViewShellProvider>
    </OpenEntityContext.Provider>,
  )
  expect(screen.getByText('shown').getAttribute('aria-pressed')).toBe('true')
  expect(screen.getByText('other').getAttribute('aria-pressed')).toBe('false')
  fireEvent.click(screen.getByText('other'))
  expect(opened).toEqual(['other'])
})

const transcript = declarePage('test.transcript', { params: ['event'] })

function PageLink() {
  const link = useLink(transcript, { event: '42.61' })
  return link === null ? <span>no link</span> : <button {...link.props}>42.61</button>
}

it('links to a page with no location, from a panel with a location and from where links have none', () => {
  for (const relative of [true, false]) {
    const followed: LinkTarget[] = []
    const opener: EntityOpener = {
      originId: 'panel', viewId: null, relative,
      follow: (target) => followed.push(target),
      hover: () => {},
      linkState: () => ({ open: false, selected: false }),
    }
    const { unmount } = render(
      <OpenEntityContext.Provider value={opener}>
        <FrontendViewShellProvider registry={dispatchRegistry([page(transcript, () => null)])}>
          <PageLink />
        </FrontendViewShellProvider>
      </OpenEntityContext.Provider>,
    )
    fireEvent.click(screen.getByText('42.61'))
    expect(followed).toEqual([{ rel: { view: 'test.transcript', location: { kind: 'absolute', location: null } }, params: { event: '42.61' } }])
    unmount()
  }
})
