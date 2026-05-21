import Foundation

extension Notification.Name {
    // Posted on the main queue whenever any AppState field changes. Observers
    // (e.g. the Activity window) can refresh themselves instead of polling.
    // MenuBarController still uses the direct onChange closure to keep the
    // pairing/connection UI snappy.
    static let lumiBuddyAppStateChanged = Notification.Name("lumiBuddyAppStateChanged")
}

enum PairingStatus: Equatable {
    case notPaired
    case paired(buddyID: String, lampHost: String)
}

enum ConnectionStatus: Equatable {
    case disconnected
    case connecting
    case connected
    case error(String)
}

struct CommandRecord {
    let id: String
    let action: String
    let ok: Bool
    let error: String?
    let timestamp: Date
}

final class AppState {
    static let shared = AppState()

    // Cap on the in-memory ring buffer. The full audit trail lives on disk
    // (see AuditLog.swift) — this list is what the menu bar and the Activity
    // window render. 100 entries is enough for a useful "tail -n 100" view
    // without holding meaningful memory.
    static let recentCommandsCap = 100

    private(set) var pairing: PairingStatus = .notPaired { didSet { notify() } }
    private(set) var connection: ConnectionStatus = .disconnected { didSet { notify() } }
    private(set) var discoveredLamps: [LampInfo] = [] { didSet { notify() } }
    private(set) var paused: Bool = false { didSet { notify() } }
    private(set) var recentCommands: [CommandRecord] = [] { didSet { notify() } }

    var lastCommand: CommandRecord? { recentCommands.first }

    var onChange: (() -> Void)?

    private init() {}

    func setPairing(_ status: PairingStatus) { onMain { self.pairing = status } }
    func setConnection(_ status: ConnectionStatus) { onMain { self.connection = status } }
    func setDiscoveredLamps(_ lamps: [LampInfo]) { onMain { self.discoveredLamps = lamps } }
    func setPaused(_ paused: Bool) { onMain { self.paused = paused } }
    func recordCommand(_ record: CommandRecord) {
        onMain {
            var list = self.recentCommands
            list.insert(record, at: 0)
            if list.count > Self.recentCommandsCap {
                list.removeLast(list.count - Self.recentCommandsCap)
            }
            self.recentCommands = list
        }
    }

    private func notify() {
        // didSet runs on whichever thread the setter ran. setPairing etc. always hop to main first,
        // so onChange always fires on main.
        onChange?()
        NotificationCenter.default.post(name: .lumiBuddyAppStateChanged, object: nil)
    }

    private func onMain(_ block: @escaping () -> Void) {
        if Thread.isMainThread { block() }
        else { DispatchQueue.main.async(execute: block) }
    }
}
