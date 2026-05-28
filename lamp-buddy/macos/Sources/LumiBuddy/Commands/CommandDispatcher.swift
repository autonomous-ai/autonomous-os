import Foundation

final class CommandDispatcher {
    private var executors: [String: Executor] = [:]
    private let auditLog: AuditLog

    init(auditLog: AuditLog) {
        self.auditLog = auditLog
        register(OpenAppExecutor())
        register(CloseAppExecutor())
        register(OpenURLExecutor())
        register(TypeTextExecutor())
        register(KeyComboExecutor())
        register(NotificationExecutor())
        register(PingExecutor())
        register(ScreenshotExecutor())
        register(ClickAtExecutor())
        register(ScrollExecutor())
        register(MouseMoveExecutor())
        register(DragExecutor())
        register(ReadClipboardExecutor())
        register(WriteClipboardExecutor())
        register(ClickButtonExecutor())
        register(CursorPosExecutor())
        register(ListDisplaysExecutor())
    }

    private func register(_ executor: Executor) {
        executors[executor.action] = executor
    }

    func dispatch(_ data: Data) async -> Data {
        let start = Date()
        let cmd: IncomingCommand
        do {
            cmd = try IncomingCommand.decode(from: data)
        } catch {
            let resp = CommandResponse(
                id: "unknown",
                ok: false,
                result: nil,
                error: (error as? LocalizedError)?.errorDescription ?? error.localizedDescription,
                durationMs: 0
            )
            return (try? resp.encode()) ?? Data()
        }

        let summary = CommandSummary.describe(action: cmd.action, params: cmd.params)

        let paused = await readPaused()
        if paused {
            let resp = CommandResponse(id: cmd.id, ok: false, result: nil, error: "buddy paused by user", durationMs: 0)
            await auditLog.append(action: cmd.action, summary: summary, ok: false, error: resp.error)
            return (try? resp.encode()) ?? Data()
        }

        guard let executor = executors[cmd.action] else {
            let resp = CommandResponse(id: cmd.id, ok: false, result: nil, error: "unknown action: \(cmd.action)", durationMs: 0)
            await auditLog.append(action: cmd.action, summary: summary, ok: false, error: resp.error)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: false, error: resp.error)
            return (try? resp.encode()) ?? Data()
        }

        do {
            let result = try await runWithTimeout(executor: executor, params: cmd.params, timeoutMs: cmd.timeoutMs)
            let duration = Int(Date().timeIntervalSince(start) * 1000)
            let resp = CommandResponse(id: cmd.id, ok: true, result: result, error: nil, durationMs: duration)
            await auditLog.append(action: cmd.action, summary: summary, ok: true, error: nil)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: true)
            return (try? resp.encode()) ?? Data()
        } catch {
            let duration = Int(Date().timeIntervalSince(start) * 1000)
            let msg = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            let resp = CommandResponse(id: cmd.id, ok: false, result: nil, error: msg, durationMs: duration)
            await auditLog.append(action: cmd.action, summary: summary, ok: false, error: msg)
            await recordOnMain(id: cmd.id, action: cmd.action, summary: summary, ok: false, error: msg)
            return (try? resp.encode()) ?? Data()
        }
    }

    private func runWithTimeout(executor: Executor, params: [String: Any], timeoutMs: Int?) async throws -> [String: Any] {
        let timeout = max(500, timeoutMs ?? 5000)
        return try await withThrowingTaskGroup(of: [String: Any].self) { group in
            group.addTask {
                try await executor.execute(params: params)
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(timeout) * 1_000_000)
                throw ExecutorError.actionFailed("timeout after \(timeout)ms")
            }
            let value = try await group.next()!
            group.cancelAll()
            return value
        }
    }

    @MainActor
    private func readPaused() -> Bool {
        return AppState.shared.paused
    }

    @MainActor
    private func recordOnMain(id: String, action: String, summary: String, ok: Bool, error: String? = nil) {
        AppState.shared.recordCommand(CommandRecord(id: id, action: action, summary: summary, ok: ok, error: error, timestamp: Date()))
    }
}
