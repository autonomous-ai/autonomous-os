import { mkdtempSync, mkdirSync, writeFileSync, openSync, readSync, closeSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { StringDecoder } from 'node:string_decoder'

export interface InteractiveHookEvent {
  type: 'ready' | 'working' | 'completed' | 'needs_input' | 'error'
  sessionId?: string
  summary?: string
}
export function normalizeInteractiveHook(value: unknown): InteractiveHookEvent | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return
  const data = value as Record<string, unknown>
  // Child/subagent hooks must never complete the owning pane's lead turn.
  if (typeof data.agent_id === 'string' && data.agent_id) return
  const name = data.hook_event_name
  let type: InteractiveHookEvent['type']
  if (name === 'SessionStart') type = 'ready'
  else if (name === 'UserPromptSubmit' || name === 'PostToolUse') type = 'working'
  else if (name === 'PermissionRequest' || (name === 'PreToolUse' && data.tool_name === 'AskUserQuestion')) type = 'needs_input'
  else if (name === 'PreToolUse') type = 'working'
  else if (name === 'Stop' || (name === 'PostCompact' && data.trigger === 'manual')) type = 'completed'
  else if (name === 'StopFailure') type = 'error'
  else return
  const sessionId = typeof data.session_id === 'string' && /^[a-zA-Z0-9_-]{1,512}$/.test(data.session_id) ? data.session_id : undefined
  const summary = typeof data.last_assistant_message === 'string' ? data.last_assistant_message.slice(0, 4000) : undefined
  return { type, ...(sessionId ? { sessionId } : {}), ...(summary ? { summary } : {}) }
}
const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'"

/** Session-local provider hooks; never writes user/project provider configuration. */
export function createInteractiveHooks(options: {
  provider: 'claude' | 'codex'
  directory: string
  onEvent: (event: InteractiveHookEvent) => void
}) {
  mkdirSync(options.directory, { recursive: true, mode: 0o700 })
  const directory = mkdtempSync(join(options.directory, 'hooks-'))
  const script = join(directory, 'capture.cjs')
  const eventsPath = join(directory, 'events.jsonl')
  writeFileSync(eventsPath, '', { mode: 0o600 })
  // Bounded hook input and normalized output only; provider stdout remains its real TUI.
  writeFileSync(script, `const fs=require('node:fs');let input='',bytes=0;process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>{bytes+=Buffer.byteLength(chunk);if(bytes>1048576)process.exit(0);input+=chunk});process.stdin.on('end',()=>{try{const v=JSON.parse(input);const out={};for(const k of ['hook_event_name','session_id','agent_id','tool_name','trigger','last_assistant_message'])if(typeof v[k]==='string')out[k]=v[k].slice(0,4000);fs.appendFileSync(process.argv[2],JSON.stringify(out)+'\\n',{mode:0o600})}catch{}});`, { mode: 0o600 })
  const command = `ELECTRON_RUN_AS_NODE=1 ${shellQuote(process.execPath)} ${shellQuote(script)} ${shellQuote(eventsPath)}`
  const names = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PermissionRequest', 'Stop', ...(options.provider === 'claude' ? ['StopFailure', 'PostCompact'] : [])]
  const hooks = Object.fromEntries(names.map((name) => [name, [{ hooks: [{ type: 'command', command, timeout: 3 }] }]]))
  const settingsPath = join(directory, 'settings.json')
  writeFileSync(settingsPath, JSON.stringify({ hooks }), { mode: 0o600 })
  const codexArgs = names.flatMap((name) => ['-c', `hooks.${name}=[{hooks=[{type="command",command=${JSON.stringify(command)},timeout=3}]}]`])
  let fd: number | undefined = openSync(eventsPath, 'r')
  let position = 0, buffer = ''
  const decoder = new StringDecoder('utf8')
  const poll = () => {
    if (fd === undefined) return
    const chunk = Buffer.alloc(65536)
    try {
      const count = readSync(fd, chunk, 0, chunk.length, position)
      position += count
      buffer += decoder.write(chunk.subarray(0, count))
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        try {
          const event = normalizeInteractiveHook(JSON.parse(line))
          if (event) options.onEvent(event)
        } catch { /* Ignore malformed provider hook frames. */ }
      }
      if (buffer.length > 1048576) buffer = ''
    } catch { /* Provider shutdown may race the final poll. */ }
  }
  const timer = setInterval(poll, 100)
  timer.unref()
  return {
    args: options.provider === 'claude' ? ['--settings', settingsPath] : codexArgs,
    env: {} as Record<string, string>,
    command,
    dispose() {
      if (fd === undefined) return
      clearInterval(timer)
      poll()
      closeSync(fd)
      fd = undefined
      rmSync(directory, { recursive: true, force: true })
    },
  }
}

