import XCTest
@testable import AutonomousBuddy

final class AppOpeningTests: XCTestCase {
    func testExplicitBrowserNeverSilentlySelectsDefault() throws {
        XCTAssertEqual(try OpenURLExecutor.browserSelection(" Chrome ").bundleID, "com.google.Chrome")
        XCTAssertEqual(try OpenURLExecutor.browserSelection("Microsoft Edge").bundleID, "com.microsoft.edgemac")
        XCTAssertNil(try OpenURLExecutor.browserSelection(nil).bundleID)
        XCTAssertNil(try OpenURLExecutor.browserSelection("default").bundleID)
        for value in ["", "chromee", "unknown.app", true, 1] as [Any] {
            XCTAssertThrowsError(try OpenURLExecutor.browserSelection(value))
        }
    }

    func testDefaultHandlerRejectionCannotReportOpened() throws {
        XCTAssertThrowsError(try OpenURLExecutor.defaultOpenResult(accepted: false))
        let accepted = try OpenURLExecutor.defaultOpenResult(accepted: true)
        XCTAssertEqual(accepted["opened"] as? Bool, true)
        XCTAssertNil(accepted["frontmost"])
    }

    func testCancelledOpenNeverLaunchesApplications() async {
        let commands: [(any Executor, [String: Any])] = [
            (OpenAppExecutor(), ["app": "Google Chrome"]),
            (OpenURLExecutor(), ["url": "https://example.com", "browser": "chrome"])
        ]
        for (executor, params) in commands {
            let task = Task {
                withUnsafeCurrentTask { $0?.cancel() }
                return try await executor.execute(params: params)
            }
            do {
                _ = try await task.value
                XCTFail("Cancelled open should not execute")
            } catch is CancellationError {
                // Expected before any launch or focus change.
            } catch {
                XCTFail("Unexpected error: \(error)")
            }
        }
    }
}
