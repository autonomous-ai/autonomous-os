import XCTest
@testable import AutonomousBuddy

final class EmbeddedHelperBridgeTests: XCTestCase {
    @MainActor
    func testStateSnapshotPublishesPairingAndConnectionWithoutCredentials() throws {
        let state = AppState.shared
        let originalPairing = state.pairing
        let originalConnection = state.connection
        let originalPaused = state.paused
        defer {
            state.setPairing(originalPairing)
            state.setConnection(originalConnection)
            state.setPaused(originalPaused)
        }
        state.setPairing(.paired(buddyID: "private-buddy-id", deviceHost: "lamp-test.local"))
        state.setConnection(.error("connection lost"))
        state.setPaused(true)

        let snapshot = EmbeddedHelperBridge.snapshot()
        XCTAssertEqual(snapshot["paired"] as? Bool, true)
        XCTAssertEqual(snapshot["deviceHost"] as? String, "lamp-test.local")
        XCTAssertEqual(snapshot["connection"] as? String, "error")
        XCTAssertEqual(snapshot["connectionError"] as? String, "connection lost")
        XCTAssertEqual(snapshot["paused"] as? Bool, true)
        XCTAssertNil(snapshot["buddyID"])
        XCTAssertNil(snapshot["token"])
        XCTAssertTrue(JSONSerialization.isValidJSONObject(snapshot))

        state.setPairing(.notPaired)
        state.setConnection(.disconnected)
        let cleared = EmbeddedHelperBridge.snapshot()
        XCTAssertEqual(cleared["paired"] as? Bool, false)
        XCTAssertNil(cleared["deviceHost"])
        XCTAssertNil(cleared["connectionError"])
    }
}
