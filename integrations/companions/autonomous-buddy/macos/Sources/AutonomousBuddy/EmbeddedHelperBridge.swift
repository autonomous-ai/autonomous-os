import Cocoa

/// Private inherited stdio pipes connect the bundled helper to Electron. No socket
/// or listening port is exposed; the parent owns the lifetime of this process.
final class EmbeddedHelperBridge {
    typealias Handler = @MainActor (String, String, [String: Any]) async -> Void
    private let handler: Handler
    private var observer: NSObjectProtocol?

    init(handler: @escaping Handler) {
        self.handler = handler
    }

    deinit {
        if let observer { NotificationCenter.default.removeObserver(observer) }
    }

    func start() {
        observer = NotificationCenter.default.addObserver(
            forName: .autonomousBuddyAppStateChanged, object: nil, queue: .main
        ) { [weak self] _ in self?.publishState() }
        publishState()
        // Blocking stdin reads belong off the AppKit runloop. EOF is a parent
        // shutdown/crash and must not leave an invisible connected helper alive.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while let line = readLine() {
                guard let data = line.data(using: .utf8),
                      let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let id = request["id"] as? String, !id.isEmpty,
                      let method = request["method"] as? String,
                      request["params"] == nil || request["params"] is [String: Any] else { continue }
                let params = request["params"] as? [String: Any] ?? [:]
                Task { @MainActor [weak self] in
                    await self?.handler(id, method, params)
                }
            }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }

    func respond(id: String, result: Any) { write(["id": id, "result": result]) }
    func fail(id: String, error: String) { write(["id": id, "error": error]) }
    func publishMenuAction(_ action: String) { write(["event": "menu", "action": action]) }
    func publishAgentRequest(id: String, command: [String: Any]) {
        write(["event": "agent_request", "id": id, "command": command])
    }
    func publishState() { write(["event": "state", "state": Self.snapshot()]) }

    // Every write is called on the main queue, preserving JSONL frame boundaries.
    private func write(_ object: [String: Any]) {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return }
        data.append(0x0A)
        try? FileHandle.standardOutput.write(contentsOf: data)
    }

    static func snapshot() -> [String: Any] {
        let state = AppState.shared
        var result: [String: Any] = [
            "paired": false,
            "paused": state.paused,
            "accessibility": AccessibilityCheck.isTrusted(),
            "screenRecording": ScreenRecordingCheck.isTrusted(),
            "devices": state.discoveredDevices.map { ["name": $0.name, "host": $0.host] },
            "recentCommands": state.recentCommands.map { record -> [String: Any] in
                var item: [String: Any] = [
                    "id": record.id, "action": record.action, "summary": record.summary,
                    "ok": record.ok, "timestamp": ISO8601DateFormatter().string(from: record.timestamp)
                ]
                if let error = record.error { item["error"] = error }
                return item
            }
        ]
        if case .paired(_, let host) = state.pairing {
            result["paired"] = true
            result["deviceHost"] = host
        }
        switch state.connection {
        case .disconnected: result["connection"] = "disconnected"
        case .connecting: result["connection"] = "connecting"
        case .connected: result["connection"] = "connected"
        case .error(let error):
            result["connection"] = "error"
            result["connectionError"] = error
        }
        return result
    }
}
