import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  File,
  Files,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  GitPullRequest,
  RefreshCw,
  Search,
} from 'lucide-react'
import type { FileEntry, GitSnapshot, Project } from '../shared/types'

type Preview = { name: string; text: string; diff: boolean }
type Props = {
  project?: Project
  path?: string
  onPreview: (preview: Preview) => void
  onError: (error: unknown) => void
}
export function GitPanel({ project, path, onPreview, onError }: Props) {
  const [tab, setTab] = useState<'git' | 'files'>('git')
  const [git, setGit] = useState<GitSnapshot | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [revision, setRevision] = useState(0)
  const [filter, setFilter] = useState('')
  const [historyOpen, setHistoryOpen] = useState(true)
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
        setGit(value)
        setError('')
      }
    } catch (error) {
      if (request.current === id) setError(error instanceof Error ? error.message : String(error))
    } finally {
      if (request.current === id) setRefreshing(false)
    }
  }, [project, path])
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
    try {
      const text = diff
        ? await window.buddy.diff(project.id, path, file)
        : await window.buddy.readFile(project.id, path, file)
      onPreview({ name: file, text, diff })
    } catch (error) {
      onError(error)
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
            <>
              <div className="branch-summary">
                <div>
                  <GitBranch size={14} />
                  <strong>{git?.branch || 'Working directory'}</strong>
                </div>
                <small title={path}>{path}</small>
              </div>
              {error ? (
                <div className="panel-error" role="status">
                  {error}
                </div>
              ) : (
                <div className="changes-section">
                  <div className="pane-section-heading">
                    <ChevronDown size={12} /> WORKING CHANGES <span>{git?.files.length ?? 0}</span>
                  </div>
                  {git?.files.length ? (
                    <div className="changed-files">
                      {git.files.map((file) => (
                        <button
                          key={file.path}
                          className="changed-file"
                          onClick={() => void open(file.path, true)}
                          title={file.path}
                        >
                          <File size={13} />
                          <span>
                            {file.path.split('/').at(-1)}
                            <small>
                              {file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/')) : ''}
                            </small>
                          </span>
                          <em
                            className={`file-status ${file.status.includes('?') || file.status.includes('A') ? 'added' : file.status.includes('D') ? 'deleted' : ''}`}
                          >
                            {file.status.trim() || 'M'}
                          </em>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="clean-state">
                      <div className="clean-icon">
                        <GitPullRequest size={19} />
                      </div>
                      <h3>{git ? 'No uncommitted changes' : 'Reading your worktree…'}</h3>
                      <p>
                        {git ? 'Changes in this worktree will appear here.' : 'Fetching local Git status.'}
                      </p>
                    </div>
                  )}
                </div>
              )}
              <div className="commit-section">
                <button className="pane-section-heading" onClick={() => setHistoryOpen(!historyOpen)}>
                  {historyOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />} RECENT COMMITS{' '}
                  <span>{git?.commits.length ?? 0}</span>
                </button>
                {historyOpen && (
                  <div className="commit-list">
                    {git?.commits.map((commit) => (
                      <div key={commit.hash} className="commit-row">
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
                      </div>
                    ))}
                    {git && !git.commits.length && <p className="sidebar-hint">No commits yet.</p>}
                  </div>
                )}
              </div>
            </>
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
