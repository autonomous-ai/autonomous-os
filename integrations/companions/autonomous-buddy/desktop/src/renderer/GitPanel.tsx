import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  File,
  Files,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  Check,
  Plus,
  Minus,
  RefreshCw,
  Search,
} from 'lucide-react'
import './GitReviewPanel.css'
import type { FileEntry, GitFile, GitSnapshot, Project } from '../shared/types'

type Preview = { name: string; text: string; diff: boolean }
type Props = {
  project?: Project
  path?: string
  onPreview: (preview: Preview) => void
  onError: (error: unknown) => void
}
export function GitPanel({ project, path, onPreview, onError }: Props) {
  const workspaceKey = JSON.stringify([project?.id ?? '', path ?? ''])
  const activeWorkspace = useRef<string | null>(workspaceKey)
  const previewRequest = useRef(0)
  const historyRequest = useRef(0)
  useLayoutEffect(() => {
    activeWorkspace.current = workspaceKey
    return () => {
      activeWorkspace.current = null
      previewRequest.current += 1
      historyRequest.current += 1
    }
  }, [workspaceKey])
  const [tab, setTab] = useState<'git' | 'files'>('git')
  const [gitState, setGitState] = useState<{ key: string; value: GitSnapshot } | null>(null)
  const git = gitState && gitState.key === workspaceKey ? gitState.value : null
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [revision, setRevision] = useState(0)
  const [filter, setFilter] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const message = drafts[workspaceKey] ?? ''
  const setMessage = (value: string) => setDrafts((current) => ({ ...current, [workspaceKey]: value }))
  const [acting, setActing] = useState(false)
  const actionPending = useRef(false)
  const [review, setReview] = useState<{ key: string; path: string; hash: string; files: GitFile[] } | null>(
    null,
  )
  const [historyLoading, setHistoryLoading] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const [branchOpen, setBranchOpen] = useState(true)
  const [collapsedChanges, setCollapsedChanges] = useState<Record<string, boolean>>({})
  const changeGroups = [
    { id: 'tracked', label: 'CHANGES', aria: 'Changes', files: (git?.files ?? []).filter((file) => file.status !== '??') },
    { id: 'untracked', label: 'UNTRACKED FILES', aria: 'Untracked files', files: (git?.files ?? []).filter((file) => file.status === '??') },
  ].filter((group) => group.files.length > 0)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const request = useRef(0)
  const invalidate = useCallback(() => {
    request.current += 1
  }, [])
  const refresh = useCallback(async () => {
    if (!project || !path) return
    const id = ++request.current
    setRefreshing(true)
    try {
      const value = await window.buddy.git(project.id, path)
      if (request.current === id) {
        setGitState({ key: workspaceKey, value })
        setError('')
      }
    } catch (error) {
      if (request.current === id) setError(error instanceof Error ? error.message : String(error))
    } finally {
      if (request.current === id) setRefreshing(false)
    }
  }, [project, path, workspaceKey])
  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0)
    const interval = setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, 5000)
    const focus = () => {
      void refresh()
      setRevision((value) => value + 1)
    }
    window.addEventListener('focus', focus)
    return () => {
      invalidate()
      clearTimeout(timer)
      clearInterval(interval)
      window.removeEventListener('focus', focus)
    }
  }, [refresh, invalidate])
  const open = async (file: string, diff: boolean) => {
    if (!project || !path) return
    const id = ++previewRequest.current
    try {
      const text = diff
        ? await window.buddy.diff(project.id, path, file)
        : await window.buddy.readFile(project.id, path, file)
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) {
        setSelectedFile(diff ? JSON.stringify([workspaceKey, file]) : null)
        onPreview({ name: file, text, diff })
      }
    } catch (error) {
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) onError(error)
    }
  }
  const stagedCount =
    git?.files.filter((file) => file.status[0] !== ' ' && file.status[0] !== '?').length ?? 0
  const unstagedCount = git?.files.filter((file) => file.status[1] !== ' ').length ?? 0
  const mutate = async (action: () => Promise<unknown>, committed = false) => {
    if (actionPending.current) return
    actionPending.current = true
    setActing(true)
    try {
      await action()
      if (committed) setMessage('')
      if (activeWorkspace.current === workspaceKey) await refresh()
    } catch (error) {
      if (activeWorkspace.current === workspaceKey) onError(error)
    } finally {
      actionPending.current = false
      if (activeWorkspace.current !== null) setActing(false)
    }
  }
  const inspectCommit = async (hash: string) => {
    if (!project || !path) return
    const id = ++historyRequest.current
    if (review?.hash === hash && review.key === workspaceKey) {
      setReview(null)
      setHistoryLoading('')
      return
    }
    setHistoryLoading(workspaceKey + hash)
    try {
      const files = await window.buddy.commitFiles(project.id, path, hash)
      if (activeWorkspace.current === workspaceKey && historyRequest.current === id)
        setReview({ key: workspaceKey, path, hash, files })
    } catch (error) {
      if (activeWorkspace.current === workspaceKey && historyRequest.current === id) onError(error)
    } finally {
      if (activeWorkspace.current === workspaceKey && historyRequest.current === id) setHistoryLoading('')
    }
  }
  const openCommitFile = async (hash: string, file: string) => {
    if (!project || !path) return
    const id = ++previewRequest.current
    try {
      const text = await window.buddy.commitDiff(project.id, path, hash, file)
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) {
        setSelectedFile(null)
        onPreview({ name: `${hash.slice(0, 7)} · ${file}`, text, diff: true })
      }
    } catch (error) {
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) onError(error)
    }
  }
  const openBranchFile = async (file: string) => {
    const comparison = git?.branchChanges
    if (!project || !path || !comparison) return
    const id = ++previewRequest.current
    try {
      const text = await window.buddy.branchDiff(
        project.id, path, comparison.baseHash, comparison.headHash, file,
      )
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) {
        setSelectedFile(JSON.stringify([workspaceKey, 'branch', file]))
        onPreview({ name: `${comparison.base} → ${comparison.headHash.slice(0, 7)} · ${file}`, text, diff: true })
      }
    } catch (error) {
      if (activeWorkspace.current === workspaceKey && previewRequest.current === id) onError(error)
    }
  }
  return (
    <aside className="git-panel">
      <div className="right-tabs">
        <button className={tab === 'git' ? 'active' : ''} onClick={() => setTab('git')}>
          <GitBranch size={15} /> Changes {git && git.files.length > 0 && <span>{git.files.length}</span>}
        </button>
        <button className={tab === 'files' ? 'active' : ''} onClick={() => setTab('files')}>
          <Files size={15} /> Files
        </button>
        <button
          className="icon-button refresh-button"
          aria-label="Refresh files and Git"
          disabled={!project || refreshing}
          onClick={() => {
            void refresh()
            setRevision((value) => value + 1)
          }}
        >
          <RefreshCw size={13} className={refreshing ? 'spinning' : ''} />
        </button>
      </div>
      {!project || !path ? (
        <div className="git-empty">
          <GitBranch size={26} strokeWidth={1.2} />
          <h3>Your changes, in context.</h3>
          <p>
            Open a project to explore files,
            <br />
            branches and commit history.
          </p>
        </div>
      ) : (
        <>
          {tab === 'git' ? (
            <div className="git-review-scroll">
              <div className="branch-summary">
                <div>
                  <GitBranch size={14} />
                  <strong>{git?.branch || 'Working directory'}</strong>
                </div>
                <small title={path}>{path}</small>
                {git?.branchChanges && <small title="Committed changes since the common ancestor with this base">→ {git.branchChanges.base}</small>}
              </div>
              <form
                className="git-commit-composer"
                onSubmit={(event) => {
                  event.preventDefault()
                  void mutate(() => window.buddy.commitStaged(project.id, path, message), true)
                }}
              >
                <label htmlFor="git-commit-message">Commit message</label>
                <textarea
                  id="git-commit-message"
                  aria-label="Commit message"
                  placeholder="Describe your changes…"
                  maxLength={10000}
                  rows={3}
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  disabled={acting}
                />
                <div className="git-commit-buttons">
                  <button
                    type="button"
                    className="git-stage-all"
                    aria-label="Stage all changes"
                    disabled={acting || !unstagedCount}
                    onClick={() =>
                      void mutate(() =>
                        window.buddy.stageFiles(
                          project.id,
                          path,
                          git!.files.filter((file) => file.status[1] !== ' ').map((file) => file.path),
                        ),
                      )
                    }
                    title="Stage all currently listed unstaged and untracked files in this worktree"
                  >
                    <Plus size={13} /> Stage all
                  </button>
                  <button
                    type="submit"
                    disabled={acting || !message.trim() || stagedCount === 0}
                    title="Commit this worktree’s staged changes; unstaged files remain on disk"
                  >
                    <Check size={13} /> Commit staged changes ({stagedCount})
                  </button>
                </div>
                <div className="git-stage-summary">
                  <span>
                    {stagedCount} staged · {unstagedCount} unstaged
                  </span>
                  {stagedCount > 0 && (
                    <button
                      type="button"
                      disabled={acting}
                      onClick={() =>
                        void mutate(() =>
                          window.buddy.unstageFiles(
                            project.id,
                            path,
                            git!.files
                              .filter((file) => file.status[0] !== ' ' && file.status[0] !== '?')
                              .map((file) => file.path),
                          ),
                        )
                      }
                      aria-label="Unstage all changes"
                    >
                      Unstage all
                    </button>
                  )}
                </div>
              </form>
              {error ? (
                <div className="panel-error" role="status">
                  {error}
                </div>
              ) : (
                <>
                {changeGroups.map((group) => (
                <div className={`changes-section git-${group.id}-changes`} key={group.id}>
                  <button
                    className="pane-section-heading git-changes-toggle"
                    aria-label={group.aria}
                    aria-expanded={!collapsedChanges[group.id]}
                    onClick={() => setCollapsedChanges((current) => ({ ...current, [group.id]: !current[group.id] }))}
                  >
                    {!collapsedChanges[group.id] ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {group.label}{' '}
                    <span>{group.files.length}</span>
                  </button>
                  {!collapsedChanges[group.id] && (
                      <div className="changed-files">
                        {group.files.map((file) => (
                          <div
                            className={`git-change-row ${selectedFile === JSON.stringify([workspaceKey, file.path]) ? 'selected' : ''}`}
                            key={file.path}
                          >
                            <button
                              className="changed-file"
                              onClick={() => void open(file.path, true)}
                              title={file.path}
                            >
                              <File size={13} />
                              <span className="git-file-name">
                                <strong>{file.path.split('/').at(-1)}</strong>
                                <small
                                  title={file.originalPath ? `Renamed from ${file.originalPath}` : file.path}
                                >
                                  {file.originalPath
                                    ? `← ${file.originalPath}`
                                    : file.path.includes('/')
                                      ? file.path.slice(0, file.path.lastIndexOf('/'))
                                      : ''}
                                </small>
                              </span>
                              {file.lineStats && (
                                <span
                                  className="git-file-stat"
                                  title={
                                    git?.commits.length
                                      ? 'Added / removed lines, HEAD to working tree; untracked files count local text'
                                      : 'Added / removed lines from the initial index or local untracked text'
                                  }
                                >
                                  <span className="git-lines-added">+{file.lineStats.added}</span>
                                  <span className="git-lines-removed">−{file.lineStats.removed}</span>
                                </span>
                              )}
                              <em
                                className={`file-status ${file.status.includes('?') || file.status.includes('A') ? 'added' : file.status.includes('D') ? 'deleted' : ''}`}
                              >
                                {file.status.trim() || 'M'}
                              </em>
                            </button>
                            <div className="git-stage-actions">
                              {file.status[1] !== ' ' && (
                                <button
                                  disabled={acting}
                                  aria-label={`Stage ${file.path}`}
                                  title="Stage this file"
                                  onClick={() =>
                                    void mutate(() => window.buddy.stageFiles(project.id, path, [file.path]))
                                  }
                                >
                                  <Plus size={12} />
                                </button>
                              )}
                              {file.status[0] !== ' ' && file.status[0] !== '?' && (
                                <button
                                  disabled={acting}
                                  aria-label={`Unstage ${file.path}`}
                                  title="Unstage this file; keep working files"
                                  onClick={() =>
                                    void mutate(() =>
                                      window.buddy.unstageFiles(project.id, path, [file.path]),
                                    )
                                  }
                                >
                                  <Minus size={12} />
                                </button>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                </div>
                ))}
                {!changeGroups.length && <p className="git-clean-summary">{git ? 'No uncommitted changes' : 'Reading your worktree…'}</p>}
                </>
              )}
              <div className="changes-section git-branch-changes">
                <button
                  className="pane-section-heading git-changes-toggle"
                  aria-label="Committed on branch"
                  aria-expanded={branchOpen}
                  onClick={() => setBranchOpen(!branchOpen)}
                >
                  {branchOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  COMMITTED ON BRANCH <span>{git?.branchChanges?.files.length ?? 0}</span>
                </button>
                {branchOpen && (
                  git?.branchChanges ? (
                    <div className="changed-files">
                      {git.branchChanges.files.map((file) => (
                        <div
                          className={`git-change-row ${selectedFile === JSON.stringify([workspaceKey, 'branch', file.path]) ? 'selected' : ''}`}
                          key={file.path}
                        >
                          <button
                            className="changed-file"
                            aria-label={`Review branch file ${file.path}`}
                            title={file.originalPath ? `${file.originalPath} → ${file.path}` : file.path}
                            onClick={() => void openBranchFile(file.path)}
                          >
                            <File size={13} />
                            <span className="git-file-name">
                              <strong>{file.path.split('/').at(-1)}</strong>
                              <small>{file.originalPath ? `← ${file.originalPath}` : file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : ''}</small>
                            </span>
                            {file.lineStats && (
                              <span className="git-file-stat" title="Committed added / removed lines since the common ancestor">
                                <span className="git-lines-added">+{file.lineStats.added}</span>
                                <span className="git-lines-removed">−{file.lineStats.removed}</span>
                              </span>
                            )}
                            <em className={`file-status ${file.status.startsWith('A') ? 'added' : file.status.startsWith('D') ? 'deleted' : ''}`}>
                              {file.status}
                            </em>
                          </button>
                        </div>
                      ))}
                      {!git.branchChanges.files.length && <p className="sidebar-hint">No committed changes against {git.branchChanges.base}.</p>}
                    </div>
                  ) : <p className="sidebar-hint">{git ? 'No comparison base available for this branch.' : 'Reading branch changes…'}</p>
                )}
              </div>
              <div className="commit-section">
                <button className="pane-section-heading" onClick={() => setHistoryOpen(!historyOpen)}>
                  {historyOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />} RECENT COMMITS{' '}
                  <span>{git?.commits.length ?? 0}</span>
                </button>
                {historyOpen && (
                  <div className="commit-list">
                    {git?.commits.map((commit) => (
                      <div key={commit.hash}>
                        <button
                          type="button"
                          className="commit-row git-commit-select"
                          aria-label={`Review commit ${commit.hash.slice(0, 7)}`}
                          aria-expanded={review?.hash === commit.hash && review.key === workspaceKey}
                          onClick={() => void inspectCommit(commit.hash)}
                        >
                          <div className="commit-graph">
                            <GitCommitHorizontal size={17} />
                          </div>
                          <div>
                            <strong title={commit.subject}>{commit.subject}</strong>
                            <small>
                              {commit.author} ·{' '}
                              {new Date(commit.date).toLocaleDateString(undefined, {
                                month: 'short',
                                day: 'numeric',
                              })}
                            </small>
                          </div>
                          <code>{commit.hash.slice(0, 7)}</code>
                        </button>
                        {historyLoading === workspaceKey + commit.hash && <small>Reading commit…</small>}
                        {review?.hash === commit.hash && review.key === workspaceKey && (
                          <div className="git-commit-files">
                            <small>
                              Compared with first parent; root commits compare with an empty tree.
                            </small>
                            {review.files.map((file) => (
                              <button
                                key={file.path}
                                onClick={() => void openCommitFile(commit.hash, file.path)}
                              >
                                <em>{file.status}</em>
                                <span>
                                  {file.originalPath ? `${file.originalPath} → ${file.path}` : file.path}
                                </span>
                              </button>
                            ))}
                            {!review.files.length && <small>No file changes.</small>}
                          </div>
                        )}
                      </div>
                    ))}
                    {git && !git.commits.length && <p className="sidebar-hint">No commits yet.</p>}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <>
              <div className="file-search">
                <Search size={13} />
                <input
                  aria-label="Filter root files"
                  placeholder="Filter root files…"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                />
              </div>
              <div className="file-tree">
                <div className="file-tree-heading">
                  <ChevronDown size={13} />
                  <Folder size={14} />
                  {project.name}
                </div>
                <Directory
                  key={revision}
                  projectId={project.id}
                  worktreePath={path}
                  relativePath=""
                  depth={0}
                  filter={filter}
                  onOpen={(file) => void open(file, false)}
                  onError={onError}
                />
              </div>
            </>
          )}
        </>
      )}
      <div className="right-footer">
        <span className="connection-dot" />
        {project ? 'Local Git · refreshes every 5s' : 'Local files stay on your computer'}
      </div>
    </aside>
  )
}
function Directory({
  projectId,
  worktreePath,
  relativePath,
  depth,
  filter = '',
  onOpen,
  onError,
}: {
  projectId: string
  worktreePath: string
  relativePath: string
  depth: number
  filter?: string
  onOpen: (file: string) => void
  onError: (error: unknown) => void
}) {
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [expanded, setExpanded] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    let alive = true
    void window.buddy
      .files(projectId, worktreePath, relativePath)
      .then((files) => {
        if (alive) {
          setEntries(files)
          setLoaded(true)
        }
      })
      .catch(onError)
    return () => {
      alive = false
    }
  }, [projectId, worktreePath, relativePath, onError])
  return (
    <>
      {entries
        .filter((entry) => entry.name.toLowerCase().includes(filter.toLowerCase()))
        .map((entry) => (
          <div key={entry.path}>
            <button
              className="file-entry"
              style={{ paddingLeft: 16 + depth * 14 }}
              onClick={() =>
                entry.directory
                  ? setExpanded((current) =>
                      current.includes(entry.path)
                        ? current.filter((path) => path !== entry.path)
                        : [...current, entry.path],
                    )
                  : onOpen(entry.path)
              }
            >
              {entry.directory ? (
                <>
                  {expanded.includes(entry.path) ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  <Folder size={13} />
                </>
              ) : (
                <>
                  <span className="file-indent" />
                  <File size={13} />
                </>
              )}
              <span>{entry.name}</span>
            </button>
            {entry.directory && expanded.includes(entry.path) && (
              <Directory
                projectId={projectId}
                worktreePath={worktreePath}
                relativePath={entry.path}
                depth={depth + 1}
                onOpen={onOpen}
                onError={onError}
              />
            )}
          </div>
        ))}
      {!entries.length && <p className="directory-empty">{loaded ? 'Empty folder' : 'Loading…'}</p>}
    </>
  )
}
