import { useState, type ReactNode } from 'react'
import {
  ArrowLeft,
  Bot,
  Check,
  ChevronDown,
  Monitor,
  Palette,
  Search,
  Settings2,
  Terminal,
  X,
} from 'lucide-react'
import type { AppearanceSettings, Project, Snapshot } from '../shared/types'
import { useAppearance } from './useAppearance'
import './SettingsPage.css'

type Section = 'appearance' | 'agents' | 'computer'
export function SettingsPage({
  providers,
  onClose,
  onComputer,
  project,
  onRemoveProject,
}: {
  providers: Snapshot['providers']
  onClose: () => void
  onComputer: () => void
  project?: Project
  onRemoveProject: () => Promise<void>
}) {
  const { loading, appearance, update, error } = useAppearance()
  const [section, setSection] = useState<Section>('appearance')
  const [query, setQuery] = useState('')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const tokens = query.toLowerCase().trim().split(/\s+/).filter(Boolean)
  const matches = (...text: string[]) => tokens.every((token) => text.join(' ').toLowerCase().includes(token))
  const save = async (patch: Partial<AppearanceSettings>) => {
    setSaving(true)
    setFailure('')
    try {
      await update(patch)
    } catch (value) {
      setFailure(value instanceof Error ? value.message : String(value))
    } finally {
      setSaving(false)
    }
  }
  const disabled = saving || loading || !!error
  const row = (group: string, label: string, description: string, control: ReactNode) =>
    matches(group, label, description) ? (
      <div className="setting-row" key={label}>
        <div>
          <strong>{label}</strong>
          <p>{description}</p>
        </div>
        <div className="setting-control">{control}</div>
      </div>
    ) : null
  const stepper = (
    label: string,
    value: number,
    min: number,
    max: number,
    step: number,
    apply: (value: number) => void,
    percent = false,
  ) => (
    <SettingsNumber
      key={label}
      label={label}
      value={value}
      min={min}
      max={max}
      step={step}
      apply={apply}
      percent={percent}
      disabled={loading || !!error}
    />
  )
  const toggle = (label: string, checked: boolean, apply: (value: boolean) => void) => (
    <button
      type="button"
      role="switch"
      aria-label={label}
      aria-checked={checked}
      className={`settings-switch ${checked ? 'on' : ''}`}
      disabled={disabled}
      onClick={() => apply(!checked)}
    >
      <span />
    </button>
  )
  const interfaceRows = [
    row(
      'Appearance Interface',
      'Theme',
      'Follow system appearance, or choose dark or light.',
      <div className="settings-segmented" role="group" aria-label="Theme">
        {(['system', 'dark', 'light'] as const).map((theme) => (
          <button
            key={theme}
            type="button"
            aria-pressed={appearance.theme === theme}
            disabled={disabled}
            onClick={() => void save({ theme })}
          >
            {theme[0].toUpperCase() + theme.slice(1)}
          </button>
        ))}
      </div>,
    ),
    row(
      'Appearance Interface',
      'UI zoom',
      'Scale the entire interface. Default 100%.',
      stepper('UI zoom', appearance.zoom, 0.75, 1.5, 0.05, (zoom) => void save({ zoom }), true),
    ),
    row(
      'Appearance Interface',
      'IDE font',
      'Font used by the workspace interface.',
      <select
        aria-label="IDE font"
        value={appearance.uiFont}
        disabled={disabled}
        onChange={(event) => void save({ uiFont: event.target.value as AppearanceSettings['uiFont'] })}
      >
        <option value="system">System default</option>
        <option value="sans">Sans serif</option>
        <option value="mono">Monospace</option>
      </select>,
    ),
  ].filter(Boolean)
  const terminalRows = [
    row(
      'Appearance Terminal',
      'Font size',
      'Terminal text size in pixels. Default 13.',
      stepper(
        'Terminal font size',
        appearance.terminalFontSize,
        10,
        24,
        1,
        (terminalFontSize) => void save({ terminalFontSize }),
      ),
    ),
    row(
      'Appearance Terminal',
      'Font family',
      'Use a local monospaced font. Missing fonts fall back to monospace.',
      <select
        aria-label="Terminal font family"
        value={appearance.terminalFontFamily}
        disabled={disabled}
        onChange={(event) =>
          void save({ terminalFontFamily: event.target.value as AppearanceSettings['terminalFontFamily'] })
        }
      >
        {(['SF Mono', 'Menlo', 'Monaco', 'monospace'] as const).map((font) => (
          <option key={font} value={font}>
            {font}
          </option>
        ))}
      </select>,
    ),
  ].filter(Boolean)
  const advancedRows = [
    row(
      'Appearance Terminal Advanced',
      'Line height',
      'Spacing between terminal lines. Default 1.4.',
      stepper(
        'Terminal line height',
        appearance.terminalLineHeight,
        1,
        2,
        0.1,
        (terminalLineHeight) => void save({ terminalLineHeight }),
      ),
    ),
    row(
      'Appearance Terminal Advanced',
      'Cursor blink',
      'Animate the cursor in the focused terminal.',
      toggle('Cursor blink', appearance.cursorBlink, (cursorBlink) => void save({ cursorBlink })),
    ),
    row(
      'Appearance Terminal Advanced',
      'Cursor style',
      'Choose a block, bar or underline cursor.',
      <select
        aria-label="Cursor style"
        value={appearance.cursorStyle}
        disabled={disabled}
        onChange={(event) =>
          void save({ cursorStyle: event.target.value as AppearanceSettings['cursorStyle'] })
        }
      >
        <option value="block">Block</option>
        <option value="bar">Bar</option>
        <option value="underline">Underline</option>
      </select>,
    ),
  ].filter(Boolean)
  const statusRows = [
    row(
      'Appearance Status bar',
      'Agent usage',
      'Show Claude and Codex quota usage at the bottom of the app.',
      toggle('Show agent usage', appearance.showUsage, (showUsage) => void save({ showUsage })),
    ),
    row(
      'Appearance Status bar',
      'Workspace status',
      'Show the current worktree and agent status at the bottom.',
      toggle(
        'Show workspace status',
        appearance.showStatusBar,
        (showStatusBar) => void save({ showStatusBar }),
      ),
    ),
  ].filter(Boolean)
  const showAppearance =
    (!tokens.length && section === 'appearance') ||
    (tokens.length > 0 &&
      !!(interfaceRows.length + terminalRows.length + advancedRows.length + statusRows.length))
  const showAgents =
    (!tokens.length && section === 'agents') ||
    (tokens.length > 0 &&
      matches('Agents Codex Claude Code Terminal installed CLI account access permissions local project'))
  const showComputer =
    (!tokens.length && section === 'computer') ||
    (tokens.length > 0 &&
      matches('Computer device pairing permissions accessibility screenshot native Swift'))
  return (
    <section
      className="settings-page"
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
      onKeyDown={(event) => {
        if (event.key !== 'Tab') return
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]',
          ),
        ].filter((item) => item.offsetParent !== null)
        const first = controls[0],
          last = controls.at(-1)
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last?.focus()
        }
        if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first?.focus()
        }
      }}
    >
      <aside className="settings-navigation">
        <button className="settings-back" onClick={onClose}>
          <ArrowLeft size={15} /> Back to app <kbd>Esc</kbd>
        </button>
        <h1>Settings</h1>
        <div className="settings-search">
          <Search size={14} />
          <input
            autoFocus
            aria-label="Search settings"
            placeholder="Search settings…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && (
            <button aria-label="Clear settings search" onClick={() => setQuery('')}>
              <X size={12} />
            </button>
          )}
        </div>
        <nav aria-label="Settings sections">
          {(
            [
              { id: 'appearance', label: 'Appearance', icon: Palette },
              { id: 'agents', label: 'Agents', icon: Bot },
              { id: 'computer', label: 'Computer & device', icon: Monitor },
            ] as const
          ).map((item) => (
            <button
              key={item.id}
              className={section === item.id && !query ? 'selected' : ''}
              aria-current={section === item.id && !query ? 'page' : undefined}
              onClick={() => {
                setSection(item.id)
                setQuery('')
              }}
            >
              <item.icon size={15} />
              {item.label}
            </button>
          ))}
        </nav>
        <div className="settings-sidebar-foot">
          <Settings2 size={14} /> Autonomous Buddy<small>Preferences saved on this Mac</small>
        </div>
      </aside>
      <div className="settings-content">
        <header>
          <div>
            <h2>
              {query
                ? 'Search results'
                : section === 'appearance'
                  ? 'Appearance'
                  : section === 'agents'
                    ? 'Agents'
                    : 'Computer & device'}
            </h2>
            <p>{query ? `Settings matching “${query}”` : 'Make this workspace feel like yours.'}</p>
          </div>
          <span className="settings-save-state">
            {loading ? (
              'Loading…'
            ) : saving ? (
              'Saving…'
            ) : failure || error ? (
              'Changes not saved'
            ) : (
              <>
                <Check size={12} /> Saved automatically
              </>
            )}
          </span>
        </header>
        {(failure || error) && (
          <p className="settings-error" role="alert">
            {failure || error}
          </p>
        )}
        {showAppearance && (
          <>
            {interfaceRows.length > 0 && (
              <SettingsCard
                title="Interface"
                subtitle="Theme, scale, and typography"
                icon={<Palette size={17} />}
              >
                {interfaceRows}
              </SettingsCard>
            )}
            {(terminalRows.length > 0 || advancedRows.length > 0) && (
              <SettingsCard
                title="Terminal"
                subtitle="Readable text and a cursor that suits your work"
                icon={<Terminal size={17} />}
              >
                {terminalRows}
                {advancedRows.length > 0 && (
                  <>
                    <button
                      className="settings-advanced"
                      aria-expanded={advanced || tokens.length > 0}
                      onClick={() => setAdvanced(!advanced)}
                    >
                      <ChevronDown size={13} /> Advanced terminal settings
                    </button>
                    {(advanced || tokens.length > 0) && advancedRows}
                  </>
                )}
              </SettingsCard>
            )}
            {statusRows.length > 0 && (
              <SettingsCard
                title="Status bar"
                subtitle="Keep the details you need in view"
                icon={<Settings2 size={17} />}
              >
                {statusRows}
              </SettingsCard>
            )}
          </>
        )}
        {showAgents && (
          <SettingsCard
            title="Agent installations"
            subtitle="Use the CLI accounts already installed on this Mac"
            icon={<Bot size={17} />}
          >
            {providers.map((provider) => (
              <div className="setting-row" key={provider.id}>
                <div>
                  <strong>
                    {{ codex: 'Codex', claude: 'Claude Code', terminal: 'Terminal' }[provider.id]}
                  </strong>
                  <p>
                    {provider.id === 'terminal'
                      ? 'Interactive local shell'
                      : 'Sign in using this provider’s CLI on your Mac.'}
                  </p>
                </div>
                <span className={provider.available ? 'settings-available' : 'settings-unavailable'}>
                  {provider.available ? 'Available' : 'Not installed'}
                </span>
              </div>
            ))}
            <p className="settings-explanation">
              Agent sessions run with the configured local CLI access. Current coding sessions use full access
              without per-command approval prompts. Computer control has its own native pairing and
              permissions.
            </p>
            {project && (
              <div className="settings-project">
                <strong>{project.name}</strong>
                <p>{project.path}</p>
                {confirmRemove ? (
                  <>
                    <p>
                      Remove this project registration and its sessions? Files on disk are kept. Stop running
                      sessions first.
                    </p>
                    <button
                      className="settings-danger"
                      disabled={saving}
                      onClick={async () => {
                        setSaving(true)
                        try {
                          await onRemoveProject()
                          setConfirmRemove(false)
                        } catch (value) {
                          setFailure(value instanceof Error ? value.message : String(value))
                        } finally {
                          setSaving(false)
                        }
                      }}
                    >
                      Confirm removal
                    </button>
                    <button onClick={() => setConfirmRemove(false)}>Cancel</button>
                  </>
                ) : (
                  <button className="settings-danger" onClick={() => setConfirmRemove(true)}>
                    Remove project from workspace
                  </button>
                )}
              </div>
            )}
          </SettingsCard>
        )}
        {showComputer && (
          <SettingsCard
            title="Computer & device"
            subtitle="Native controls inside the same app"
            icon={<Monitor size={17} />}
          >
            <p className="settings-explanation">
              Manage device pairing, Accessibility and Screen Recording permissions, pause computer control,
              or inspect activity.
            </p>
            <button className="settings-open-computer" onClick={onComputer}>
              <Monitor size={15} /> Open computer & device settings
            </button>
          </SettingsCard>
        )}
        {!showAppearance && !showAgents && !showComputer && (
          <div className="settings-no-results">
            <Search size={25} />
            <h3>No matching settings</h3>
            <p>Try theme, zoom, font, cursor, usage, agent, or pairing.</p>
          </div>
        )}
      </div>
    </section>
  )
}
function SettingsCard({
  title,
  subtitle,
  icon,
  children,
}: {
  title: string
  subtitle: string
  icon: ReactNode
  children: ReactNode
}) {
  return (
    <section className="settings-card" aria-label={title}>
      <header>
        {icon}
        <div>
          <h3>{title}</h3>
          <p>{subtitle}</p>
        </div>
      </header>
      <div>{children}</div>
    </section>
  )
}

