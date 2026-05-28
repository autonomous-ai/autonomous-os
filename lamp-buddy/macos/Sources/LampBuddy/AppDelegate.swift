import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var menuBarController: MenuBarController?
    private var discovery: LampDiscovery?
    private var pairingManager: PairingManager?
    private var dispatcher: CommandDispatcher?
    private var connection: LampConnection?
    private var auditLog: AuditLog?
    private var pairingWindow: PairingWindowController?
    private var activityWindow: ActivityWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let store = PairingStore()
        let audit = AuditLog()
        let dispatcher = CommandDispatcher(auditLog: audit)
        let pairingManager = PairingManager(store: store)

        self.auditLog = audit
        self.dispatcher = dispatcher
        self.pairingManager = pairingManager

        // Bonjour discovery — best-effort. If lamp doesn't advertise `_lamp._tcp`,
        // user pairs by typing `lamp-xxxx.local` manually.
        let discovery = LampDiscovery()
        discovery.onLampsChanged = { lamps in
            AppState.shared.setDiscoveredLamps(lamps)
        }
        discovery.start()
        self.discovery = discovery

        menuBarController = MenuBarController(
            onPair: { [weak self] host in self?.showPairing(host: host) },
            onUnpair: { [weak self] in self?.unpair() },
            onTogglePause: { paused in AppState.shared.setPaused(paused) },
            onShowActivity: { [weak self] in self?.showActivity() },
            onAbout: { [weak self] in self?.showAbout() },
            onQuit: { NSApp.terminate(nil) }
        )

        // Auto-reconnect if a record already exists from a previous run.
        if let record = pairingManager.current() {
            AppState.shared.setPairing(.paired(buddyID: record.buddyID, lampHost: record.lampHost))
            startConnection(record: record)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        connection?.disconnect()
        discovery?.stop()
    }

    // MARK: - actions

    private func showPairing(host: String?) {
        guard let pairingManager else { return }
        let controller = PairingWindowController(manager: pairingManager, initialHost: host)
        controller.onSuccess = { [weak self] record in
            AppState.shared.setPairing(.paired(buddyID: record.buddyID, lampHost: record.lampHost))
            self?.startConnection(record: record)
        }
        pairingWindow = controller
        NSApp.activate(ignoringOtherApps: true)
        controller.window?.center()
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
    }

    private func unpair() {
        // Tell the lamp first so it drops its pairing record before we forget
        // our token. Fire-and-forget with a 5s timeout inside notifyRevokeSelf;
        // local state always clears on completion regardless of lamp reachability.
        let snapshot = pairingManager?.current()
        let manager = pairingManager
        Task {
            if let record = snapshot, let manager {
                await manager.notifyRevokeSelf(host: record.lampHost, token: record.token)
            }
            await MainActor.run { [weak self] in
                self?.connection?.disconnect()
                self?.connection = nil
                try? self?.pairingManager?.unpair()
                AppState.shared.setPairing(.notPaired)
            }
        }
    }

    private func startConnection(record: PairingRecord) {
        connection?.disconnect()
        guard let dispatcher else { return }
        let c = LampConnection(host: record.lampHost, token: record.token, dispatcher: dispatcher)
        c.connect()
        connection = c
    }

    private func showActivity() {
        if activityWindow == nil {
            activityWindow = ActivityWindowController()
        }
        activityWindow?.show()
    }

    private func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Lamp Buddy"
        alert.informativeText = """
            Native macOS companion that lets your Lamp control this Mac \
            via voice commands processed by OpenClaw.

            MVP build: pairing, persistent WebSocket, command execution. \
            Lamp-side Go endpoints are required for end-to-end use; see \
            lamp-buddy/docs/lamp-buddy-mvp.md.
            """
        alert.alertStyle = .informational
        alert.runModal()
    }
}