/** Ask Codex for its own hashes; trust only our exact command through CLI overrides. */
export async function codexHookTrustArgs(args: string[], command: string, cwd: string, executable = 'codex'): Promise<string[]> {
  const { spawn } = await import('node:child_process')
  return new Promise((resolve, reject) => {
    const env = { ...process.env }
    for (const key of ['CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CODEX_THREAD_ID']) delete env[key]
    const child = spawn(executable, ['app-server', ...args], { cwd, env, stdio: 'pipe' })
    let buffer = '', bytes = 0, settled = false
    const finish = (error?: Error, result?: string[]) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      child.stdin.end()
      child.kill()
      const kill = setTimeout(() => child.kill('SIGKILL'), 1000)
      kill.unref()
      child.once('close', () => clearTimeout(kill))
      if (error) reject(error)
      else resolve(result ?? [])
    }
    const timer = setTimeout(() => finish(new Error('Codex hook discovery timed out')), 10000)
    child.stderr.resume()
    child.on('error', () => finish(new Error('Codex hook discovery could not start')))
    child.on('close', () => { if (!settled) finish(new Error('Codex hook discovery exited')) })
    child.stdin.on('error', () => finish(new Error('Codex hook discovery input closed')))
    const send = (value: object) => child.stdin.write(JSON.stringify(value) + '\n')
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => {
      bytes += Buffer.byteLength(chunk)
      if (bytes > 1048576) { finish(new Error('Codex hook discovery exceeded limit')); return }
      buffer += chunk
      let newline: number
      while (!settled && (newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline)
        buffer = buffer.slice(newline + 1)
        let response: { id?: number; error?: unknown; result?: { data?: { hooks?: Record<string, unknown>[] }[] } }
        try { response = JSON.parse(line) } catch { continue }
        if (response.error && (response.id === 1 || response.id === 2)) { finish(new Error('Codex does not support scoped hook discovery')); return }
        if (response.id === 1) {
          send({ method: 'initialized' })
          send({ id: 2, method: 'hooks/list', params: { cwds: [cwd] } })
        }
        if (response.id === 2) {
          const data = response.result?.data
          const hooks = Array.isArray(data) ? data.flatMap((item) => item && Array.isArray(item.hooks) ? item.hooks : []) : []
          const trust = new Map<string, string>()
          for (const hook of hooks) {
            if (hook && hook.command === command && typeof hook.key === 'string' && hook.key.includes('<session-flags>') &&
              typeof hook.currentHash === 'string' && hook.currentHash.length < 256)
              trust.set(hook.key, hook.currentHash)
          }
          if (!trust.size) { finish(new Error('Codex did not discover session-local hooks')); return }
          const table = [...trust].map(([key, hash]) => `${JSON.stringify(key)}={trusted_hash=${JSON.stringify(hash)}}`).join(',')
          finish(undefined, ['-c', `hooks.state={${table}}`])
        }
      }
    })
    send({ id: 1, method: 'initialize', params: { clientInfo: { name: 'autonomous-buddy-hooks', version: '1' }, capabilities: { experimentalApi: true } } })
  })
}
