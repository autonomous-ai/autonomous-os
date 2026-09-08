import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import type { NativeAction, NativeCommandResult, NativeState } from '../shared/types'

export class NativeHelper {
  private child?: ChildProcessWithoutNullStreams
  private buffer = ''
  private stopping = false
  private pending = new Map<
    string,
    { resolve(value: unknown): void; reject(error: Error): void; timer: ReturnType<typeof setTimeout> }
  >()
  private state: NativeState = {
    available: false,
    paired: false,
    connection: 'disconnected',
    paused: false,
    accessibility: false,
    screenRecording: false,
    devices: [],
  }
  constructor(
    private executable: string,
    private publish: (state: NativeState) => void,
    private args = ['--embedded-helper'],
    private onMenuAction: (action: 'open-manager' | 'quit') => void = () => {},
    private onAgentRequest?: (command: Record<string, unknown>) => Promise<unknown>,
  ) {}

  start() {
    if (this.child) return
    this.stopping = false
    this.buffer = ''
    const child = spawn(this.executable, this.args, { stdio: 'pipe', env: process.env })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (data: string) => {
      if (this.child !== child) return
      this.buffer += data
      if (this.buffer.length > 8 * 1024 * 1024) {
        this.fail('Native response exceeded its size limit')
        child.kill()
        return
      }
      let newline: number
      while ((newline = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, newline)
        this.buffer = this.buffer.slice(newline + 1)
        this.receive(line)
      }
    })
    // Drain diagnostics without forwarding native command contents into the renderer or logs.
    child.stderr.resume()
    child.stdin.on('error', () => {
      if (!this.stopping) this.fail('Native helper input closed')
    })
    child.on('error', (error) => {
      if (this.child === child) this.fail(`Computer use could not start: ${error.message}`)
    })
    child.on('close', () => {
      if (this.child !== child) return
      this.child = undefined
      this.fail(this.stopping ? undefined : 'Computer use stopped. Restart it from Computer settings.')
    })
  }

  private receive(line: string) {
    let message: {
      id?: string
      error?: string
      result?: unknown
      event?: string
      action?: string
      command?: Record<string, unknown>
      state?: Partial<NativeState>
    }
    try {
      message = JSON.parse(line)
    } catch {
      return
    }
    if (!message || typeof message !== 'object') return
    if (message.event === 'agent_request' && typeof message.id === 'string' && message.command) {
      const child = this.child
      const command = message.command
      void Promise.resolve().then(() => this.onAgentRequest?.(command) ?? {
        id: command.id, ok: false, error: 'Agent manager is unavailable',
      }).then((response) => {
        if (this.child === child && !this.stopping)
          return this.request('agent_response', { id: message.id, response })
      }).catch(() => {})
      return
    }
    if (message.event === 'menu' && (message.action === 'open-manager' || message.action === 'quit'))
      this.onMenuAction(message.action)
    if (message.event === 'state' && message.state) this.updateState(message.state)
    if (typeof message.id !== 'string') return
    const pending = this.pending.get(message.id)
    if (!pending) return
    this.pending.delete(message.id)
    clearTimeout(pending.timer)
    if (message.error) pending.reject(new Error(String(message.error)))
    else pending.resolve(message.result)
  }

  private updateState(state: Partial<NativeState>) {
    this.state = {
      ...this.state,
      ...state,
      deviceHost: state.deviceHost,
      connectionError: state.connectionError,
      available: true,
      error: undefined,
    }
    this.publish(structuredClone(this.state))
  }
  private fail(error?: string) {
    this.state = { ...this.state, available: false, connection: 'disconnected', error }
    this.publish(structuredClone(this.state))
    for (const request of this.pending.values()) {
      clearTimeout(request.timer)
      request.reject(new Error(error || 'Native helper stopped'))
    }
    this.pending.clear()
  }
  private request(method: string, params: Record<string, unknown> = {}, timeout = 10000): Promise<unknown> {
    if (!this.child || this.child.exitCode !== null || this.child.killed)
      return Promise.reject(new Error('Computer use is unavailable. Restart it from Computer settings.'))
    const id = randomUUID()
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`Computer use ${method} timed out`))
      }, timeout)
      this.pending.set(id, { resolve, reject, timer })
      this.child!.stdin.write(JSON.stringify({ id, method, params }) + '\n', (error) => {
        if (error && this.pending.delete(id)) {
          clearTimeout(timer)
          reject(error)
        }
      })
    })
  }
  async status(): Promise<NativeState> {
    try {
      this.updateState((await this.request('status')) as Partial<NativeState>)
    } catch (error) {
      this.fail(error instanceof Error ? error.message : String(error))
    }
    return structuredClone(this.state)
  }
  async action(action: NativeAction, params: Record<string, unknown> = {}): Promise<unknown> {
    if (!['pair', 'unpair', 'pause', 'permissions', 'activity', 'restart'].includes(action))
      throw new Error('Unsupported computer action')
    if (action === 'restart') {
      await this.stop()
      this.start()
      return this.status()
    }
    const result = await this.request(action, params)
    await this.status()
    return result
  }
  async command(action: string, params: Record<string, unknown> = {}): Promise<NativeCommandResult> {
    if (
      typeof action !== 'string' ||
      !action ||
      action.length > 80 ||
      !params ||
      typeof params !== 'object' ||
      Array.isArray(params)
    )
      throw new Error('Invalid computer command')
    return (await this.request(
      'command',
      { action, params, timeout_ms: 30000 },
      35000,
    )) as NativeCommandResult
  }
  async agentEvent(event: object): Promise<unknown> {
    return this.request('agent_event', event as Record<string, unknown>)
  }
  async stop(): Promise<void> {
    const child = this.child
    if (!child) return
    this.stopping = true
    // Closing the inherited pipe is the ownership signal; the helper exits on EOF.
    child.stdin.end()
    await new Promise<void>((resolve) => {
      if (child.exitCode !== null) {
        resolve()
        return
      }
      const timer = setTimeout(() => {
        child.kill('SIGKILL')
      }, 2000)
      child.once('close', () => {
        clearTimeout(timer)
        resolve()
      })
    })
  }
}
