import Foundation

/// Correlates device requests over the parent-owned pipe; never replays work.
@MainActor
final class AgentRequestRelay {
    nonisolated init() {}

    typealias Publish = (String, [String: Any]) -> Void
    private struct Pending {
        let commandID: String
        let continuation: CheckedContinuation<Data, Never>
        let timeout: Task<Void, Never>
    }
    private var pending: [String: Pending] = [:]
    var publish: Publish?

    static func failure(id: String, message: String) -> Data {
        (try? JSONSerialization.data(withJSONObject: ["id": id, "ok": false, "error": message])) ?? Data()
    }

    func request(_ command: [String: Any]) async -> Data {
        let commandID = command["id"] as? String ?? ""
        guard !Task.isCancelled else { return Self.failure(id: commandID, message: "Agent request cancelled") }
        guard let publish else { return Self.failure(id: commandID, message: "Agent manager is unavailable") }
        let relayID = UUID().uuidString
        let milliseconds = min(10_000, max(1, command["timeout_ms"] as? Int ?? 10_000))
        return await withTaskCancellationHandler(operation: {
            await withCheckedContinuation { continuation in
                let timeout = Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: UInt64(milliseconds) * 1_000_000)
                    guard !Task.isCancelled else { return }
                    self?.fail(relayID, message: "Agent request timed out")
                }
                pending[relayID] = Pending(commandID: commandID, continuation: continuation, timeout: timeout)
                publish(relayID, command)
            }
        }, onCancel: {
            Task { @MainActor [weak self] in self?.fail(relayID, message: "Agent request cancelled") }
        })
    }

    @discardableResult
    func respond(id: String, response: [String: Any]) -> Bool {
        guard let item = pending[id], response["id"] as? String == item.commandID,
              response["ok"] is Bool,
              let data = try? JSONSerialization.data(withJSONObject: response) else { return false }
        pending.removeValue(forKey: id)
        item.timeout.cancel()
        item.continuation.resume(returning: data)
        return true
    }

    private func fail(_ id: String, message: String) {
        guard let item = pending.removeValue(forKey: id) else { return }
        item.timeout.cancel()
        item.continuation.resume(returning: Self.failure(id: item.commandID, message: message))
    }
}
