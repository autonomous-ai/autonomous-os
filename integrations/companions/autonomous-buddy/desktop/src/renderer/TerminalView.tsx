import { useEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import type { SessionEvent } from '../shared/types'
function mergeEvents(a: SessionEvent[], b: SessionEvent[]) {
  return [...new Map([...a, ...b].map((event) => [event.seq, event])).values()]
    .sort((x, y) => x.seq - y.seq)
    .slice(-2000)
}

export default function TerminalView({
  sessionId,
  onError,
  focused = true,
}: {
  sessionId: string
  focused?: boolean
  onError: (error: unknown) => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const terminal = useRef<Terminal | null>(null)
  const focusedRef = useRef(focused)
  useEffect(() => {
    focusedRef.current = focused
    if (focused) terminal.current?.focus()
  }, [focused])
  useEffect(() => {
    if (!host.current) return
    let alive = true,
      hydrated = false,
      lastSeq = 0
    const queued: SessionEvent[] = []
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"SFMono-Regular", Menlo, Consolas, monospace',
      lineHeight: 1.4,
      scrollback: 5000,
      theme: {
        background: '#23262c',
        foreground: '#d7dae0',
        cursor: '#a6c7ad',
        selectionBackground: '#465451',
        black: '#282c34',
        red: '#e79a99',
        green: '#a4c795',
        yellow: '#dfc58f',
        blue: '#91b9db',
        magenta: '#c4a6d8',
        cyan: '#8bc5c3',
        white: '#d7dae0',
      },
    })
    terminal.current = term
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    const write = (event: SessionEvent) => {
      if (event.seq <= lastSeq) return
      lastSeq = event.seq
      if (event.type === 'terminal') term.write(event.text)
    }
    const unsubscribe = window.buddy.onUpdate((update) => {
      if (update.type !== 'event' || update.event.sessionId !== sessionId) return
      if (hydrated) write(update.event)
      else queued.push(update.event)
    })
    void window.buddy
      .session(sessionId)
      .then((detail) => {
        if (!alive) return
        mergeEvents(detail.events, queued).forEach(write)
        hydrated = true
        fit.fit()
        if (focusedRef.current) term.focus()
      })
      .catch(onError)
    const data = term.onData((text) => {
      void window.buddy.terminalWrite(sessionId, text).catch(onError)
    })
    const resize = term.onResize(({ cols, rows }) => {
      void window.buddy.terminalResize(sessionId, cols, rows).catch(onError)
    })
    const observer = new ResizeObserver(() => {
      if (alive && host.current?.clientWidth) fit.fit()
    })
    observer.observe(host.current)
    return () => {
      alive = false
      observer.disconnect()
      unsubscribe()
      data.dispose()
      resize.dispose()
      if (terminal.current === term) terminal.current = null
      term.dispose()
    }
  }, [sessionId, onError])
  return <div className="terminal-host" ref={host} aria-label="Interactive terminal" />
}
