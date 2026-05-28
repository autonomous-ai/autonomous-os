import AppKit
import ApplicationServices

enum AccessibilityCheck {
    static func isTrusted() -> Bool {
        return AXIsProcessTrusted()
    }

    /// Triggers the system prompt + opens System Settings the first time.
    @discardableResult
    static func requestPrompt() -> Bool {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options: NSDictionary = [key: true]
        return AXIsProcessTrustedWithOptions(options)
    }
}
