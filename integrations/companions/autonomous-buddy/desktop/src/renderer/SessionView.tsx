import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import {
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  Pencil,
  Play,
  Square,
  Terminal as TerminalIcon,
} from 'lucide-react'
import type { Session, SessionEvent } from '../shared/types'
import { providerName, statusName, StatusDot } from './App'

const TerminalView = lazy(() => import('./TerminalView'))

function mergeEvents(current: SessionEvent[], incoming: SessionEvent[]) {
  return [...new Map([...current, ...incoming].map((event) => [event.seq, event])).values()]
    .sort((a, b) => a.seq - b.seq)
    .slice(-2000)
}

type SessionViewProps = { session: Session; onError: (error: unknown) => void; focused?: boolean }

export function SessionView(props: SessionViewProps) {
  return props.session.provider === 'terminal' || props.session.mode === 'interactive' ? (
    <InteractiveSessionView {...props} />
  ) : (
    <StructuredSessionView {...props} />
  )
}

function InteractiveSessionView({ session, onError, focused = true }: SessionViewProps) {
  useEffect(() => {
    const markRead = () => {
      if (focused && session.unread && document.hasFocus())
        void window.buddy.markRead(session.id).catch(onError)
    }
    markRead()
    window.addEventListener('focus', markRead)
    return () => window.removeEventListener('focus', markRead)
  }, [focused, session.id, session.unread, onError])
  return (
    <div className="session-view interactive-session">
      <Suspense fallback={<div className="terminal-loading">Opening terminal…</div>}>
        <TerminalView sessionId={session.id} onError={onError} focused={focused} />
      </Suspense>
    </div>
  )
}

export function InteractiveSessionControls({ session, onError }: SessionViewProps) {
  const [busy, setBusy] = useState(false)
  const live = !session.closed && (session.processActive ?? session.status === 'running')
  const label = live ? 'Stop' : session.provider === 'terminal' ? 'Restart terminal' : 'Resume CLI'
  return (
    <>
      <span
        title={`${providerName(session.provider)} · ${statusName(session)}`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 5,
          fontSize: 9,
          marginRight: 5,
          whiteSpace: 'nowrap',
        }}
      >
        <StatusDot session={session} />
        {statusName(session)}
      </span>
      <button
        className="icon-button"
        aria-label={`${label} ${session.title}`}
        title={label}
        disabled={busy || !!session.closed}
        onClick={() => {
          setBusy(true)
          const action = live ? window.buddy.stop(session.id) : window.buddy.restartInteractive(session.id)
          void action.catch(onError).finally(() => setBusy(false))
        }}
      >
        {live ? <Square size={12} /> : <Play size={12} />}
      </button>
    </>
  )
}

