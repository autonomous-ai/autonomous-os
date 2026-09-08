import XCTest
@testable import AutonomousBuddy

final class KeyboardFocusTests: XCTestCase {
    func testPinnedProcessRejectsFocusChangesAndMissingForeground() throws {
        let guardValue = KeyboardFocusGuard(target: "Notes", processID: 123)
        XCTAssertNoThrow(try guardValue.validate(frontmostPID: 123))
        XCTAssertThrowsError(try guardValue.validate(frontmostPID: 456))
        XCTAssertThrowsError(try guardValue.validate(frontmostPID: nil))
        // Returning to the same app name with a different PID is still stale.
        XCTAssertThrowsError(try guardValue.validate(frontmostPID: 124))
        XCTAssertNoThrow(try KeyboardFocusGuard(target: nil, processID: nil).validate(frontmostPID: nil))
    }

    func testTargetParsingKeepsUnscopedCompatibilityButRejectsBadValues() throws {
        XCTAssertNil(try KeyboardFocusGuard.parseTarget(nil))
        XCTAssertEqual(try KeyboardFocusGuard.parseTarget(" Notes "), "Notes")
        for value in [true, 123, "", "   ", String(repeating: "a", count: 1025)] as [Any] {
            XCTAssertThrowsError(try KeyboardFocusGuard.parseTarget(value))
        }
    }

    func testWrongAppRejectsBeforePermissionPromptOrInput() async {
        let impossibleTarget = "AutonomousBuddyMissingTestApp-\(UUID().uuidString)"
        let commands: [(any Executor, [String: Any])] = [
            (TypeTextExecutor(), ["text": "must not type", "app": impossibleTarget]),
            (KeyComboExecutor(), ["keys": ["cmd", "n"], "app": impossibleTarget])
        ]
        for (executor, params) in commands {
            do {
                _ = try await executor.execute(params: params)
                XCTFail("Wrong app accepted")
            } catch ExecutorError.actionFailed(let message) {
                XCTAssertTrue(message.contains("not frontmost"))
            } catch {
                XCTFail("Focus validation must run before permission lookup: \(error)")
            }
        }
    }
}
