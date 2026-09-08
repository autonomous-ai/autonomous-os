import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Bell,
  ChevronRight,
  Command,
  GitBranch,
  Layers,
  PanelLeft,
  Plus,
  Search,
  Settings2,
  Terminal,
  X,
  Check,
  Bot,
  FolderOpen,
  FileDiff,
  Monitor,
} from 'lucide-react'
import type { Project, Provider, Session, Snapshot, Worktree } from '../shared/types'
import { SessionWorkspace, type SessionWorkspaceHandle } from './SessionWorkspace'
import { GitPanel } from './GitPanel'
import { ComputerPanel } from './ComputerPanel'
import { WorkspaceSidebar } from './WorkspaceSidebar'
import { ProviderUsageBar } from './ProviderUsageBar'
import { SettingsPage } from './SettingsPage'
import { useAppearance } from './useAppearance'

export { providerName } from './SessionIdentity'
import { providerName, SessionIdentity } from './SessionIdentity'
export const statusName = (session: Session) =>
  session.provider === 'terminal' && session.status === 'running'
    ? 'Shell active'
    : {
        idle: 'Ready',
        running: 'Working',
        needs_input: 'Needs attention',
        completed: 'Completed',
        error: 'Error',
        stopped: 'Stopped',
      }[session.status]
export function StatusDot({ session }: { session: Session }) {
  return <span className={`status-dot ${session.status}`} title={statusName(session)} />
}

type Selection = { projectId: string; path: string }
type Modal = 'session' | 'worktree' | 'computer' | null
const emptySnapshot: Snapshot = { projects: [], sessions: [], providers: [], workspaces: [] }

