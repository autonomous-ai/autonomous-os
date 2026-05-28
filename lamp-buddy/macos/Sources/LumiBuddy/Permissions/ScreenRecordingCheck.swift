import CoreGraphics
import Foundation

enum ScreenRecordingCheck {
    static func isTrusted() -> Bool {
        return CGPreflightScreenCaptureAccess()
    }

    /// Triggers the system prompt the first time it's called.
    /// After the user has denied once, only manual grant in System Settings → Privacy & Security → Screen Recording works.
    @discardableResult
    static func requestPrompt() -> Bool {
        return CGRequestScreenCaptureAccess()
    }
}
