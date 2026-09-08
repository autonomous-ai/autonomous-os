import { useEffect, useRef, useState } from 'react'
import {
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  FolderGit2,
  FolderOpen,
  GitBranch,
  MoreHorizontal,
  Moon,
  Pin,
  Plus,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import { createPortal } from 'react-dom'
import type { Project, Session, Snapshot, Worktree, WorkspaceMeta } from '../shared/types'

type Target = { project: Project; tree: Worktree; session?: Session }
type Menu = Target & { x: number; y: number }
type Dialog = {
  kind:
    | 'rename'
    | 'delete-session'
    | 'delete-worktree'
    | 'sleep'
    | 'update'
    | 'parent'
    | 'group'
    | 'move-group'
  target: Target
}
const statusLabels = { active: 'Active', review: 'In review', done: 'Done' }
const providerLabels = { codex: 'Codex', claude: 'Claude Code', terminal: 'Terminal' }
const sessionLabels = {
  idle: 'Ready',
  running: 'Working',
  needs_input: 'Needs attention',
  completed: 'Completed',
  error: 'Error',
  stopped: 'Stopped',
}

export function WorkspaceSidebar({
  snapshot,
  trees,
  query,
  selection,
  activeId,
  onChooseTree,
  onChooseSession,
  onNewSession,
  onNewWorktree,
  onRefresh,
  onError,
}: {
  snapshot: Snapshot
  trees: Record<string, Worktree[]>
  query: string
  selection: { projectId: string; path: string } | null
  activeId?: string
  onChooseTree: (projectId: string, tree: Worktree) => void
  onChooseSession: (session: Session) => void
  onNewSession: (projectId: string, tree: Worktree) => void
  onNewWorktree: (project: Project) => void
  onRefresh: (project: Project) => Promise<unknown>
  onError: (error: unknown) => void
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [menu, setMenu] = useState<Menu | null>(null)
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [pending, setPending] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const meta = (projectId: string, path: string) =>
    snapshot.workspaces.find((item) => item.projectId === projectId && item.worktreePath === path)
  useEffect(() => {
    if (!menu) return
    menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
    const close = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenu(null)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null)
    }
    window.addEventListener('pointerdown', close)
    window.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('pointerdown', close)
      window.removeEventListener('keydown', escape)
    }
  }, [menu])
  const run = (action: () => Promise<unknown>) => {
    setMenu(null)
    void action().catch(onError)
  }
  const openMenu = (event: React.MouseEvent, target: Target) => {
    event.preventDefault()
    setMenu({
      ...target,
      x: Math.min(event.clientX, window.innerWidth - 252),
      y: Math.max(8, Math.min(event.clientY, window.innerHeight - 520)),
    })
  }
  const hasUnread = (target: Target) =>
    !!meta(target.project.id, target.tree.path)?.unread ||
    snapshot.sessions.some(
      (session) =>
        session.projectId === target.project.id &&
        session.worktreePath === target.tree.path &&
        session.unread,
    )
  const toggleRead = (target: Target) =>
    run(async () => {
      const unread = hasUnread(target)
      if (unread)
        await Promise.all(
          snapshot.sessions
            .filter(
              (session) =>
                session.projectId === target.project.id &&
                session.worktreePath === target.tree.path &&
                session.unread,
            )
            .map((session) => window.buddy.markRead(session.id)),
        )
      await window.buddy.updateWorkspace(target.project.id, target.tree.path, { unread: !unread })
    })
  const update = (
    target: Target,
    value: Partial<
      Pick<WorkspaceMeta, 'pinned' | 'status' | 'unread' | 'displayName' | 'parentWorktreePath'>
    >,
  ) => run(() => window.buddy.updateWorkspace(target.project.id, target.tree.path, value))
  const confirm = (kind: Dialog['kind'], target: Target) => {
    setMenu(null)
    setDialog({ kind, target })
  }
  const completeDialog = async (title: string) => {
    if (!dialog) return
    setPending(true)
    try {
      const { project, tree, session } = dialog.target
      if (dialog.kind === 'group' || dialog.kind === 'move-group')
        await window.buddy.setProjectGroup(project.id, title.trim() || null)
      if (dialog.kind === 'update')
        await window.buddy.updateWorkspace(project.id, tree.path, { displayName: title.trim() })
      if (dialog.kind === 'parent')
        await window.buddy.updateWorkspace(project.id, tree.path, { parentWorktreePath: title || null })
      if (dialog.kind === 'rename' && session) await window.buddy.renameSession(session.id, title.trim())
      if (dialog.kind === 'delete-session' && session) {
        await window.buddy.removeSession(session.id)
        localStorage.removeItem(`buddy.draft.${session.id}`)
      }
      if (dialog.kind === 'delete-worktree') {
        await window.buddy.removeWorktree(project.id, tree.path)
        await onRefresh(project)
        onChooseTree(project.id, { path: project.path, branch: 'Primary', primary: true, head: '' })
      }
      if (dialog.kind === 'sleep') await window.buddy.sleepWorkspace(project.id, tree.path)
      setDialog(null)
    } catch (error) {
      onError(error)
    } finally {
      setPending(false)
    }
  }
  return (
    <>
      {[...snapshot.projects]
        .sort((a, b) => (a.group ?? '').localeCompare(b.group ?? ''))
        .map((project, projectIndex, projects) => {
          const projectSessions = snapshot.sessions.filter((session) => session.projectId === project.id)
          const needle = query.toLowerCase()
          const projectMatch = project.name.toLowerCase().includes(needle)
          const found = [
            ...(trees[project.id] ?? [
              { path: project.path, branch: 'Project folder', primary: true, head: '' },
            ]),
          ]
            .filter(
              (tree) =>
                projectMatch ||
                tree.branch.toLowerCase().includes(needle) ||
                (meta(project.id, tree.path)?.displayName ?? '').toLowerCase().includes(needle) ||
                projectSessions.some(
                  (session) =>
                    session.worktreePath === tree.path && session.title.toLowerCase().includes(needle),
                ),
            )
            .sort(
              (a, b) =>
                Number(meta(project.id, b.path)?.pinned ?? false) -
                Number(meta(project.id, a.path)?.pinned ?? false),
            )
          if (!found.length) return null
          const ordered: Worktree[] = []
          const visited = new Set<string>()
          const append = (tree: Worktree) => {
            if (visited.has(tree.path)) return
            visited.add(tree.path)
            ordered.push(tree)
            found
              .filter((child) => meta(project.id, child.path)?.parentWorktreePath === tree.path)
              .forEach(append)
          }
          found
            .filter(
              (tree) =>
                !found.some((parent) => parent.path === meta(project.id, tree.path)?.parentWorktreePath),
            )
            .forEach(append)
          found.forEach(append)
          return (
            <section className="project-group" key={project.id}>
              {project.group && projects[projectIndex - 1]?.group !== project.group && (
                <div className="project-group-label">
                  <FolderOpen size={12} />
                  {project.group}
                </div>
              )}
              <div className="project-heading">
                <FolderGit2 size={15} />
                <strong title={project.path}>{project.name}</strong>
                <span className="project-worktree-count">{found.length}</span>
                <button
                  className="icon-button project-add"
                  aria-label={`New worktree in ${project.name}`}
                  onClick={() => onNewWorktree(project)}
                >
                  <Plus size={14} />
                </button>
              </div>
              {ordered.map((tree) => {
                const key = `${project.id}:${tree.path}`
                const state = meta(project.id, tree.path)
                const sessions = projectSessions.filter((session) => session.worktreePath === tree.path)
                const agents = sessions.filter((session) => session.provider !== 'terminal')
                const running = sessions.filter((session) => session.status === 'running').length
                const attention = sessions.some(
                  (session) => session.status === 'needs_input' || session.status === 'error',
                )
                const unread = state?.unread || sessions.some((session) => session.unread)
                const selected = selection?.projectId === project.id && selection.path === tree.path
                const hidden = collapsed.has(key) && !query
                const target = { project, tree }
                return (
                  <div
                    className={`tree-card workspace-card ${selected ? 'active' : ''} ${unread ? 'has-unread' : ''} ${state?.parentWorktreePath ? 'workspace-child' : ''}`}
                    key={key}
                    onContextMenu={(event) => openMenu(event, target)}
                  >
                    <div className="workspace-card-heading">
                      <button
                        className="icon-button workspace-collapse"
                        aria-label={`${hidden ? 'Expand' : 'Collapse'} ${tree.branch || 'workspace'}`}
                        aria-expanded={!hidden}
                        onClick={() =>
                          setCollapsed((current) => {
                            const next = new Set(current)
                            if (next.has(key)) next.delete(key)
                            else next.add(key)
                            return next
                          })
                        }
                      >
                        {hidden ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                      </button>
                      <button
                        className="tree-select"
                        onClick={() => onChooseTree(project.id, tree)}
                        title={tree.path}
                      >
                        <GitBranch size={13} />
                        <span>{state?.displayName || tree.branch || tree.path.split('/').at(-1)}</span>
                        {state?.pinned && <Pin size={11} />}
                        {unread && <i className="unread" />}
                      </button>
                      <button
                        className="icon-button workspace-more"
                        aria-label={`Workspace actions for ${tree.branch || 'folder'}`}
                        aria-haspopup="menu"
                        onClick={(event) => openMenu(event, target)}
                      >
                        <MoreHorizontal size={15} />
                      </button>
                    </div>
                    <div className="tree-path" title={tree.path}>
                      {tree.branch || tree.path.split('/').slice(-2).join('/')}
                    </div>
                    {state?.parentWorktreePath && (
                      <div className="workspace-parent" title={state.parentWorktreePath}>
                        ↳{' '}
                        {meta(project.id, state.parentWorktreePath)?.displayName ||
                          found.find((parent) => parent.path === state.parentWorktreePath)?.branch ||
                          state.parentWorktreePath.split('/').at(-1)}
                      </div>
                    )}
                    <div className="workspace-summary">
                      <span>
                        {agents.length} {agents.length === 1 ? 'agent' : 'agents'}
                        {sessions.length !== agents.length &&
                          ` · ${sessions.length - agents.length} terminal`}
                      </span>
                      <span className={`workspace-stage ${state?.status ?? 'active'}`}>
                        {attention
                          ? 'Attention'
                          : running
                            ? `${running} running`
                            : statusLabels[state?.status ?? 'active']}
                      </span>
                      {tree.primary && <span className="primary-tag">primary</span>}
                    </div>
                    {!hidden && (
                      <div className="workspace-sessions">
                        {sessions
                          .filter(
                            (session) =>
                              projectMatch ||
                              tree.branch.toLowerCase().includes(needle) ||
                              (meta(project.id, tree.path)?.displayName ?? '')
                                .toLowerCase()
                                .includes(needle) ||
                              session.title.toLowerCase().includes(needle),
                          )
                          .map((session) => (
                            <div
                              className={`workspace-session ${activeId === session.id ? 'current' : ''}`}
                              key={session.id}
                              onContextMenu={(event) => {
                                event.stopPropagation()
                                openMenu(event, { ...target, session })
                              }}
                            >
                              <button
                                className="session-row"
                                onClick={() => onChooseSession(session)}
                                title={`${providerLabels[session.provider]} · ${sessionLabels[session.status]}`}
                              >
                                <span className={`status-dot ${session.status}`} />
                                {session.provider === 'terminal' ? <Terminal size={12} /> : <Bot size={12} />}
                                <span>{session.title}</span>
                                {session.unread && <i className="unread" />}
                              </button>
                              <button
                                className="icon-button session-more"
                                aria-label={`Session actions for ${session.title}`}
                                aria-haspopup="menu"
                                onClick={(event) => openMenu(event, { ...target, session })}
                              >
                                <MoreHorizontal size={13} />
                              </button>
                            </div>
                          ))}
                        <button className="inline-add" onClick={() => onNewSession(project.id, tree)}>
                          <Plus size={12} /> New session
                        </button>
                      </div>
                    )}
                  </div>
                )
              })}
            </section>
          )
        })}
      {menu &&
        createPortal(
          <div
            ref={menuRef}
            className="workspace-context-menu"
            role="menu"
            aria-label={menu.session ? 'Session actions' : 'Workspace actions'}
            style={{ left: menu.x, top: menu.y, maxHeight: window.innerHeight - menu.y - 8 }}
            onKeyDown={(event) => {
              if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
              event.preventDefault()
              const buttons = [
                ...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'),
              ]
              const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
              const next =
                event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? buttons.length - 1
                    : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
              buttons[next]?.focus()
            }}
          >
            <div className="context-heading">
              {menu.session ? 'Agent session' : 'Workspace'}
              <small>{menu.session?.title ?? menu.tree.branch}</small>
            </div>
            {menu.session ? (
              <>
                <button role="menuitem" onClick={() => confirm('rename', menu)}>
                  Rename session
                </button>
                <button
                  role="menuitem"
                  onClick={() => run(() => window.buddy.stop(menu.session!.id))}
                  disabled={!(menu.session.processActive ?? ['running', 'needs_input'].includes(menu.session.status))}
                >
                  <Moon size={14} /> Stop session
                </button>
                <button
                  role="menuitem"
                  className="danger-action"
                  disabled={menu.session.processActive ?? ['running', 'needs_input'].includes(menu.session.status)}
                  onClick={() => confirm('delete-session', menu)}
                >
                  <Trash2 size={14} /> Delete session
                </button>
              </>
            ) : (
              <>
                <button role="menuitem" onClick={() => confirm('update', menu)}>
                  Update workspace name
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(null)
                    onNewSession(menu.project.id, menu.tree)
                  }}
                >
                  <Plus size={14} /> New session
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(null)
                    onNewWorktree(menu.project)
                  }}
                >
                  <GitBranch size={14} /> New worktree
                </button>
                <div className="context-section">Move to status</div>
                {(Object.keys(statusLabels) as WorkspaceMeta['status'][]).map((status) => (
                  <button
                    role="menuitemradio"
                    aria-checked={(meta(menu.project.id, menu.tree.path)?.status ?? 'active') === status}
                    key={status}
                    onClick={() => update(menu, { status })}
                  >
                    <span className={`workspace-stage-dot ${status}`} />
                    {statusLabels[status]}
                    {(meta(menu.project.id, menu.tree.path)?.status ?? 'active') === status && (
                      <Check size={12} className="menu-check" />
                    )}
                  </button>
                ))}
                <div className="context-section">Open in</div>
                <button
                  role="menuitem"
                  onClick={() =>
                    run(() => window.buddy.openWorkspace(menu.project.id, menu.tree.path, 'finder'))
                  }
                >
                  <FolderOpen size={14} /> Finder
                </button>
                <button
                  role="menuitem"
                  onClick={() =>
                    run(() => window.buddy.openWorkspace(menu.project.id, menu.tree.path, 'terminal'))
                  }
                >
                  <Terminal size={14} /> Terminal
                </button>
                <button
                  role="menuitem"
                  onClick={() =>
                    run(() => window.buddy.openWorkspace(menu.project.id, menu.tree.path, 'vscode'))
                  }
                >
                  <FolderGit2 size={14} /> Visual Studio Code
                </button>
                <div className="context-divider" />
                <button
                  role="menuitem"
                  onClick={() => run(() => window.buddy.copyWorkspacePath(menu.project.id, menu.tree.path))}
                >
                  <Copy size={14} /> Copy path
                </button>
                <button
                  role="menuitem"
                  onClick={() => update(menu, { pinned: !meta(menu.project.id, menu.tree.path)?.pinned })}
                >
                  <Pin size={14} />
                  {meta(menu.project.id, menu.tree.path)?.pinned ? 'Unpin' : 'Pin'}
                </button>
                <button role="menuitem" onClick={() => toggleRead(menu)}>
                  {hasUnread(menu) ? 'Mark read' : 'Mark unread'}
                </button>
                <button role="menuitem" onClick={() => confirm('group', menu)}>
                  New group from project
                </button>
                <button role="menuitem" onClick={() => confirm('move-group', menu)}>
                  Move project to group…
                </button>
                <button role="menuitem" onClick={() => confirm('parent', menu)}>
                  Set parent worktree…
                </button>
                <button role="menuitem" onClick={() => confirm('sleep', menu)}>
                  <Moon size={14} /> Sleep workspace
                </button>
                <div className="context-divider" />
                <button
                  role="menuitem"
                  className="danger-action"
                  disabled={menu.tree.primary || menu.tree.locked}
                  onClick={() => confirm('delete-worktree', menu)}
                >
                  <Trash2 size={14} /> Delete worktree
                </button>
                {menu.tree.primary && (
                  <small className="context-note">The primary worktree cannot be deleted.</small>
                )}
              </>
            )}
          </div>,
          document.body,
        )}
      {dialog && (
        <WorkspaceDialog
          key={`${dialog.kind}:${dialog.target.session?.id ?? dialog.target.tree.path}`}
          dialog={dialog}
          groups={[
            ...new Set(snapshot.projects.flatMap((project) => (project.group ? [project.group] : []))),
          ]}
          metadata={meta(dialog.target.project.id, dialog.target.tree.path)}
          trees={trees[dialog.target.project.id] ?? []}
          pending={pending}
          onClose={() => {
            if (!pending) setDialog(null)
          }}
          onSubmit={completeDialog}
        />
      )}
    </>
  )
}