export function App() {
  const { appearance } = useAppearance()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const settingsOpenRef = useRef(false)
  const settingsReturnFocus = useRef<HTMLElement | null>(null)
  const restoreSettingsFocus = useRef(false)
  const openSettings = useCallback(() => {
    if (!settingsOpenRef.current) {
      settingsReturnFocus.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    }
    settingsOpenRef.current = true
    restoreSettingsFocus.current = false
    setSettingsOpen(true)
  }, [])
  const closeSettings = useCallback((restoreFocus = true) => {
    settingsOpenRef.current = false
    restoreSettingsFocus.current = restoreFocus
    setSettingsOpen(false)
  }, [])
  useEffect(() => {
    if (settingsOpen || !restoreSettingsFocus.current) return
    // Wait for the overlay to unmount and the workspace's inert attribute to clear.
    const frame = requestAnimationFrame(() => {
      restoreSettingsFocus.current = false
      const previous = settingsReturnFocus.current
      settingsReturnFocus.current = null
      if (!document.hasFocus() || document.querySelector('[role="dialog"]')) return
      const target = previous?.isConnected && previous !== document.body &&
        !previous.closest('[inert]') && !previous.matches(':disabled')
        ? previous
        : document.querySelector<HTMLElement>(
          '.session-pane.focused textarea, button[aria-label="Workspace settings"]',
        )
      target?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [settingsOpen])
  const [snapshot, setSnapshot] = useState<Snapshot>(emptySnapshot)
  const [loaded, setLoaded] = useState(false)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [trees, setTrees] = useState<Record<string, Worktree[]>>({})
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [focusedPane, setFocusedPane] = useState<{ tabId: string; sessionId: string } | null>(null)
  const reportFocusedPane = useCallback((tabId: string, sessionId: string) => {
    setFocusedPane((previous) => previous?.tabId === tabId && previous.sessionId === sessionId ? previous : { tabId, sessionId })
  }, [])
  const [closedTabs, setClosedTabs] = useState<string[]>(() => {
    try {
      const value: unknown = JSON.parse(localStorage.getItem('buddy.closedTabs') ?? '[]')
      return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : []
    } catch {
      return []
    }
  })
  const closedTabsRef = useRef(closedTabs)
  const migratedClosedTabs = useRef(false)
  useEffect(() => {
    closedTabsRef.current = closedTabs
    localStorage.setItem('buddy.closedTabs', JSON.stringify(closedTabs))
  }, [closedTabs])
  const [renameTab, setRenameTab] = useState<Session | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const [query, setQuery] = useState('')
  const [modal, setModal] = useState<Modal>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [attentionOnly, setAttentionOnly] = useState(false)
  const [preview, setPreview] = useState<{ name: string; text: string; diff: boolean } | null>(null)
  const [sidebarWidth, setSidebarWidth] = useState(280)
  const [gitWidth, setGitWidth] = useState(330)
  useEffect(
    () =>
      window.buddy.onOpenSettings(() => {
        setModal(null)
        setRenameTab(null)
        openSettings()
      }),
    [openSettings],
  )
  useEffect(() => {
    if (!loaded) return
    localStorage.setItem('buddy.selection', JSON.stringify({ selection, sessionId }))
  }, [loaded, selection, sessionId])
  const searchRef = useRef<HTMLInputElement>(null)
  const workspaceRef = useRef<SessionWorkspaceHandle>(null)
  const selectionRef = useRef(selection)
  const pendingFocusRef = useRef<string | null>(null)
  const initialFocusRef = useRef<string | null>(null)
  const sessionChooserRef = useRef<(session: Session) => void>(() => {})
  const resolveFocus = useCallback((value: Snapshot) => {
    const target = value.sessions.find((item) => !item.closed && item.id === pendingFocusRef.current)
    if (target) {
      pendingFocusRef.current = null
      sessionChooserRef.current(target)
    }
  }, [])
  useEffect(() => {
    selectionRef.current = selection
  }, [selection])
  const fail = useCallback(
    (error: unknown) => setError(error instanceof Error ? error.message : String(error)),
    [],
  )
  const refreshTrees = useCallback(async (project: Project) => {
    const found = await window.buddy.worktrees(project.id)
    setTrees((current) => ({ ...current, [project.id]: found }))
    return found
  }, [])

  useEffect(() => {
    let alive = true
    let received = false
    const unsubscribe = window.buddy.onUpdate((update) => {
      if (update.type === 'snapshot') {
        received = true
        setSnapshot({ ...update.snapshot, sessions: update.snapshot.sessions.filter((session) => !session.closed) })
        resolveFocus(update.snapshot)
      }
    })
    void window.buddy
      .snapshot()
      .then((value) => {
        if (!alive) return
        if (!received) setSnapshot({ ...value, sessions: value.sessions.filter((session) => !session.closed) })
        setLoaded(true)
        let remembered: { sessionId?: string; selection?: Selection } = {}
        try {
          remembered = JSON.parse(localStorage.getItem('buddy.selection') ?? '{}') ?? {}
        } catch {
          /* Ignore invalid local UI preferences. */
        }
        const openSessions = value.sessions.filter((session) => !session.closed && !closedTabsRef.current.includes(session.id))
        const rememberedProject = value.projects.find(
          (project) => project.id === remembered.selection?.projectId,
        )
        const first =
          value.sessions.find((session) => !session.closed && session.id === initialFocusRef.current) ??
          openSessions.find((session) => session.id === remembered.sessionId) ??
          (rememberedProject ? undefined : openSessions.at(-1))
        if (first) {
          setSelection({ projectId: first.projectId, path: first.worktreePath })
          setSessionId(first.id)
        } else if (rememberedProject && remembered.selection) {
          setSelection(remembered.selection)
        } else if (value.projects[0])
          setSelection({ projectId: value.projects[0].id, path: value.projects[0].path })
        resolveFocus(value)
      })
      .catch(fail)
    return () => {
      alive = false
      unsubscribe()
    }
  }, [fail, resolveFocus])

  const projectIds = snapshot.projects.map((project) => project.id).join(',')
  useEffect(() => {
    const refresh = () => {
      void window.buddy
        .snapshot()
        .then((value) => Promise.all(value.projects.map(refreshTrees)))
        .catch(fail)
    }
    refresh()
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [projectIds, refreshTrees, fail])

  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (settingsOpenRef.current) {
        if (event.key === 'Escape') closeSettings()
        return
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'k') {
        event.preventDefault()
        searchRef.current?.focus()
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'n') {
        event.preventDefault()
        if (selectionRef.current) setModal('session')
      }
      if (event.key === 'Escape') {
        setModal(null)
        setRenameTab(null)
        setPreview(null)
      }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [closeSettings])

  const project = snapshot.projects.find((item) => item.id === selection?.projectId)
  const worktree = project && trees[project.id]?.find((tree) => tree.path === selection?.path)
  const sessions = snapshot.sessions.filter(
    (item) => item.projectId === selection?.projectId && item.worktreePath === selection?.path,
  )
  const visibleSessions = sessions.filter((session) => !closedTabs.includes(session.id))
  const active = visibleSessions.find((item) => item.id === sessionId)
  const voiceSession = focusedPane?.tabId === active?.id
    ? sessions.find((item) => item.id === focusedPane?.sessionId && !item.closed) ?? active
    : active
  const voiceSessionId = voiceSession?.id
  useEffect(() => {
    if (!loaded) return
    void window.buddy.setActiveContext(selection ? {
      projectId: selection.projectId,
      worktreePath: selection.path,
      ...(voiceSessionId ? { sessionId: voiceSessionId } : {}),
    } : null).catch(fail)
  }, [loaded, selection, voiceSessionId, fail])

  const attention = snapshot.sessions.filter(
    (item) => item.unread || item.status === 'needs_input' || item.status === 'error',
  )

  const chooseSession = useCallback(
    (session: Session) => {
      if (session.id === active?.id) workspaceRef.current?.revealPrimary()
      setClosedTabs((current) => current.filter((id) => id !== session.id))
      setSelection({ projectId: session.projectId, path: session.worktreePath })
      setSessionId(session.id)
      setPreview(null)
      void window.buddy.markRead(session.id).catch(fail)
    },
    [active?.id, fail],
  )
  useEffect(() => {
    sessionChooserRef.current = chooseSession
  }, [chooseSession])
  useEffect(
    () =>
      window.buddy.onFocusSession((id) => {
        pendingFocusRef.current = id
        initialFocusRef.current = id
        resolveFocus(snapshot)
      }),
    [snapshot, resolveFocus],
  )
  const chooseTree = (projectId: string, tree: Worktree) => {
    setSelection({ projectId, path: tree.path })
    setPreview(null)
    const first = snapshot.sessions.find(
      (item) =>
        item.projectId === projectId && item.worktreePath === tree.path && !closedTabs.includes(item.id),
    )
    setSessionId(first?.id ?? null)
    if (first) void window.buddy.markRead(first.id).catch(fail)
  }
  useEffect(() => {
    if (!loaded || migratedClosedTabs.current) return
    migratedClosedTabs.current = true
    // Older versions only hid tabs. Honor those saved close intents on migration.
    const hidden = snapshot.sessions.filter((session) => closedTabs.includes(session.id))
    for (const session of hidden) {
      void window.buddy.closeSession(session.id).catch(fail)
    }
  }, [loaded, snapshot.sessions, closedTabs, fail])
  const closeTab = useCallback(
    (id: string) => {
      void window.buddy.closeSession(id).then(() => {
        localStorage.removeItem(`buddy.draft.${id}`)
        setClosedTabs((current) => [...new Set([...current, id])])
        if (sessionId === id) {
          const index = visibleSessions.findIndex((session) => session.id === id)
          const next = visibleSessions[index + 1] ?? visibleSessions[index - 1]
          setSessionId(next?.id ?? null)
        }
      }).catch(fail)
    },
    [sessionId, visibleSessions, fail],
  )
  const closeActiveTab = useCallback(() => {
    if (document.querySelector('[role="dialog"], [role="menu"]')) return
    if (preview) setPreview(null)
    else if (active) closeTab(active.id)
  }, [preview, active, closeTab])
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (
        (event.metaKey || event.ctrlKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === 'w'
      ) {
        event.preventDefault()
        closeActiveTab()
      }
    }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [closeActiveTab])
  useEffect(() => window.buddy.onCloseActiveTab(closeActiveTab), [closeActiveTab])
  const addProject = async () => {
    setBusy(true)
    try {
      const added = await window.buddy.addProject()
      if (added) {
        await refreshTrees(added)
        setSelection({ projectId: added.id, path: added.path })
        setSessionId(null)
        setPreview(null)
      }
    } catch (error) {
      fail(error)
    } finally {
      setBusy(false)
    }
  }
  const createSession = async (provider: Provider, title: string) => {
    if (!selection) return
    setBusy(true)
    try {
      const session = await window.buddy.createSession({
        projectId: selection.projectId,
        worktreePath: selection.path,
        provider,
        title,
      })
      chooseSession(session)
      setModal(null)
    } catch (error) {
      fail(error)
    } finally {
      setBusy(false)
    }
  }
  const resize = (event: React.PointerEvent, side: 'left' | 'right') => {
    const start = event.clientX,
      width = side === 'left' ? sidebarWidth : gitWidth
    event.currentTarget.setPointerCapture(event.pointerId)
    const move = (moveEvent: PointerEvent) => {
      const delta = (moveEvent.clientX - start) * (side === 'left' ? 1 : -1)
      if (side === 'left') setSidebarWidth(Math.max(190, Math.min(390, width + delta)))
      else setGitWidth(Math.max(260, Math.min(550, width + delta)))
    }
    const end = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', end)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', end, { once: true })
  }

  return (
    <div
      className="app-shell"
      style={
        {
          '--sidebar-width': `${sidebarOpen ? sidebarWidth : 0}px`,
          '--git-width': `${gitWidth}px`,
        } as React.CSSProperties
      }
    >
      <header className="titlebar">
        <div className="brand">
          <span className="brand-name">
            Autonomous <strong>Buddy</strong>
          </span>
          <button
            className="icon-button"
            aria-label="Toggle sidebar"
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            <PanelLeft size={15} />
          </button>
        </div>
        <div className="title-path">
          <GitBranch size={13} />
          <span>{project?.name ?? 'Agent workspace'}</span>
          {worktree && (
            <>
              <ChevronRight size={12} />
              <span>{worktree.branch || 'Detached HEAD'}</span>
            </>
          )}
        </div>
        <button className="command-button" onClick={() => searchRef.current?.focus()}>
          <Search size={13} /> Search <kbd>⌘ K</kbd>
        </button>
      </header>
      <div className="workspace" inert={settingsOpen}>
        {sidebarOpen && (
          <>
            <aside className="sidebar">
              <div className="search-field">
                <Search size={14} />
                <input
                  ref={searchRef}
                  aria-label="Search projects and sessions"
                  placeholder="Search projects, sessions…"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
                <kbd>⌘K</kbd>
              </div>
              <nav className="navigation">
                <button
                  className={!attentionOnly ? 'nav-item selected' : 'nav-item'}
                  onClick={() => setAttentionOnly(false)}
                >
                  <Layers size={15} /> Workspace <span>{snapshot.sessions.length}</span>
                </button>
                <button
                  className={attentionOnly ? 'nav-item selected' : 'nav-item'}
                  onClick={() => setAttentionOnly(true)}
                >
                  <Bell size={15} /> Needs attention{' '}
                  {attention.length > 0 && <span className="count">{attention.length}</span>}
                </button>
                <button className="nav-item" onClick={() => setModal('computer')}>
                  <Monitor size={15} /> Computer & device
                </button>
              </nav>
              <div className="section-label">
                <span>{attentionOnly ? 'ATTENTION' : 'PROJECTS'}</span>
                <div>
                  <button
                    className="icon-button"
                    aria-label="Refresh projects"
                    onClick={() => snapshot.projects.forEach((item) => void refreshTrees(item).catch(fail))}
                  >
                    <Settings2 size={13} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label="Add project"
                    disabled={busy}
                    onClick={() => void addProject()}
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
              <div className="project-list">
                {attentionOnly ? (
                  <>
                    {attention.map((session) => (
                      <button
                        key={session.id}
                        className="attention-row"
                        onClick={() => chooseSession(session)}
                      >
                        <StatusDot session={session} />
                        <span>
                          <strong>{session.title}</strong>
                          <small>
                            {statusName(session)} · {providerName(session.provider)}
                          </small>
                        </span>
                      </button>
                    ))}
                    {!attention.length && (
                      <p className="sidebar-hint">
                        All caught up.
                        <br />
                        Agent updates will appear here.
                      </p>
                    )}
                  </>
                ) : (
                  <WorkspaceSidebar
                    snapshot={snapshot}
                    trees={trees}
                    query={query}
                    selection={selection}
                    activeId={active?.id}
                    onChooseTree={chooseTree}
                    onChooseSession={chooseSession}
                    onNewSession={(projectId, tree) => {
                      chooseTree(projectId, tree)
                      setModal('session')
                    }}
                    onNewWorktree={(item) => {
                      setSelection({ projectId: item.id, path: item.path })
                      setSessionId(null)
                      setPreview(null)
                      setModal('worktree')
                    }}
                    onRefresh={refreshTrees}
                    onError={fail}
                  />
                )}
                {!snapshot.projects.length && (
                  <p className="sidebar-hint">
                    Your projects, worktrees
                    <br />
                    and agents. In one place.
                  </p>
                )}
              </div>
              <button className="open-project" disabled={busy} onClick={() => void addProject()}>
                <Plus size={15} /> Open project <kbd>local</kbd>
              </button>
              <footer className="sidebar-footer">
                <span className="buddy-monogram">a</span>
                <span>
                  Autonomous Buddy<small>Agent workspace</small>
                </span>
                <button
                  className="icon-button"
                  aria-label="Workspace settings"
                  onClick={openSettings}
                >
                  <Settings2 size={16} />
                </button>
              </footer>
            </aside>
            <div className="resize-handle" onPointerDown={(event) => resize(event, 'left')} />
          </>
        )}
        <main className="main-pane">
          <div className="tabbar">
            {visibleSessions.map((session) => (
              <div
                className={`session-tab-wrap ${session.id === active?.id && !preview ? 'active' : ''}`}
                key={session.id}
              >
                <button
                  className={`session-tab ${session.id === active?.id && !preview ? 'active' : ''}`}
                  onClick={() => chooseSession(session)}
                  onDoubleClick={() => {
                    setRenameTab(session)
                    setRenameTitle(session.title)
                  }}
                  title={`${providerName(session.provider)} · ${session.title} · ${statusName(session)} · Double-click to rename`}
                >
                  <StatusDot session={session} />
                  <SessionIdentity provider={session.provider} title={session.title} />
                </button>
                <button
                  className="icon-button tab-close"
                  aria-label={`Close tab ${session.title}`}
                  title="Close session (⌘W), stop its process, and remove it from the workspace."
                  onClick={() => closeTab(session.id)}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
            {preview && (
              <button className="session-tab active preview-tab" onClick={() => setPreview(null)}>
                <FileDiff size={13} />
                <span>{preview.name.split('/').at(-1)}</span>
                <X size={13} />
              </button>
            )}
            <button
              className="icon-button new-tab"
              aria-label="New session"
              disabled={!selection}
              onClick={() => setModal('session')}
            >
              <Plus size={16} />
            </button>
            <span className="tabbar-space" />
            <span className="local-label">
              <span /> Local
            </span>
          </div>
          {preview ? (
            <div className="file-preview">
              <div className="pane-heading">
                <span>
                  {preview.diff ? 'Changes' : 'File'} / {preview.name}
                </span>
                <button
                  className="icon-button"
                  aria-label="Close file preview"
                  onClick={() => setPreview(null)}
                >
                  <X size={15} />
                </button>
              </div>
              <pre>
                {preview.text.split('\n').map((line, i) => (
                  <div
                    key={i}
                    className={
                      preview.diff
                        ? line.startsWith('+')
                          ? 'diff-add'
                          : line.startsWith('-')
                            ? 'diff-remove'
                            : line.startsWith('@@')
                              ? 'diff-hunk'
                              : ''
                        : ''
                    }
                  >
                    <span className="line-number">{i + 1}</span>
                    {line || ' '}
                  </div>
                ))}
              </pre>
            </div>
          ) : active ? (
            <SessionWorkspace
              onActiveSessionChange={reportFocusedPane}
              ref={workspaceRef}
              key={active.id}
              session={active}
              sessions={sessions}
              onError={fail}
              onCloseTab={() => closeTab(active.id)}
            />
          ) : (
            <div className="welcome">
              <div className="welcome-logo">
                <Command size={30} strokeWidth={1.4} />
              </div>
              <div className="eyebrow">YOUR WORK. YOUR AGENTS.</div>
              <h1>{project ? 'Make room for your next idea.' : 'A workspace for getting things done.'}</h1>
              <p>
                {project
                  ? 'Start an agent session or open a terminal in this worktree.'
                  : 'Open a local project to bring your agents, terminals and changes together.'}
              </p>
              <div className="welcome-actions">
                <button
                  className="primary-button"
                  disabled={busy || !loaded}
                  onClick={() => (project ? setModal('session') : void addProject())}
                >
                  {project ? <Plus size={16} /> : <FolderOpen size={16} />}
                  {project ? 'New session' : 'Open project'}
                </button>
                {project && (
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() => void createSession('terminal', '')}
                  >
                    <Terminal size={15} /> Open terminal
                  </button>
                )}
              </div>
              <div className="welcome-details">
                <span>
                  <GitBranch size={14} /> Isolated worktrees
                </span>
                <span>
                  <Bot size={14} /> Your agent subscriptions
                </span>
                <span>
                  <Check size={14} /> Local-first
                </span>
              </div>
            </div>
          )}
        </main>
        <div className="resize-handle" onPointerDown={(event) => resize(event, 'right')} />
        <GitPanel
          key={`${selection?.projectId}:${selection?.path}`}
          project={project}
          path={selection?.path}
          onPreview={setPreview}
          onError={fail}
        />
      </div>
      {appearance.showUsage && <ProviderUsageBar />}
      {appearance.showStatusBar && (
        <footer className="statusbar">
          <span className="statusbar-left">
            <span className="connection-dot" /> Local workspace{' '}
            {worktree && (
              <>
                <GitBranch size={12} />
                {worktree.branch || 'Detached HEAD'}
              </>
            )}
          </span>
          <span>
            {active ? (
              <>
                <StatusDot session={active} />
                {providerName(active.provider)}
                <span className="status-separator">/</span>
                {statusName(active)}
              </>
            ) : (
              'Ready when you are'
            )}
            <span className="status-separator">·</span>
            {snapshot.sessions.filter((session) => session.processActive ?? session.status === 'running').length} active
          </span>
        </footer>
      )}
      {settingsOpen && (
        <SettingsPage
          providers={snapshot.providers}
          project={project}
          onClose={() => closeSettings()}
          onComputer={() => {
            closeSettings(false)
            setModal('computer')
          }}
          onRemoveProject={async () => {
            if (!project) return
            await window.buddy.removeProject(project.id)
            setSelection(null)
            setSessionId(null)
            setPreview(null)
          }}
        />
      )}
      {renameTab && (
        <div className="modal-backdrop">
          <section className="modal" role="dialog" aria-modal="true" aria-label="Rename session">
            <button
              className="icon-button modal-close"
              aria-label="Close dialog"
              onClick={() => setRenameTab(null)}
            >
              <X size={16} />
            </button>
            <h2>Rename session</h2>
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void window.buddy
                  .renameSession(renameTab.id, renameTitle.trim())
                  .then(() => setRenameTab(null))
                  .catch(fail)
              }}
            >
              <label htmlFor="tab-session-title">Session name</label>
              <input
                id="tab-session-title"
                autoFocus
                value={renameTitle}
                maxLength={120}
                onChange={(event) => setRenameTitle(event.target.value)}
              />
              <button className="primary-button full-width" type="submit" disabled={!renameTitle.trim()}>
                Rename session
              </button>
            </form>
          </section>
        </div>
      )}
      {error && (
        <div className="error-toast" role="alert">
          <span>{error}</span>
          <button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}>
            <X size={16} />
          </button>
        </div>
      )}
      {modal && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setModal(null)
          }}
        >
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label={
              modal === 'session'
                ? 'New session'
                : modal === 'worktree'
                  ? 'New worktree'
                  : 'Computer & device'
            }
          >
            <button
              className="icon-button modal-close"
              aria-label="Close dialog"
              onClick={() => setModal(null)}
            >
              <X size={18} />
            </button>
            {modal === 'session' ? (
              <NewSession providers={snapshot.providers} busy={busy} onCreate={createSession} />
            ) : modal === 'worktree' ? (
              <NewWorktree
                busy={busy}
                onCreate={async (branch) => {
                  if (!project) return
                  setBusy(true)
                  try {
                    const created = await window.buddy.createWorktree(project.id, branch)
                    await refreshTrees(project)
                    chooseTree(project.id, created)
                    setModal(null)
                  } catch (error) {
                    fail(error)
                  } finally {
                    setBusy(false)
                  }
                }}
              />
            ) : (
              <ComputerPanel onError={fail} />
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function NewSession({
  providers,
  busy,
  onCreate,
}: {
  providers: Snapshot['providers']
  busy: boolean
  onCreate: (provider: Provider, title: string) => Promise<void>
}) {
  const [provider, setProvider] = useState<Provider>(
    providers.find((item) => item.available && item.id !== 'terminal')?.id ?? 'terminal',
  )
  const [title, setTitle] = useState('')
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        void onCreate(provider, title)
      }}
    >
      <div className="dialog-icon">
        <Bot size={24} />
      </div>
      <h2>Start a new session</h2>
      <p>Choose an agent. Keep the whole conversation in one place.</p>
      <label>Agent</label>
      <div className="provider-options">
        {(['codex', 'claude', 'terminal'] as Provider[]).map((id) => {
          const available = providers.some((item) => item.id === id && item.available)
          return (
            <button
              key={id}
              type="button"
              className={provider === id ? 'provider-option chosen' : 'provider-option'}
              disabled={!available}
              onClick={() => setProvider(id)}
            >
              {id === 'terminal' ? <Terminal size={20} /> : <Bot size={20} />}
              <strong>{providerName(id)}</strong>
              <small>
                {available ? (id === 'terminal' ? 'Interactive shell' : 'Connected CLI') : 'Not installed'}
              </small>
            </button>
          )
        })}
      </div>
      <label htmlFor="session-title">
        Session name <span className="muted">optional</span>
      </label>
      <input
        autoFocus
        id="session-title"
        placeholder="What are you working on?"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        maxLength={120}
      />
      <button className="primary-button full-width" disabled={busy} type="submit">
        <Plus size={16} />
        {busy ? 'Starting…' : 'Create session'}
      </button>
      <small className="dialog-footnote">⌘ N to start a session · Escape to close</small>
    </form>
  )
}
function NewWorktree({ busy, onCreate }: { busy: boolean; onCreate: (branch: string) => Promise<void> }) {
  const [branch, setBranch] = useState('')
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        void onCreate(branch.trim())
      }}
    >
      <div className="dialog-icon">
        <GitBranch size={24} />
      </div>
      <h2>Create a worktree</h2>
      <p>A separate branch and folder for your next task. Existing work stays in place.</p>
      <label htmlFor="branch-name">New branch name</label>
      <input
        autoFocus
        id="branch-name"
        value={branch}
        onChange={(event) => setBranch(event.target.value)}
        placeholder="feat/my-next-idea"
        maxLength={150}
        required
      />
      <button className="primary-button full-width" type="submit" disabled={busy || !branch.trim()}>
        <GitBranch size={16} />
        {busy ? 'Creating…' : 'Create worktree'}
      </button>
    </form>
  )
}
