import XCTest
@testable import AutonomousBuddy

final class ComputerInputRobustnessTests: XCTestCase {
    func testUntrustedNumbersCannotTrapIntegerConversions() throws {
        for invalid: Any in [Double.nan, Double.infinity, -Double.infinity, Double.greatestFiniteMagnitude,
                             true, "12", 1.5, -1, UInt64.max] {
            XCTAssertThrowsError(try ExecutorParameters.integer(["count": invalid], "count", range: 0...100))
        }
        XCTAssertEqual(try ExecutorParameters.integer(["count": 3], "count", range: 0...100), 3)
        XCTAssertEqual(try ExecutorParameters.integer([:], "count", default: 2, range: 0...100), 2)
    }

    func testComboRequiresExactlyOnePrimaryKey() throws {
        for keys in [["cmd"], ["cmd", "a", "b"], ["cmd", "unknown"], []] {
            XCTAssertThrowsError(try KeyComboExecutor.parse(keys: keys))
        }
        let (flags, code) = try KeyComboExecutor.parse(keys: ["cmd", "shift", "a"])
        XCTAssertTrue(flags.contains(.maskCommand))
        XCTAssertTrue(flags.contains(.maskShift))
        XCTAssertEqual(code, 0)
    }

    func testScreenshotSizeRejectsUnsafeAndEmptyAllocations() throws {
        for scale in [Double.nan, Double.infinity, 0, -1, 2] {
            XCTAssertThrowsError(try ScreenshotExecutor.outputSize(width: 3840, height: 2160, scale: scale))
        }
        XCTAssertThrowsError(try ScreenshotExecutor.outputSize(width: Int.max, height: 100, scale: 1))
        XCTAssertThrowsError(try ScreenshotExecutor.outputSize(width: 1, height: 1, scale: 0.01))
        XCTAssertThrowsError(try ScreenshotExecutor.outputSize(width: 10_000, height: 10_000, scale: 1))
        let (w, h) = try ScreenshotExecutor.outputSize(width: 3840, height: 2160, scale: 1.0 / 3)
        XCTAssertEqual(w, 1280)
        XCTAssertEqual(h, 720)
    }

    func testScreenshotTransformIncludesNegativeSecondaryDisplayOriginAndRounding() {
        let transform = ScreenshotExecutor.imageTransform(originX: -1920, originY: 120,
            pointWidth: 1920, pointHeight: 1080, imageWidth: 1279, imageHeight: 719)
        XCTAssertEqual(transform["origin_x"], -1920)
        XCTAssertEqual(transform["origin_y"], 120)
        XCTAssertEqual(transform["scale_x"]!, 1920.0 / 1279, accuracy: 0.000001)
        XCTAssertEqual(transform["scale_y"]!, 1080.0 / 719, accuracy: 0.000001)
        XCTAssertEqual(transform["origin_x"]! + 1279 * transform["scale_x"]!, 0, accuracy: 0.000001)
    }

    func testScreenshotRetentionKeepsUniqueLatestTwentyPaths() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ScreenshotStore(directory: directory)
        var paths = Set<URL>()
        var latest: URL?
        for index in 0..<23 {
            let path = try await store.save(data: Data("capture \(index)".utf8))
            XCTAssertTrue(paths.insert(path).inserted)
            latest = path
            // Force equal future mtimes: the newly written capture must survive even
            // when older files sort ahead of it, independently of directory URL aliases.
            let existing = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            for file in existing {
                try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: 4_000_000_000)],
                                                      ofItemAtPath: file.path)
            }
        }
        let retained = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        XCTAssertEqual(retained.count, 20)
        XCTAssertTrue(retained.map(\.standardizedFileURL).contains(try XCTUnwrap(latest).standardizedFileURL))
        XCTAssertEqual(try String(contentsOf: XCTUnwrap(latest)), "capture 22")
    }

    func testExtremeSummaryNumbersCannotTrapBeforeValidation() {
        for value in [Double.greatestFiniteMagnitude, 1e300, -1e300, Double.infinity, Double.nan] {
            XCTAssertFalse(CommandSummary.describe(action: "click_at", params: ["x": value, "y": value]).isEmpty)
            XCTAssertFalse(CommandSummary.describe(action: "screenshot", params: ["scale": value]).isEmpty)
        }
    }

    func testLegacyClickBoundsRejectBeforePermissionCheck() async {
        for invalid: Any in [-1, 0, 31, Double.infinity, 1e300, true, "12"] {
            do {
                _ = try await ClickButtonExecutor().execute(params: ["label": "never click", "max_depth": invalid])
                XCTFail("Expected invalid depth")
            } catch ExecutorError.invalidParam {
                // Validation happens before permission lookup or any Accessibility event.
            } catch {
                XCTFail("Unexpected error: \(error)")
            }
        }
    }

    func testAlreadyCanceledInputNeverChecksPermissionsOrPostsEvents() async {
        let commands: [(any Executor, [String: Any])] = [
            (TypeTextExecutor(), ["text": "do not type"]),
            (KeyComboExecutor(), ["keys": ["cmd", "a"]]),
            (ClickAtExecutor(), ["x": 1, "y": 1]),
            (ScrollExecutor(), ["delta_y": 10]),
            (MouseMoveExecutor(), ["x": 1, "y": 1]),
            (DragExecutor(), ["from": ["x": 1, "y": 1], "to": ["x": 2, "y": 2]]),
            (ScreenshotExecutor(), [:]),
            (ClickButtonExecutor(), ["label": "never click"]),
        ]
        for (executor, params) in commands {
            let task = Task {
                withUnsafeCurrentTask { $0?.cancel() }
                return try await executor.execute(params: params)
            }
            do {
                _ = try await task.value
                XCTFail("Expected cancellation for \(executor.action)")
            } catch is CancellationError {
                // Expected before any OS interaction.
            } catch {
                XCTFail("Unexpected error for \(executor.action): \(error)")
            }
        }
    }
}
