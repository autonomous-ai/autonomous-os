import type { AutoUpdater, MessageBoxOptions } from 'electron'

export const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000
export const UPDATE_START_DELAY_MS = 30 * 1000

export function updateFeedURL(arch: string): string {
  if (arch !== 'arm64' && arch !== 'x64') throw new Error('Unsupported update architecture')
  return `https://storage.googleapis.com/s3-autonomous-upgrade-3/os/ota/autonomous-buddy/${arch}/latest.json`
}

type Updater = Pick<AutoUpdater, 'on' | 'removeListener' | 'setFeedURL' | 'checkForUpdates'>
type Phase = 'idle' | 'checking' | 'downloading' | 'ready' | 'installing'
export interface UpdateMenuState { label: string; enabled: boolean }
interface Options {
  updater: Updater
  supported: boolean
  arch: string
  version: string
  showMessage: (options: MessageBoxOptions) => Promise<{ response: number }>
  notifyReady: (onClick: () => void) => void
  publish: (state: UpdateMenuState) => void
  install: () => Promise<void>
}

/** Owns update scheduling and prompts without opening the agent workspace. */
export class AppUpdater {
  private phase: Phase = 'idle'
  private manual = false
  private configured = false
  private started = false
  private disposed = false
  private promptOpen = false
  private startup?: ReturnType<typeof setTimeout>
  private interval?: ReturnType<typeof setInterval>

  constructor(private options: Options) {}

  get menuState(): UpdateMenuState {
    const labels: Record<Phase, string> = {
      idle: 'Check for Updates…', checking: 'Checking for Updates…',
      downloading: 'Downloading Update…', ready: 'Restart to Update…', installing: 'Restarting…',
    }
    return { label: labels[this.phase], enabled: !this.disposed && (this.phase === 'idle' || this.phase === 'ready') }
  }

  private publish() { this.options.publish(this.menuState) }
  private setPhase(phase: Phase) { this.phase = phase; this.publish() }

  start() {
    if (this.started || this.disposed) return
    this.started = true
    this.publish()
    if (!this.options.supported) return
    const updater = this.options.updater
    updater.on('error', this.onError)
    updater.on('update-available', this.onAvailable)
    updater.on('update-not-available', this.onNotAvailable)
    updater.on('update-downloaded', this.onDownloaded)
    this.startup = setTimeout(() => { void this.check(false) }, UPDATE_START_DELAY_MS)
    this.startup.unref()
    this.interval = setInterval(() => { void this.check(false) }, UPDATE_INTERVAL_MS)
    this.interval.unref()
  }

  async check(manual = true) {
    if (this.disposed) return
    if (!this.options.supported) {
      if (manual) await this.message({ message: 'Updates are available in the installed macOS app.', detail: 'Install the signed Autonomous Buddy app in Applications to receive updates.' })
      return
    }
    if (this.phase === 'ready') {
      if (manual) await this.offerRestart()
      return
    }
    // Squirrel downloads automatically; overlapping checks start duplicate downloads.
    if (this.phase !== 'idle') { this.manual ||= manual; return }
    this.manual = manual
    this.setPhase('checking')
    try {
      if (!this.configured) {
        // Native initialization may emit error synchronously instead of throwing.
        this.configured = true
        this.options.updater.setFeedURL({ url: updateFeedURL(this.options.arch), serverType: 'json' })
        if (!this.configured) return
      }
      this.options.updater.checkForUpdates()
    } catch { this.onError() }
  }

  private onAvailable = () => { if (!this.disposed) this.setPhase('downloading') }
  private onNotAvailable = () => {
    if (this.disposed) return
    const manual = this.manual
    this.manual = false
    this.setPhase('idle')
    if (manual) void this.message({ message: 'Autonomous Buddy is up to date.', detail: `You are running version ${this.options.version}.` })
  }
  private onDownloaded = () => {
    if (this.disposed) return
    const manual = this.manual
    this.manual = false
    this.setPhase('ready')
    if (manual) void this.offerRestart()
    else this.options.notifyReady(() => { if (!this.disposed) void this.offerRestart() })
  }
  private onError = () => {
    if (this.disposed) return
    const manual = this.manual
    this.manual = false
    this.configured = false
    this.setPhase('idle')
    if (manual) void this.message({
      type: 'error', message: 'Autonomous Buddy could not update.',
      detail: 'Check your internet connection and try again. If this continues, install the latest signed app in Applications. Your data is preserved.',
    })
  }

  private async message(options: MessageBoxOptions) {
    if (this.disposed || this.promptOpen) return
    this.promptOpen = true
    try { return await this.options.showMessage({ type: 'info', title: 'Autonomous Buddy Update', buttons: ['OK'], ...options }) }
    catch { /* A dialog can close while the application is terminating. */ }
    finally { this.promptOpen = false }
  }

  private async offerRestart() {
    if (this.phase !== 'ready') return
    const answer = await this.message({
      message: 'An Autonomous Buddy update is ready.',
      detail: 'Restart to install it. Active agent sessions and device control will stop during the restart. If you choose Later, the update installs when you next quit Buddy.',
      buttons: ['Restart to Update', 'Later'], defaultId: 1, cancelId: 1,
    })
    if (answer?.response !== 0 || this.disposed || this.phase !== 'ready') return
    this.setPhase('installing')
    try { await this.options.install() }
    catch {
      if (!this.disposed) {
        this.setPhase('ready')
        await this.message({ type: 'error', message: 'Could not restart Buddy.', detail: 'Quit and reopen Buddy to finish installing the downloaded update.' })
      }
    }
  }

  dispose() {
    this.disposed = true
    clearTimeout(this.startup)
    clearInterval(this.interval)
    const updater = this.options.updater
    // Squirrel may report a late download/install error during asynchronous shutdown.
    // Keep the guarded error listener until this process exits.
    updater.removeListener('update-available', this.onAvailable)
    updater.removeListener('update-not-available', this.onNotAvailable)
    updater.removeListener('update-downloaded', this.onDownloaded)
  }
}