function StructuredSessionView({
  session,
  onError,
  focused = true,
}: {
  session: Session
  onError: (error: unknown) => void
  focused?: boolean
}) {
  const [events, setEvents] = useState<SessionEvent[]>([])
  const [prompt, setPrompt] = useState(() => localStorage.getItem(`buddy.draft.${session.id}`) ?? '')
  const updatePrompt = (value: string) => {
    setPrompt(value)
    if (value) localStorage.setItem(`buddy.draft.${session.id}`, value)
    else localStorage.removeItem(`buddy.draft.${session.id}`)
  }
  const [sending, setSending] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(session.title)
  const [atBottom, setAtBottom] = useState(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const running = session.status === 'running'
  const isTerminal = session.provider === 'terminal'
  useEffect(() => {
    if (isTerminal) return
    let alive = true
    const unsubscribe = window.buddy.onUpdate((update) => {
      if (update.type === 'event' && update.event.sessionId === session.id)
        setEvents((current) => mergeEvents(current, [update.event]))
    })
    void window.buddy
      .session(session.id)
      .then((detail) => {
        if (alive) setEvents((current) => mergeEvents(detail.events, current))
      })
      .catch(onError)
    return () => {
      alive = false
      unsubscribe()
    }
  }, [session.id, isTerminal, onError])
  useEffect(() => {
    if (atBottom && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [events, atBottom])
  useEffect(() => {
    const markFocusedRead = () => {
      if (focused && session.unread && document.hasFocus())
        void window.buddy.markRead(session.id).catch(onError)
    }
    markFocusedRead()
    window.addEventListener('focus', markFocusedRead)
    return () => window.removeEventListener('focus', markFocusedRead)
  }, [session.id, session.unread, focused, onError])

  const send = async () => {
    if (!prompt.trim() || running || sending) return
    setSending(true)
    try {
      await window.buddy.send(session.id, prompt.trim())
      updatePrompt('')
      setAtBottom(true)
      inputRef.current?.focus()
    } catch (error) {
      onError(error)
    } finally {
      setSending(false)
    }
  }
  // Join adjacent stream fragments so token-by-token output forms a single transcript block.
  const blocks: { type: SessionEvent['type']; text: string; id: string; at: number }[] = []
  for (const event of events) {
    if (event.type === 'status') continue
    const last = blocks.at(-1)
    if (event.type === 'output' && last?.type === 'output') last.text += event.text
    else blocks.push({ ...event })
  }
  return (
    <div className="session-view">
      <div className="session-toolbar">
        <div className="session-identity">
          {isTerminal ? <TerminalIcon size={15} /> : <Bot size={15} />}
          {renaming ? (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void window.buddy
                  .renameSession(session.id, title.trim())
                  .then(() => setRenaming(false))
                  .catch(onError)
              }}
            >
              <input
                aria-label="Session name"
                autoFocus
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={120}
              />
              <button className="icon-button" aria-label="Save session name">
                <Check size={13} />
              </button>
            </form>
          ) : (
            <button
              className="rename-button"
              title="Rename session"
              onClick={() => {
                setTitle(session.title)
                setRenaming(true)
              }}
            >
              {session.title}
              <Pencil size={12} />
            </button>
          )}
        </div>
        <div className="session-status">
          <StatusDot session={session} />
          {!isTerminal && (
            <span title="Agent tools run with full access, without approval prompts">
              Full access · no approvals
            </span>
          )}
          {statusName(session)}
          {running && (
            <button
              className="stop-button"
              aria-label="Stop session"
              onClick={() => void window.buddy.stop(session.id).catch(onError)}
            >
              <Square size={11} fill="currentColor" /> Stop
            </button>
          )}
        </div>
      </div>
      {isTerminal ? (
        <Suspense fallback={<div className="sidebar-hint">Opening terminal…</div>}>
          <TerminalView sessionId={session.id} onError={onError} focused={focused} />
        </Suspense>
      ) : (
        <>
          <div
            ref={scrollRef}
            className="transcript"
            onScroll={(event) => {
              const node = event.currentTarget
              setAtBottom(node.scrollHeight - node.scrollTop - node.clientHeight < 100)
            }}
          >
            {blocks.length === 0 && (
              <div className="session-empty">
                <Bot size={28} strokeWidth={1.4} />
                <h2>What would you like to work on?</h2>
                <p>
                  {providerName(session.provider)} is ready in this worktree.
                  <br />
                  Ask a question, explore an idea, or start building.
                </p>
                <div className="suggestions">
                  {[
                    'Explain the structure of this project',
                    'Review the current changes',
                    'Help me plan a new feature',
                  ].map((text) => (
                    <button
                      key={text}
                      onClick={() => {
                        updatePrompt(text)
                        inputRef.current?.focus()
                      }}
                    >
                      {text}
                      <ArrowUp size={13} />
                    </button>
                  ))}
                </div>
              </div>
            )}
            {blocks.map((block) => (
              <article key={block.id} className={`transcript-block ${block.type}`}>
                <div className="message-label">
                  {block.type === 'prompt'
                    ? 'YOU'
                    : block.type === 'error'
                      ? 'ERROR'
                      : block.type === 'result'
                        ? 'RESULT'
                        : providerName(session.provider).toUpperCase()}
                  <time>
                    {new Date(block.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </time>
                </div>
                <pre>{block.text}</pre>
              </article>
            ))}
            {running && (
              <div className="working-line">
                <span className="working-pulse" />
                {providerName(session.provider)} is working…
              </div>
            )}
            {session.status === 'needs_input' && (
              <div className="attention-banner">
                This agent needs your attention. Review the result and send a follow-up. Interactive
                permission dialogs are available in a terminal session.
              </div>
            )}
          </div>
          {!atBottom && (
            <button className="jump-bottom" onClick={() => setAtBottom(true)}>
              <ChevronDown size={14} /> Latest output
            </button>
          )}
          <div className="composer-wrap">
            <form
              className="composer"
              onSubmit={(event) => {
                event.preventDefault()
                void send()
              }}
            >
              <textarea
                ref={inputRef}
                aria-label="Message agent"
                placeholder={`Message ${providerName(session.provider)}…`}
                value={prompt}
                onChange={(event) => updatePrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault()
                    void send()
                  }
                }}
                rows={3}
                disabled={sending}
              />
              <div className="composer-footer">
                <span>
                  <Bot size={13} />
                  {providerName(session.provider)}
                  <span className="composer-context">
                    {' '}
                    · {session.providerSessionId ? 'Continuing this session' : 'New conversation'}
                  </span>
                </span>
                <button
                  type="submit"
                  className="send-button"
                  aria-label="Send message"
                  disabled={running || sending || !prompt.trim()}
                >
                  <ArrowUp size={17} />
                </button>
              </div>
            </form>
            <div className="composer-hint">
              <span>Enter to send · Shift + Enter for a new line</span>
              <span>
                {running ? 'Wait for this turn or stop it to follow up' : 'Runs in your selected worktree'}
              </span>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
