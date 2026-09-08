import { describe, expect, it, vi } from 'vitest'
import { normalizeClaudeUsage, normalizeCodexUsage, ProviderUsageService, readCodexUsage, readClaudeUsage, normalizeClaudeOAuthUsage } from '../src/main/provider-usage'

describe('provider quota snapshots', () => {
  it('uses official percentages and epoch seconds, preserving zero and missing windows', () => {
    expect(normalizeCodexUsage({ rateLimits: { primary: { usedPercent: 0, windowDurationMins: 300, resetsAt: 1800000000 }, secondary: { usedPercent: 99, windowDurationMins: 10080 } } }).windows)
      .toEqual([{ label: '5h', usedPercent: 0, resetsAt: 1800000000000 }, { label: '7d', usedPercent: 99 }])
    expect(normalizeCodexUsage({ rateLimits: { primary: { usedPercent: 101 } } }).state).toBe('unavailable')
    expect(normalizeCodexUsage({}).windows).toEqual([])
    expect(normalizeClaudeUsage({ rate_limits: { five_hour: { used_percentage: 27, resets_at: 1800000000 } } }).windows)
      .toEqual([{ label: '5h', usedPercent: 27, resetsAt: 1800000000000 }])
    expect(normalizeClaudeUsage({ cost: { total_cost_usd: 42 } }).state).toBe('unavailable')
  })

  it('caches for 60 seconds, coalesces refreshes and isolates failures', async () => {
    let now = 1
    const codex = vi.fn(async () => normalizeCodexUsage({}))
    const claude = vi.fn(async () => { throw new Error('sensitive diagnostic') })
    const service = new ProviderUsageService({ codex, claude }, () => now)
    const initial = await Promise.all([service.read(), service.read(true)])
    expect(codex).toHaveBeenCalledTimes(1)
    expect(JSON.stringify(initial)).not.toContain('sensitive')
    await service.read()
    expect(codex).toHaveBeenCalledTimes(1)
    now += 60000
    await service.read()
    await service.read(true)
    expect(codex).toHaveBeenCalledTimes(3)
  })

  it('performs initialize then rate-limit RPC without a model prompt', async () => {
    const script = `
      const readline = require('node:readline');
      let initialized = false;
      readline.createInterface({ input: process.stdin }).on('line', line => {
        const request = JSON.parse(line);
        if (request.method === 'initialize') process.stdout.write(JSON.stringify({id:1,result:{}})+'\\n');
        else if (request.method === 'initialized') initialized = true;
        else if (request.method === 'account/rateLimits/read' && initialized) process.stdout.write(JSON.stringify({id:2,result:{rateLimits:{primary:{usedPercent:32,windowDurationMins:300}}}})+'\\n');
        else process.exit(9);
      });`
    const result = await readCodexUsage({ executable: process.execPath, args: ['-e', script], timeoutMs: 2000 })
    expect(result).toMatchObject({ state: 'ready', windows: [{ label: '5h', usedPercent: 32 }] })
  })

  it('bounds failed probes and never returns server error text', async () => {
    const denied = await readCodexUsage({ executable: process.execPath, args: ['-e', `process.stdout.write(JSON.stringify({id:1,error:{message:'secret-token'}})+'\\n');setInterval(()=>{},1000)`], timeoutMs: 2000 })
    expect(denied.state).toBe('unavailable')
    expect(JSON.stringify(denied)).not.toContain('secret-token')
    const timeout = await readCodexUsage({ executable: process.execPath, args: ['-e', 'setInterval(()=>{},1000)'], timeoutMs: 100 })
    expect(timeout.message).toContain('timed out')
  })
})


describe('Claude read-only quota compatibility', () => {
  it('uses fixed official host and no redirects, returns only quota fields', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({
      five_hour: { utilization: 31, resets_at: '2027-01-15T08:00:00Z' },
      seven_day: { utilization: 44 }, secret: 'do-not-return',
    })))
    const usage = await readClaudeUsage({ token: async () => 'fixture-token', fetcher })
    expect(fetcher.mock.calls[0]).toMatchObject(['https://api.anthropic.com/api/oauth/usage', { redirect: 'error', method: 'GET', headers: { Authorization: 'Bearer fixture-token' } }])
    expect(usage.windows).toEqual([{ label: '5h', usedPercent: 31, resetsAt: Date.parse('2027-01-15T08:00:00Z') }, { label: '7d', usedPercent: 44 }])
    expect(JSON.stringify(usage)).not.toMatch(/fixture-token|do-not-return/)
  })

  it('does not fetch without credentials and sanitizes auth/transport/oversize errors', async () => {
    const fetcher = vi.fn(async () => new Response('secret body', { status: 401 }))
    expect((await readClaudeUsage({ token: async () => undefined, fetcher })).state).toBe('unavailable')
    expect(fetcher).not.toHaveBeenCalled()
    expect((await readClaudeUsage({ token: async () => 'fixture-token', fetcher })).message).toContain('Sign in')
    expect(JSON.stringify(await readClaudeUsage({ token: async () => { throw new Error('secret token') }, fetcher }))).not.toContain('secret token')
    const large = await readClaudeUsage({ token: async () => 'fixture-token', fetcher: async () => new Response('x'.repeat(65537)) })
    expect(large.message).toContain('size limit')
  })
})


it('preserves optional model quotas and prefers scoped values over aliases', () => {
  const timestamp = '2026-07-17T20:00:00.099908+00:00'
  const usage = normalizeClaudeOAuthUsage({
    five_hour: { utilization: 11 }, seven_day: { utilization: 22 }, fable_weekly: { utilization: 33 },
    limits: [
      { kind: 'weekly_scoped', percent: 14, resets_at: timestamp, scope: { model: { display_name: 'Fable' } } },
      { kind: 'weekly_scoped', percent: 2, scope: { model: { display_name: 'Sonnet' } } },
      { kind: 'weekly_scoped', percent: 66, scope: null },
    ],
  })
  expect(usage.windows).toEqual([
    { label: '5h', usedPercent: 11 }, { label: '7d', usedPercent: 22 },
    { label: 'Fable 7d', usedPercent: 14, resetsAt: Date.parse(timestamp) },
    { label: 'Sonnet 7d', usedPercent: 2 },
  ])
  for (const key of ['fable_weekly', 'fable_seven_day', 'seven_day_fable'])
    expect(normalizeClaudeOAuthUsage({ [key]: { used_percentage: 0 } }).windows).toEqual([{ label: 'Fable 7d', usedPercent: 0 }])
  expect(normalizeClaudeOAuthUsage({ fable: { utilization: 14 } }).windows).toEqual([])
  expect(normalizeClaudeOAuthUsage({ limits: [{ kind: 'daily_scoped', percent: 14, scope: { model: { display_name: 'Fable' } } }] }).windows).toEqual([])
})
