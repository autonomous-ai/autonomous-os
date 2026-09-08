import { createHash } from 'node:crypto'
import type { BuddyUpdate, SessionStatus, SessionEvent } from '../shared/types'
import type { Manager } from './manager'

export interface AgentNotice {
  type: 'agent_event'
  project_id: string
  session_id: string
  seq: number
  status: SessionStatus
  title: string
  summary: string
}
export class AgentDeviceBridge {
  constructor(private manager: Manager, private notify: (event: AgentNotice) => Promise<unknown>) {}

  async dispatch(command: Record<string, unknown>) {
    const id = typeof command.id === 'string' ? command.id : ''
    try {
      if (!id || typeof command.action !== 'string') throw new Error('Invalid agent command')
      const params = command.params ?? {}
      if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('Invalid agent parameters')
      return { id, ok: true, result: await this.execute(command.action, params as Record<string, unknown>) }
    } catch (error) {
      return { id, ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  }
  private text(params: Record<string, unknown>, name: string) {
    const value = params[name]
    if (typeof value !== 'string' || !value.trim()) throw new Error(`${name} is required`)
    return value
  }
  private async execute(action: string, params: Record<string, unknown>): Promise<unknown> {
    if (action === 'agent.list') return this.manager.snapshot()
    const projectId = this.text(params, 'project_id')
    const snapshot = await this.manager.snapshot()
    const project = snapshot.projects.find((item) => item.id === projectId)
    if (!project) throw new Error('Unknown project; list registered projects before routing')
    if (action === 'agent.create') {
      const provider = this.text(params, 'provider')
      if (provider !== 'codex' && provider !== 'claude') throw new Error('Voice sessions require Codex or Claude')
      const title = typeof params.title === 'string' ? params.title.slice(0, 120) : undefined
      return this.once(action, params, { projectId, provider, title }, () =>
        this.manager.createSession({ projectId, provider, title, worktreePath: project.path }))
    }
    if (!['agent.send', 'agent.session', 'agent.stop'].includes(action)) throw new Error('Unsupported agent action')
    const sessionId = this.text(params, 'session_id')
    const detail = await this.manager.session(sessionId)
    if (detail.session.projectId !== projectId) throw new Error('Session does not belong to this project')
    if (action === 'agent.session') {
      const afterValue = params.after_seq ?? 0
      if (typeof afterValue !== 'number') throw new Error('after_seq must be a number')
      const after = afterValue
      if (!Number.isSafeInteger(after) || (after as number) < 0) throw new Error('after_seq must be a nonnegative integer')
      const events: SessionEvent[] = []
      let bytes = 0
      for (const item of detail.events.filter((item) => item.seq > (after as number))) {
        const event = { ...item, text: item.text.slice(0, 8192) }
        const size = Buffer.byteLength(JSON.stringify(event))
        if (events.length >= 100 || bytes + size > 128 * 1024) break
        events.push(event)
        bytes += size
      }
      const first = detail.events[0]?.seq ?? 0
      return { session: detail.session, events, next_seq: events.at(-1)?.seq ?? after,
        truncated: first > (after as number) + 1, has_more: (events.at(-1)?.seq ?? after) < (detail.events.at(-1)?.seq ?? 0) }
    }
    if (action === 'agent.stop') {
      await this.manager.stop(sessionId)
      return { session: (await this.manager.session(sessionId)).session }
    }
    const prompt = this.text(params, 'prompt')
    if (prompt.length > 100000) throw new Error('Prompt is too long')
    return this.once(action, params, { projectId, sessionId, prompt }, async () => {
      await this.manager.send(sessionId, prompt)
      return { accepted: true, project_id: projectId, session_id: sessionId }
    })
  }
  private once(action: string, params: Record<string, unknown>, normalized: object, execute: () => Promise<unknown>) {
    const requestId = this.text(params, 'request_id')
    const fingerprint = createHash('sha256').update(JSON.stringify({ action, ...normalized })).digest('hex')
    return this.manager.deviceRequest(requestId, fingerprint, execute)
  }
  async update(update: BuddyUpdate) {
    if (update.type !== 'event' || update.event.type !== 'status') return
    if (!['completed', 'needs_input', 'error'].includes(update.event.text)) return
    await this.publishSession(update.event.sessionId)
  }
  async reconnect() {
    const snapshot = await this.manager.snapshot()
    for (const session of snapshot.sessions) {
      if (session.unread && ['completed', 'needs_input', 'error'].includes(session.status))
        await this.publishSession(session.id)
    }
  }
  private async publishSession(id: string) {
    const { session, events } = await this.manager.session(id)
    const statusEvent = [...events].reverse().find((event) => event.type === 'status')
    if (!statusEvent || !['completed', 'needs_input', 'error'].includes(session.status)) return
    await this.notify({
      type: 'agent_event', project_id: session.projectId, session_id: session.id,
      seq: statusEvent.seq, status: session.status, title: session.title,
      summary: [...events].reverse().find((event) => ['result', 'output', 'error'].includes(event.type))?.text.slice(0, 1200) ?? '',
    })
  }
}
