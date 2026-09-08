import { spawn } from 'node:child_process'
import { accessSync, constants } from 'node:fs'
import path from 'node:path'
import type { Provider } from '../shared/types.js'
export interface ProcessHandle {
  write(data: string): void
  resize?(cols: number, rows: number): void
  kill(): void
}
export interface LaunchOptions {
  provider: Provider
  cwd: string
  sessionId?: string
  prompt: string
  data(text: string): void
  diagnostic(text: string): void
  error(error: Error): void
  exit(code: number): void
}
export type Launcher = (options: LaunchOptions) => Promise<ProcessHandle>
export function available(provider: Provider): boolean {
  if (provider === 'terminal') return true
  return (process.env.PATH ?? '').split(path.delimiter).some((dir) => {
    try {
      accessSync(path.join(dir, provider), constants.X_OK)
      return true
    } catch {
      return false
    }
  })
}
export function invocation(
  provider: Exclude<Provider, 'terminal'>,
  sessionId?: string,
): { command: string; args: string[] } {
  if (provider === 'codex')
    return {
      command: 'codex',
      args: [
        'exec',
        '-c',
        'sandbox_mode="workspace-write"',
        ...(sessionId
          ? ['resume', '--skip-git-repo-check', '--json', sessionId, '-']
          : ['--skip-git-repo-check', '--json', '-']),
      ],
    }
  return {
    command: 'claude',
    args: [
      '--print',
      '--verbose',
      '--output-format',
      'stream-json',
      '--include-partial-messages',
      ...(sessionId ? ['--resume', sessionId] : []),
    ],
  }
}
function groupStop(pid: number | undefined, fallback: () => void, live: () => boolean): void {
  const signal = (value: NodeJS.Signals) => {
    try {
      if (pid && process.platform !== 'win32') process.kill(-pid, value)
      else fallback()
    } catch {
      if (live()) {
        try {
          fallback()
        } catch {
          /* Process already exited. */
        }
      }
    }
  }
  signal('SIGTERM')
  const timer = setTimeout(() => {
    if (live()) signal('SIGKILL')
  }, 1500)
  timer.unref()
}
export const launch: Launcher = async (options) => {
  if (options.provider === 'terminal') {
    const pty = await import('node-pty')
    const child = pty.spawn(process.env.SHELL || '/bin/sh', [], {
      name: 'xterm-256color',
      cwd: options.cwd,
      cols: 100,
      rows: 30,
      env: process.env as Record<string, string>,
    })
    let live = true
    child.onData(options.data)
    child.onExit(({ exitCode }) => {
      live = false
      options.exit(exitCode)
    })
    return {
      write: (data) => child.write(data),
      resize: (cols, rows) => child.resize(cols, rows),
      kill: () =>
        groupStop(
          child.pid,
          () => child.kill(),
          () => live,
        ),
    }
  }
  const command = invocation(options.provider, options.sessionId)
  const child = spawn(command.command, command.args, {
    cwd: options.cwd,
    env: process.env,
    detached: process.platform !== 'win32',
    stdio: 'pipe',
  })
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', options.data)
  // stderr is diagnostic output, never part of the JSON event protocol.
  child.stderr.on('data', options.diagnostic)
  let live = true
  child.on('error', options.error)
  child.on('close', (code) => {
    live = false
    options.exit(code ?? 1)
  })
  child.stdin.on('error', () => {
    /* The process exit event reports early stdin closure. */
  })
  child.stdin.end(options.prompt)
  return {
    write: (data) => child.stdin.write(data),
    kill: () =>
      groupStop(
        child.pid,
        () => {
          child.kill()
        },
        () => live,
      ),
  }
}
export interface ParsedEvent {
  text?: string
  kind?: 'output' | 'result' | 'error'
  sessionId?: string
  completed?: boolean
  failed?: boolean
  needsInput?: boolean
}
export function parseEvent(line: string, provider: Provider): ParsedEvent {
  let event: {
    type?: string
    subtype?: string
    thread_id?: string
    session_id?: string
    text?: string
    message?: string | { content?: { type: string; name: string; input: unknown }[] }
    error?: { message?: string }
    item?: { type?: string; text?: string; command?: string; aggregated_output?: string; changes?: unknown }
    event?: { delta?: { type?: string; text?: string } }
    is_error?: boolean
    permission_denials?: unknown[]
    result?: string
    errors?: string[]
  }
  try {
    event = JSON.parse(line)
  } catch {
    return { text: line + '\n', kind: 'output' }
  }
  if (!event || typeof event !== 'object' || Array.isArray(event))
    return { text: line + '\n', kind: 'output' }
  if (event.type === 'buddy.diagnostic') return { text: String(event.text), kind: 'output' }
  if (provider === 'codex') {
    if (event.type === 'thread.started') return { sessionId: event.thread_id }
    if (event.type === 'turn.completed') return { completed: true }
    if (event.type === 'turn.failed' || event.type === 'error')
      return {
        failed: true,
        kind: 'error',
        text: event.error?.message || (typeof event.message === 'string' ? event.message : 'Agent failed'),
      }
    if (event.type === 'item.completed' || event.type === 'item.started') {
      const item = event.item ?? {}
      if (item.type === 'agent_message') return { kind: 'output', text: item.text + '\n' }
      if (item.type === 'command_execution')
        return {
          kind: 'output',
          text: [item.command, item.aggregated_output].filter(Boolean).join('\n') + '\n',
        }
      if (item.type === 'file_change')
        return { kind: 'output', text: JSON.stringify(item.changes ?? []) + '\n' }
    }
  } else {
    if (event.type === 'system' && event.subtype === 'init') return { sessionId: event.session_id }
    if (event.type === 'stream_event') {
      const delta = event.event?.delta
      if (delta?.type === 'text_delta') return { kind: 'output', text: delta.text }
    }
    if (event.type === 'assistant') {
      const content = typeof event.message === 'object' ? (event.message.content ?? []) : []
      const text = content
        .filter((v: { type: string }) => v.type === 'tool_use')
        .map((v: { name: string; input: unknown }) => `\n${v.name}: ${JSON.stringify(v.input)}\n`)
        .join('')
      return text ? { text, kind: 'output' } : {}
    }
    if (event.type === 'result')
      return {
        sessionId: event.session_id,
        completed: !event.is_error,
        failed: !!event.is_error,
        needsInput: !!event.permission_denials?.length,
        kind: event.is_error ? 'error' : 'result',
        text: event.result || event.errors?.join('\n') || event.subtype,
      }
  }
  return {}
}
