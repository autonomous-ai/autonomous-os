import { useEffect, useImperativeHandle, useRef, useState, type Ref } from 'react'
import { Columns2, Rows2, X } from 'lucide-react'
import type { Session } from '../shared/types'
import { SessionIdentity } from './SessionIdentity'
import { InteractiveSessionControls, SessionView } from './SessionView'
import {
  closePane,
  paneSessions,
  resizePane,
  restoreLayout,
  splitPane,
  type PaneLayout,
  type SplitDirection,
} from './split-layout'

export interface SessionWorkspaceHandle {
  revealPrimary(): void
}

export function SessionWorkspace({
  session,
  sessions,
  onActiveSessionChange,
  onError,
  onCloseTab,
  ref,
}: {
  ref?: Ref<SessionWorkspaceHandle>
  session: Session
  sessions: Session[]
  onActiveSessionChange: (tabId: string, sessionId: string) => void
  onError: (error: unknown) => void
  onCloseTab: () => void
}) {
  const storageKey = `buddy.panes.${session.id}`
  const allowed = sessions.filter(
    (item) =>
      !item.closed && item.projectId === session.projectId && item.worktreePath === session.worktreePath,
  )
  const [layout, setLayout] = useState<PaneLayout>(() => {
    try {
      const restored = restoreLayout(
        JSON.parse(localStorage.getItem(storageKey) ?? 'null'),
        new Set(allowed.map((item) => item.id)),
      )
      if (!restored) return { kind: 'leaf', sessionId: session.id }
      return paneSessions(restored).includes(session.id)
        ? restored
        : splitPane(restored, paneSessions(restored)[0], session.id, 'horizontal', crypto.randomUUID())
    } catch {
      return { kind: 'leaf', sessionId: session.id }
    }
  })
  const effectiveLayout = restoreLayout(layout, new Set(allowed.map((item) => item.id))) ??
    (allowed.some((item) => item.id === session.id) ? { kind: 'leaf' as const, sessionId: session.id } : null)
  const serializedLayout = JSON.stringify(effectiveLayout)
  const [focused, setFocused] = useState(session.id)
  const effectiveFocused = paneSessions(effectiveLayout).includes(focused)
    ? focused : paneSessions(effectiveLayout)[0]

  useEffect(() => {
    if (effectiveFocused) onActiveSessionChange(session.id, effectiveFocused)
  }, [session.id, effectiveFocused, onActiveSessionChange])

  const [pending, setPending] = useState(false)
  useImperativeHandle(
    ref,
    () => ({
      revealPrimary() {
        setLayout((current) =>
          paneSessions(current).includes(session.id)
            ? current
            : splitPane(current, paneSessions(current)[0], session.id, 'horizontal', crypto.randomUUID()),
        )
        setFocused(session.id)
      },
    }),
    [session.id],
  )
  const mounted = useRef(true)
  const actionPending = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    if (serializedLayout === 'null') localStorage.removeItem(storageKey)
    else localStorage.setItem(storageKey, serializedLayout)
  }, [serializedLayout, storageKey])
  const split = async (target: string, direction: SplitDirection) => {
    if (actionPending.current) return
    actionPending.current = true
    setPending(true)
    try {
      const created = await window.buddy.createSession({
        projectId: session.projectId,
        worktreePath: session.worktreePath,
        provider: 'terminal',
        title: `Terminal ${allowed.filter((item) => item.provider === 'terminal').length + 1}`,
      })
      if (mounted.current) {
        setLayout((current) => splitPane(current, target, created.id, direction, crypto.randomUUID()))
        setFocused(created.id)
      }
    } catch (error) {
      onError(error)
    } finally {
      actionPending.current = false
      if (mounted.current) setPending(false)
    }
  }
  const close = async (id: string) => {
    if (actionPending.current) return
    actionPending.current = true
    setPending(true)
    try {
      await window.buddy.closeSession(id)
      localStorage.removeItem(`buddy.draft.${id}`)
      const next = effectiveLayout ? closePane(effectiveLayout, id) : null
      if (next) localStorage.setItem(storageKey, JSON.stringify(next))
      else localStorage.removeItem(storageKey)
      if (!next || id === session.id) {
        onCloseTab()
        return
      }
      if (mounted.current) {
        setLayout(next)
        if (effectiveFocused === id) setFocused(paneSessions(next)[0])
      }
    } catch (error) {
      onError(error)
    } finally {
      actionPending.current = false
      if (mounted.current) setPending(false)
    }
  }
  const render = (node: PaneLayout): React.ReactNode => {
    if (node.kind === 'split')
      return (
        <SplitBranch
          key={node.id}
          node={node}
          onResize={(ratio) => setLayout((current) => resizePane(current, node.id, ratio))}
          first={render(node.first)}
          second={render(node.second)}
        />
      )
    const leafSession = allowed.find((item) => item.id === node.sessionId)
    return (
      <section
        key={node.sessionId}
        data-session-id={node.sessionId}
        className={`session-pane ${effectiveFocused === node.sessionId ? 'focused' : ''}`}
        aria-label={`Session pane ${leafSession?.title ?? 'Starting session'}`}
        onFocusCapture={() => setFocused(node.sessionId)}
        onPointerDown={() => setFocused(node.sessionId)}
      >
        <div className="session-pane-heading">
          <span className="session-pane-identity">{leafSession ? <SessionIdentity provider={leafSession.provider} title={leafSession.title} /> : 'Session unavailable'}</span>
          <div>
            {leafSession && (leafSession.provider === 'terminal' || leafSession.mode === 'interactive') && (
              <InteractiveSessionControls session={leafSession} onError={onError} />
            )}
            <button
              className="icon-button"
              aria-label={`Split right ${leafSession?.title ?? 'pane'}`}
              title="Split right with a new terminal"
              disabled={pending || !leafSession}
              onClick={() => void split(node.sessionId, 'horizontal')}
            >
              <Columns2 size={13} />
            </button>
            <button
              className="icon-button"
              aria-label={`Split down ${leafSession?.title ?? 'pane'}`}
              title="Split down with a new terminal"
              disabled={pending || !leafSession}
              onClick={() => void split(node.sessionId, 'vertical')}
            >
              <Rows2 size={13} />
            </button>
            <button
              className="icon-button"
              aria-label={`Close pane ${leafSession?.title ?? 'session'}`}
              title="Close session, stop its process, and remove it from the workspace"
              disabled={pending}
              onClick={() => void close(node.sessionId)}
            >
              <X size={13} />
            </button>
          </div>
        </div>
        {leafSession ? (
          <SessionView session={leafSession} onError={onError} focused={effectiveFocused === node.sessionId} />
        ) : (
          <div className="pane-unavailable">
            This session is no longer available. Close this pane to continue.
          </div>
        )}
      </section>
    )
  }
  return (
    <div className="session-workspace" aria-label="Split session workspace">
      {effectiveLayout ? render(effectiveLayout) : null}
    </div>
  )
}

