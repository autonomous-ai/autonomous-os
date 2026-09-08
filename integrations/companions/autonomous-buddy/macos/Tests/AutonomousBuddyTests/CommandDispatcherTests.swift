import XCTest
@testable import AutonomousBuddy

private actor BlockingExecutor: Executor {
    nonisolated let action = "test_block"
    private var started = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func execute(params: [String: Any]) async throws -> [String: Any] {
        started = true
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
        try await Task.sleep(nanoseconds: 60_000_000_000)
        return ["unexpected": true]
    }

    func waitUntilStarted() async {
        if started { return }
        await withCheckedContinuation { waiters.append($0) }
    }
}

final class CommandDispatcherTests: XCTestCase {
    private func command(_ id: String, _ action: String, params: [String: Any] = [:], timeout: Int = 5000) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["id": id, "action": action, "params": params, "timeout_ms": timeout])
    }

    private func response(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func dispatcher(_ executor: Executor) -> CommandDispatcher {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("buddy-test-\(UUID()).log")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return CommandDispatcher(auditLog: AuditLog(url: url), additionalExecutors: [executor])
    }

    func testConcurrentInputRejectedAndCancellationTargetsOnlyActiveID() async throws {
        let executor = BlockingExecutor()
        let dispatcher = dispatcher(executor)
        let request = try command("active", executor.action)
        let running = Task { await dispatcher.dispatch(request) }
        await executor.waitUntilStarted()
        let busyData = await dispatcher.dispatch(try command("second", executor.action))
        let busy = try response(busyData)
        XCTAssertEqual(busy["ok"] as? Bool, false)
        XCTAssertTrue((busy["error"] as? String ?? "").contains("busy"))
        let wrongData = await dispatcher.dispatch(try command("cancel-other", "cancel_command", params: ["id": "other"]))
        let wrong = try response(wrongData)
        XCTAssertEqual((wrong["result"] as? [String: Any])?["cancel_requested"] as? Bool, false)
        let cancelledData = await dispatcher.dispatch(try command("cancel-active", "cancel_command", params: ["id": "active"]))
        let cancelled = try response(cancelledData)
        XCTAssertEqual((cancelled["result"] as? [String: Any])?["cancel_requested"] as? Bool, true)
        let resultData = await running.value
        XCTAssertEqual(try response(resultData)["error"] as? String, "command cancelled")
    }

    func testCallerCancellationStopsExecutionAndAllowsNextCommand() async throws {
        let executor = BlockingExecutor()
        let dispatcher = dispatcher(executor)
        let request = try command("active", executor.action)
        let running = Task { await dispatcher.dispatch(request) }
        await executor.waitUntilStarted()
        running.cancel()
        let data = await running.value
        XCTAssertEqual(try response(data)["error"] as? String, "command cancelled")
        let next = await dispatcher.dispatch(try command("next", "ping"))
        XCTAssertEqual(try response(next)["ok"] as? Bool, true)
    }

    func testTimeoutCancelsCooperativeExecutor() async throws {
        let executor = BlockingExecutor()
        let dispatcher = dispatcher(executor)
        let start = Date()
        let data = await dispatcher.dispatch(try command("timeout", executor.action, timeout: 500))
        XCTAssertEqual(try response(data)["error"] as? String, "timeout after 500ms")
        XCTAssertLessThan(Date().timeIntervalSince(start), 3)
    }

    func testMalformedTimeoutAndParamsRejected() throws {
        for value in [true, -1, 1, 60001, 1e300, 500.5, "500"] as [Any] {
            let data = try JSONSerialization.data(withJSONObject: ["id": "id", "action": "ping", "timeout_ms": value])
            XCTAssertThrowsError(try IncomingCommand.decode(from: data))
        }
        for params in ["bad", [1, 2], NSNull()] as [Any] {
            let data = try JSONSerialization.data(withJSONObject: ["id": "id", "action": "ping", "params": params])
            XCTAssertThrowsError(try IncomingCommand.decode(from: data))
        }
        let decoded = try IncomingCommand.decode(from: command("id", "ping", timeout: 0))
        XCTAssertNil(decoded.timeoutMs)
    }
}
