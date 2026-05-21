import Cocoa

final class MenuBarController: NSObject {
    private let statusItem: NSStatusItem
    private let menu = NSMenu()

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()

        if let button = statusItem.button {
            button.title = "💡"
            button.toolTip = "Lumi Buddy"
        }

        buildMenu()
        statusItem.menu = menu
    }

    private func buildMenu() {
        let statusItemEntry = NSMenuItem(title: "Lumi Buddy — Not paired", action: nil, keyEquivalent: "")
        statusItemEntry.isEnabled = false
        menu.addItem(statusItemEntry)

        menu.addItem(NSMenuItem.separator())

        let pairItem = NSMenuItem(title: "Pair with Lumi…", action: #selector(pairAction(_:)), keyEquivalent: "p")
        pairItem.target = self
        menu.addItem(pairItem)

        menu.addItem(NSMenuItem.separator())

        let aboutItem = NSMenuItem(title: "About Lumi Buddy", action: #selector(aboutAction(_:)), keyEquivalent: "")
        aboutItem.target = self
        menu.addItem(aboutItem)

        let quitItem = NSMenuItem(title: "Quit Lumi Buddy", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quitItem)
    }

    @objc private func aboutAction(_ sender: Any?) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Lumi Buddy"
        alert.informativeText = """
            MVP scaffold (Phase 1A).

            Pair this Mac with your Lumi lamp to allow voice-driven \
            computer control. Networking, pairing, and command execution \
            land in subsequent phases.

            See docs/lumi-buddy-mvp.md for the full plan.
            """
        alert.alertStyle = .informational
        alert.runModal()
    }

    @objc private func pairAction(_ sender: Any?) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Pairing not yet implemented"
        alert.informativeText = "This scaffold is Phase 1A. Pairing flow lands in Phase 1C."
        alert.alertStyle = .informational
        alert.runModal()
    }
}