function SplitBranch({
  node,
  first,
  second,
  onResize,
}: {
  node: Extract<PaneLayout, { kind: 'split' }>
  first: React.ReactNode
  second: React.ReactNode
  onResize: (ratio: number) => void
}) {
  const container = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)
  const horizontal = node.direction === 'horizontal'
  const move = (event: React.PointerEvent) => {
    if (!dragging.current || !container.current) return
    const bounds = container.current.getBoundingClientRect()
    onResize(
      horizontal
        ? (event.clientX - bounds.left) / bounds.width
        : (event.clientY - bounds.top) / bounds.height,
    )
  }
  return (
    <div ref={container} className={`pane-split ${node.direction}`}>
      <div className="pane-child" style={{ flex: `${node.ratio} 1 0` }}>
        {first}
      </div>
      <div
        className="pane-divider"
        role="separator"
        tabIndex={0}
        aria-label={`Resize ${horizontal ? 'columns' : 'rows'}`}
        aria-orientation={horizontal ? 'vertical' : 'horizontal'}
        aria-valuemin={15}
        aria-valuemax={85}
        aria-valuenow={Math.round(node.ratio * 100)}
        onPointerDown={(event) => {
          dragging.current = true
          event.currentTarget.setPointerCapture(event.pointerId)
          event.preventDefault()
        }}
        onPointerMove={move}
        onPointerUp={() => {
          dragging.current = false
        }}
        onPointerCancel={() => {
          dragging.current = false
        }}
        onKeyDown={(event) => {
          const decrease = horizontal ? 'ArrowLeft' : 'ArrowUp'
          const increase = horizontal ? 'ArrowRight' : 'ArrowDown'
          if (event.key === decrease || event.key === increase) {
            event.preventDefault()
            onResize(node.ratio + (event.key === increase ? 0.05 : -0.05))
          }
          if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault()
            onResize(event.key === 'Home' ? 0.15 : 0.85)
          }
        }}
      />
      <div className="pane-child" style={{ flex: `${1 - node.ratio} 1 0` }}>
        {second}
      </div>
    </div>
  )
}