function WorkspaceDialog({
  dialog,
  groups,
  metadata,
  trees,
  pending,
  onClose,
  onSubmit,
}: {
  dialog: Dialog
  groups: string[]
  metadata?: WorkspaceMeta
  trees: Worktree[]
  pending: boolean
  onClose: () => void
  onSubmit: (title: string) => Promise<void>
}) {
  const [title, setTitle] = useState(
    dialog.kind === 'move-group'
      ? (dialog.target.project.group ?? '')
      : dialog.kind === 'parent'
        ? (metadata?.parentWorktreePath ?? '')
        : dialog.kind === 'update'
          ? metadata?.displayName || dialog.target.tree.branch
          : (dialog.target.session?.title ?? ''),
  )
  const label = {
    rename: 'Rename session',
    'delete-session': 'Delete session',
    'delete-worktree': 'Delete worktree',
    sleep: 'Sleep workspace',
    update: 'Update workspace name',
    parent: 'Set parent worktree',
    group: 'New group from project',
    'move-group': 'Move project to group',
  }[dialog.kind]
  return createPortal(
    <div
      className="modal-backdrop"
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose()
      }}
    >
      <section className="modal" role="dialog" aria-modal="true" aria-label={label}>
        <button
          className="icon-button modal-close"
          aria-label="Close dialog"
          disabled={pending}
          onClick={onClose}
        >
          <X size={16} />
        </button>
        <h2>{label}</h2>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void onSubmit(title)
          }}
        >
          {dialog.kind === 'move-group' ? (
            <>
              <p>Group projects in the sidebar without changing their files.</p>
              <label htmlFor="project-group">Project group</label>
              <select
                id="project-group"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                autoFocus
              >
                <option value="">No group</option>
                {groups.map((group) => (
                  <option key={group} value={group}>
                    {group}
                  </option>
                ))}
              </select>
            </>
          ) : dialog.kind === 'parent' ? (
            <>
              <p>Organize related worktrees under a parent. This does not rebase or change Git branches.</p>
              <label htmlFor="parent-worktree">Parent worktree</label>
              <select
                id="parent-worktree"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                autoFocus
              >
                <option value="">No parent</option>
                {trees
                  .filter((tree) => tree.path !== dialog.target.tree.path)
                  .map((tree) => (
                    <option key={tree.path} value={tree.path}>
                      {tree.branch || tree.path}
                    </option>
                  ))}
              </select>
            </>
          ) : dialog.kind === 'rename' || dialog.kind === 'update' || dialog.kind === 'group' ? (
            <>
              <label htmlFor="rename-session-title">
                {dialog.kind === 'group'
                  ? 'Group name'
                  : dialog.kind === 'update'
                    ? 'Workspace name'
                    : 'Session name'}
              </label>
              <input
                id="rename-session-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={dialog.kind === 'group' ? 80 : 120}
                autoFocus
              />
            </>
          ) : (
            <p>
              {dialog.kind === 'sleep'
                ? 'Stop every running agent and terminal in this workspace. Sessions and history remain available; stopped work is not automatically resumed.'
                : dialog.kind === 'delete-session'
                  ? `Delete “${title}” and its conversation history? Project files are kept.`
                  : `Remove the worktree folder “${dialog.target.tree.branch}”? The branch is kept. Modified files, a primary worktree, or running sessions prevent removal.`}
            </p>
          )}
          <div className="workspace-dialog-actions">
            <button
              type="button"
              className="secondary-button"
              onClick={onClose}
              disabled={pending}
              autoFocus={dialog.kind !== 'rename'}
            >
              Cancel
            </button>
            <button
              className="primary-button"
              disabled={pending || (['rename', 'update', 'group'].includes(dialog.kind) && !title.trim())}
              type="submit"
            >
              {pending ? 'Working…' : label}
            </button>
          </div>
        </form>
      </section>
    </div>,
    document.body,
  )
}
