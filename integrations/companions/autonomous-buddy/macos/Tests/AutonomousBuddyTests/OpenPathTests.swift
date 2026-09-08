import XCTest
@testable import AutonomousBuddy

final class OpenPathTests: XCTestCase {
    func testHomeExpansionUsesMacHomeAndPreservesSpaces() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let downloads = home.appendingPathComponent("Downloads")
        try FileManager.default.createDirectory(at: downloads, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let file = downloads.appendingPathComponent("note with spaces.txt")
        try Data("sample".utf8).write(to: file)
        XCTAssertEqual(try OpenPathExecutor.resolvePath("~/Downloads", home: home).path, downloads.path)
        XCTAssertEqual(try OpenPathExecutor.resolvePath("~/Downloads/note with spaces.txt", home: home).path, file.path)
        XCTAssertEqual(try OpenPathExecutor.resolvePath("~", home: home).path, home.path)
        XCTAssertEqual(try OpenPathExecutor.resolvePath(file.path, home: home).path, file.path)
        XCTAssertThrowsError(try OpenPathExecutor.resolvePath("~/missing", home: home))
    }

    func testRejectsRelativePathsAccountExpansionAndInvalidTypes() {
        for value in ["Downloads", "./Downloads", "~someone/Downloads", "file:///tmp", "", true, 123, "/tmp/\0"] as [Any] {
            XCTAssertThrowsError(try OpenPathExecutor.resolvePath(value, home: URL(fileURLWithPath: "/tmp")))
        }
        XCTAssertThrowsError(try OpenPathExecutor.mode("delete"))
        XCTAssertThrowsError(try OpenPathExecutor.mode(true))
        XCTAssertEqual(try OpenPathExecutor.mode(nil), "open")
        XCTAssertEqual(try OpenPathExecutor.mode("reveal"), "reveal")
    }

    func testCancellationBeforePathLookupOrLaunch() async {
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await OpenPathExecutor().execute(params: ["path": "~/Downloads"])
        }
        do {
            _ = try await task.value
            XCTFail("Cancelled path open executed")
        } catch is CancellationError {
            // No path lookup, UI launch, or permission prompt occurred.
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }
}
