import { describe, expect, it } from 'vitest'
import {
  closePane,
  paneSessions,
  resizePane,
  restoreLayout,
  splitPane,
  type PaneLayout,
} from '../src/renderer/split-layout'

const primary: PaneLayout = { kind: 'leaf', sessionId: 'agent' }
describe('split workspace layout', () => {
  it('supports nested horizontal and vertical panes without duplicating session views', () => {
    const columns = splitPane(primary, 'agent', 'terminal-one', 'horizontal', 'columns')
    const nested = splitPane(columns, 'terminal-one', 'terminal-two', 'vertical', 'rows')
    const fourth = splitPane(nested, 'agent', 'terminal-three', 'horizontal', 'nested-columns')
    expect(paneSessions(fourth)).toEqual(['agent', 'terminal-three', 'terminal-one', 'terminal-two'])
    expect(splitPane(fourth, 'agent', 'terminal-two', 'vertical', 'duplicate')).toBe(fourth)
  })
  it('closing a leaf collapses only its empty branch, retaining other sessions', () => {
    const columns = splitPane(primary, 'agent', 'terminal-one', 'horizontal', 'columns')
    const nested = splitPane(columns, 'terminal-one', 'terminal-two', 'vertical', 'rows')
    const closed = closePane(nested, 'terminal-one')
    expect(closed).toEqual({ ...columns, second: { kind: 'leaf', sessionId: 'terminal-two' } })
    expect(closePane(primary, 'agent')).toBeNull()
    expect(closePane(primary, 'missing')).toBe(primary)
  })
  it('restores valid sessions while pruning foreign, deleted, and duplicated leaves', () => {
    const nested = splitPane(
      splitPane(primary, 'agent', 'foreign', 'horizontal', 'columns'),
      'foreign',
      'terminal',
      'vertical',
      'rows',
    )
    expect(paneSessions(restoreLayout(nested, new Set(['agent', 'terminal'])))).toEqual(['agent', 'terminal'])
    expect(
      restoreLayout(
        {
          kind: 'split',
          id: 'duplicate',
          direction: 'horizontal',
          ratio: 0.5,
          first: primary,
          second: primary,
        },
        new Set(['agent']),
      ),
    ).toEqual(primary)
    expect(restoreLayout({ kind: 'split', direction: 'invalid' }, new Set(['agent']))).toBeNull()
  })
  it('persists resize geometry, clamps unsafe values, and preserves siblings', () => {
    const layout = splitPane(primary, 'agent', 'terminal', 'horizontal', 'columns')
    const resized = resizePane(layout, 'columns', 0.7)
    expect(restoreLayout(JSON.parse(JSON.stringify(resized)), new Set(['agent', 'terminal']))).toEqual(
      resized,
    )
    expect(resizePane(layout, 'columns', 99)).toMatchObject({ ratio: 0.85 })
    expect(resizePane(layout, 'columns', -99)).toMatchObject({ ratio: 0.15 })
    expect(resizePane(layout, 'columns', NaN)).toMatchObject({ ratio: 0.5 })
  })
})