function SettingsNumber({
  label,
  value,
  min,
  max,
  step,
  apply,
  percent,
  disabled,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  apply: (value: number) => void
  percent: boolean
  disabled: boolean
}) {
  const display = percent ? Math.round(value * 100) : value
  const [draft, setDraft] = useState<string | null>(null)
  const normalize = (input: number) =>
    Number(Math.min(max, Math.max(min, Math.round(input / step) * step)).toFixed(2))
  const commit = () => {
    const parsed = Number(draft ?? display) / (percent ? 100 : 1)
    if (draft?.trim() === '' || !Number.isFinite(parsed)) {
      setDraft(null)
      return
    }
    const next = normalize(parsed)
    setDraft(null)
    if (next !== value) apply(next)
  }
  return (
    <div className="settings-stepper">
      <button
        type="button"
        aria-label={`Decrease ${label}`}
        disabled={disabled || value <= min}
        onClick={() => apply(normalize(value - step))}
      >
        −
      </button>
      <input
        aria-label={label}
        type="number"
        min={percent ? min * 100 : min}
        max={percent ? max * 100 : max}
        step={percent ? step * 100 : step}
        value={draft ?? display}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            event.currentTarget.blur()
          }
        }}
      />
      {percent && <span>%</span>}
      <button
        type="button"
        aria-label={`Increase ${label}`}
        disabled={disabled || value >= max}
        onClick={() => apply(normalize(value + step))}
      >
        +
      </button>
    </div>
  )
}
