export interface ProviderUsage {
  provider: 'claude' | 'codex'
  state: 'ready' | 'unavailable' | 'error'
  message?: string
  windows: { label: string; usedPercent: number; resetsAt?: number }[]
  updatedAt: number
}
