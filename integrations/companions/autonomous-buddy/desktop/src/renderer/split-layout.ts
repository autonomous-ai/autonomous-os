export type SplitDirection = 'horizontal' | 'vertical'
export type PaneLayout =
  | { kind: 'leaf'; sessionId: string }
  | {
      kind: 'split'
      id: string
      direction: SplitDirection
      ratio: number
      first: PaneLayout
      second: PaneLayout
    }

export function paneSessions(layout: PaneLayout | null): string[] {
  if (!layout) return []
  return layout.kind === 'leaf'
    ? [layout.sessionId]
    : [...paneSessions(layout.first), ...paneSessions(layout.second)]
}

export function splitPane(
  layout: PaneLayout,
  target: string,
  sessionId: string,
  direction: SplitDirection,
  splitId: string,
): PaneLayout {
  if (paneSessions(layout).includes(sessionId)) return layout
  if (layout.kind === 'leaf')
    return layout.sessionId === target
      ? {
          kind: 'split',
          id: splitId,
          direction,
          ratio: 0.5,
          first: layout,
          second: { kind: 'leaf', sessionId },
        }
      : layout
  return {
    ...layout,
    first: splitPane(layout.first, target, sessionId, direction, splitId),
    second: splitPane(layout.second, target, sessionId, direction, splitId),
  }
}

export function closePane(layout: PaneLayout, sessionId: string): PaneLayout | null {
  if (layout.kind === 'leaf') return layout.sessionId === sessionId ? null : layout
  const first = closePane(layout.first, sessionId)
  const second = closePane(layout.second, sessionId)
  return first && second ? { ...layout, first, second } : (first ?? second)
}

export function resizePane(layout: PaneLayout, splitId: string, ratio: number): PaneLayout {
  if (layout.kind === 'leaf') return layout
  if (layout.id === splitId)
    return { ...layout, ratio: Number.isFinite(ratio) ? Math.min(0.85, Math.max(0.15, ratio)) : layout.ratio }
  return {
    ...layout,
    first: resizePane(layout.first, splitId, ratio),
    second: resizePane(layout.second, splitId, ratio),
  }
}

// Treat saved UI data as untrusted: reject malformed nodes and prune deleted,
// duplicate, or foreign-workspace session IDs while retaining valid siblings.
export function restoreLayout(value: unknown, allowedSessions: Set<string>): PaneLayout | null {
  const seenSessions = new Set<string>()
  const seenSplits = new Set<string>()
  let remaining = 1000
  const read = (input: unknown, depth: number): PaneLayout | null => {
    if (!input || typeof input !== 'object' || --remaining < 0 || depth > 64) return null
    const node = input as Record<string, unknown>
    if (node.kind === 'leaf') {
      if (
        typeof node.sessionId !== 'string' ||
        !allowedSessions.has(node.sessionId) ||
        seenSessions.has(node.sessionId)
      )
        return null
      seenSessions.add(node.sessionId)
      return { kind: 'leaf', sessionId: node.sessionId }
    }
    if (
      node.kind !== 'split' ||
      typeof node.id !== 'string' ||
      seenSplits.has(node.id) ||
      !['horizontal', 'vertical'].includes(String(node.direction))
    )
      return null
    seenSplits.add(node.id)
    const first = read(node.first, depth + 1)
    const second = read(node.second, depth + 1)
    if (!first || !second) return first ?? second
    return {
      kind: 'split',
      id: node.id,
      direction: node.direction as SplitDirection,
      ratio:
        typeof node.ratio === 'number' && Number.isFinite(node.ratio)
          ? Math.min(0.85, Math.max(0.15, node.ratio))
          : 0.5,
      first,
      second,
    }
  }
  return read(value, 0)
}
