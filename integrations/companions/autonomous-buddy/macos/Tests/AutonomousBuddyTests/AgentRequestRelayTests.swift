import XCTest
@testable import AutonomousBuddy

final class AgentRequestRelayTests: XCTestCase {
    private func object(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    @MainActor
    func testCorrelatesResponsesAndRejectsMismatchedOrDuplicateReplies() async throws {
        let relay = AgentRequestRelay()
        var relayID = ""
        relay.publish = { id, command in
            relayID = id
            XCTAssertEqual(command["action"] as? String, "agent.prompt")
            XCTAssertFalse(relay.respond(id: id, response: ["id": "wrong", "ok": true]))
            XCTAssertTrue(relay.respond(id: id, response: ["id": "device-1", "ok": true, "result": ["session_id": "session-1"]]))
        }
        let result = try object(await relay.request(["id": "device-1", "action": "agent.prompt"]))
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual(result["id"] as? String, "device-1")
        XCTAssertFalse(relay.respond(id: relayID, response: ["id": "device-1", "ok": true]))
    }

    @MainActor
    func testParallelRequestsCanCompleteInReverseOrder() async throws {
        let relay = AgentRequestRelay()
        var ids: [String: String] = [:]
        relay.publish = { id, command in ids[command["id"] as! String] = id }
        let first = Task { await relay.request(["id": "first"]) }
        let second = Task { await relay.request(["id": "second"]) }
        while ids.count < 2 { await Task.yield() }
        XCTAssertTrue(relay.respond(id: ids["second"]!, response: ["id": "second", "ok": true]))
        XCTAssertTrue(relay.respond(id: ids["first"]!, response: ["id": "first", "ok": false, "error": "first failed"]))
        let secondResult = try object(await second.value)
        let firstResult = try object(await first.value)
        XCTAssertEqual(secondResult["id"] as? String, "second")
        XCTAssertEqual(secondResult["ok"] as? Bool, true)
        XCTAssertEqual(firstResult["id"] as? String, "first")
        XCTAssertEqual(firstResult["error"] as? String, "first failed")
    }

    @MainActor
    func testUnavailableTimeoutAndCancellationNeverReplay() async throws {
        let relay = AgentRequestRelay()
        var result = try object(await relay.request(["id": "absent"]))
        XCTAssertEqual(result["error"] as? String, "Agent manager is unavailable")
        var published: [String] = []
        relay.publish = { id, _ in published.append(id) }
        result = try object(await relay.request(["id": "timeout", "timeout_ms": 1]))
        XCTAssertEqual(result["error"] as? String, "Agent request timed out")
        let task = Task { await relay.request(["id": "cancel"]) }
        while published.count < 2 { await Task.yield() }
        task.cancel()
        result = try object(await task.value)
        XCTAssertEqual(result["error"] as? String, "Agent request cancelled")
        XCTAssertFalse(relay.respond(id: published[1], response: ["id": "cancel", "ok": true]))
        XCTAssertEqual(published.count, 2)
    }

    @MainActor
    func testDeviceRoutesAgentCommandsBeforeComputerDispatcherAndHonorsPause() async throws {
        let state = AppState.shared
        let paused = state.paused
        defer { state.setPaused(paused) }
        state.setPaused(false)
        let dispatcher = CommandDispatcher(auditLog: AuditLog(url: URL(fileURLWithPath: "/dev/null")))
        let connection = DeviceConnection(host: "127.0.0.1:1", token: "fake", dispatcher: dispatcher)
        let command = try JSONSerialization.data(withJSONObject: ["id": "voice-1", "action": "agent.prompt", "params": ["session_id": "s1"]])
        var response = try object(await connection.routeCommand(command))
        XCTAssertEqual(response["error"] as? String, "Agent manager is unavailable")
        var calls = 0
        connection.agentHandler = { request in
            calls += 1
            XCTAssertEqual((request["params"] as? [String: Any])?["session_id"] as? String, "s1")
            return try! JSONSerialization.data(withJSONObject: ["id": "voice-1", "ok": true])
        }
        response = try object(await connection.routeCommand(command))
        XCTAssertEqual(response["ok"] as? Bool, true)
        XCTAssertEqual(calls, 1)
        state.setPaused(true)
        response = try object(await connection.routeCommand(command))
        XCTAssertEqual(response["error"] as? String, "Buddy is paused")
        XCTAssertEqual(calls, 1)
        XCTAssertFalse(connection.sendAgentEvent(["type": "agent_event", "session_id": "s1"]))
    }

    func testMockDeviceURLAllowsOnlyExplicitLoopbackWebSocket() {
        XCTAssertEqual(AppDelegate.testDeviceHost("ws://127.0.0.1:1234/api/buddy/ws"), "127.0.0.1:1234")
        for value in ["ws://lamp.local:1234", "ws://localhost:1234", "wss://127.0.0.1:1234", "ws://127.0.0.1:1234/other", "ws://user@127.0.0.1:1234", "ws://127.0.0.1:1234?x=1", "ws://127.0.0.1:0"] {
            XCTAssertNil(AppDelegate.testDeviceHost(value), value)
        }
    }
}
