import { describe, it, expect } from 'vitest'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import { createInteractiveHooks, codexHookTrustArgs, normalizeInteractiveHook, type InteractiveHookEvent } from '../src/main/interactive-hooks'

describe('session-local interactive hooks', () => {
  it('normalizes lifecycle without treating children or unknown events as completion', () => {
    expect(normalizeInteractiveHook({ hook_event_name: 'SessionStart', session_id: 'uuid-1' })).toEqual({ type: 'ready', sessionId: 'uuid-1' })
    expect(normalizeInteractiveHook({ hook_event_name: 'UserPromptSubmit' })?.type).toBe('working')
    expect(normalizeInteractiveHook({ hook_event_name: 'UserPromptSubmit', prompt: ' Fix\n  tab identity\u001b ' })?.prompt).toBe('Fix tab identity')
    expect(normalizeInteractiveHook({ hook_event_name: 'PreToolUse', prompt: 'ignore tool text' })?.prompt).toBeUndefined()
    expect(normalizeInteractiveHook({ hook_event_name: 'PreToolUse', tool_name: 'AskUserQuestion' })?.type).toBe('needs_input')
    expect(normalizeInteractiveHook({ hook_event_name: 'StopFailure' })?.type).toBe('error')
    expect(normalizeInteractiveHook({ hook_event_name: 'Stop', agent_id: 'child' })).toBeUndefined()
    expect(normalizeInteractiveHook({ hook_event_name: 'PostCompact', trigger: 'auto' })).toBeUndefined()
    expect(normalizeInteractiveHook({ hook_event_name: 'Stop', session_id: 'bad\nidentity', last_assistant_message: 'a'.repeat(5000) })).toEqual({ type: 'completed', summary: 'a'.repeat(4000) })
  })
  it('captures hook stdin through the real isolated script and stops after disposal', async () => {
    const root = mkdtempSync(join(tmpdir(), 'buddy-hooks-test-'))
    const events: InteractiveHookEvent[] = []
    const bridge = createInteractiveHooks({ provider: 'claude', directory: root, onEvent: (event) => events.push(event) })
    try {
      expect(bridge.args[0]).toBe('--settings')
      await new Promise<void>((resolve, reject) => {
        const child = spawn('/bin/sh', ['-c', bridge.command], { stdio: ['pipe', 'ignore', 'pipe'] })
        child.on('error', reject)
        child.on('exit', (code) => code === 0 ? resolve() : reject(new Error('Hook process failed')))
        child.stdin.end(JSON.stringify({ hook_event_name: 'Stop', session_id: 'lead-1', last_assistant_message: 'Đã xong', secret: 'never-copied' }))
      })
      await expect.poll(() => events).toEqual([{ type: 'completed', sessionId: 'lead-1', summary: 'Đã xong' }])
      bridge.dispose()
      bridge.dispose()
    } finally { bridge.dispose(); rmSync(root, { recursive: true, force: true }) }
  })
})


describe('Codex scoped hook trust', () => {
  it('trusts only matching session-flag commands using one inline state table', async () => {
    const root = mkdtempSync(join(tmpdir(), 'buddy-hook-trust-'))
    const executable = join(root, 'fake-codex')
    const hooks = [
      { command: 'our-command', key: '<session-flags>/config.toml:SessionStart.0', currentHash: 'hash-one' },
      { command: 'our-command', key: '<session-flags>/config.toml:Stop.0', currentHash: 'hash-two' },
      { command: 'other-command', key: '<session-flags>/config.toml:Stop.1', currentHash: 'unrelated' },
      { command: 'our-command', key: '/home/config.toml:Stop.0', currentHash: 'global' },
    ]
    writeFileSync(executable, '#!' + process.execPath + '\n' +
      `require('node:readline').createInterface({input:process.stdin}).on('line',line=>{const v=JSON.parse(line);if(v.id===1)console.log(JSON.stringify({id:1,result:{}}));if(v.id===2)console.log(JSON.stringify({id:2,result:{data:[{hooks:${JSON.stringify(hooks)}}]}}))})`, { mode: 0o700 })
    try {
      const args = await codexHookTrustArgs([], 'our-command', root, executable)
      expect(args).toEqual(['-c', 'hooks.state={"<session-flags>/config.toml:SessionStart.0"={trusted_hash="hash-one"},"<session-flags>/config.toml:Stop.0"={trusted_hash="hash-two"}}'])
      await expect(codexHookTrustArgs([], 'missing', root, executable)).rejects.toThrow('did not discover')
    } finally { rmSync(root, { recursive: true, force: true }) }
  })
})
