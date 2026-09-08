import { Asterisk, Sparkles, Terminal } from 'lucide-react'
import type { Provider } from '../shared/types'

export const providerName = (provider: Provider) =>
  ({ codex: 'Codex', claude: 'Claude Code', terminal: 'Terminal' })[provider]

export function ProviderIcon({ provider, size = 16 }: { provider: Provider; size?: number }) {
  const Icon = provider === 'claude' ? Asterisk : provider === 'codex' ? Sparkles : Terminal
  return <Icon size={size} className={`agent-provider-icon ${provider}`} aria-hidden="true" />
}

/** Keep provider identity visible even when the task title is truncated. */
export function SessionIdentity({ provider, title }: { provider: Provider; title: string }) {
  return <>
    <ProviderIcon provider={provider} size={13} />
    <span className="agent-provider-name">{providerName(provider)}</span>
    <span className="agent-task-title">{title}</span>
  </>
}
