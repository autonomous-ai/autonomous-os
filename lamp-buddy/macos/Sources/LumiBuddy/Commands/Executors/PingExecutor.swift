import Foundation

struct PingExecutor: Executor {
    let action = "ping"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        return [
            "pong": true,
            "timestamp": ISO8601DateFormatter().string(from: Date()),
        ]
    }
}
