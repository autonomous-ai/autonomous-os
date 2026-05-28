import AppKit
import ApplicationServices
import Foundation

// click_button uses macOS Accessibility API to find a UI element by label/role and
// invoke its AXPress action. Works reliably for native macOS apps (Settings, Finder, Notes,
// Calculator, etc.). For Chrome / Safari, the web content's accessibility tree is exposed
// but coverage is inconsistent — some sites work, some don't. When this fails, fall back to
// the Vision phase (screenshot + click_at) from the lamp/OpenClaw skill side.

struct ClickButtonExecutor: Executor {
    let action = "click_button"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let label = (params["label"] as? String)?.trimmingCharacters(in: .whitespaces),
              !label.isEmpty else {
            throw ExecutorError.missingParam("label")
        }
        let appName = params["app"] as? String
        let role = (params["role"] as? String) ?? (kAXButtonRole as String)
        let maxDepth = (params["max_depth"] as? Int) ?? 50

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for click_button")
        }

        let workspace = NSWorkspace.shared
        let apps: [NSRunningApplication]
        if let appName {
            apps = workspace.runningApplications.filter { app in
                app.localizedName == appName
                    || app.bundleIdentifier == appName
                    || (app.bundleURL?.lastPathComponent == "\(appName).app")
            }
            if apps.isEmpty {
                throw ExecutorError.actionFailed("app not running: \(appName)")
            }
        } else if let frontmost = workspace.frontmostApplication {
            apps = [frontmost]
        } else {
            throw ExecutorError.actionFailed("no frontmost app — specify `app`")
        }

        for app in apps {
            let axApp = AXUIElementCreateApplication(app.processIdentifier)
            if let element = findElement(in: axApp, label: label, role: role, maxDepth: maxDepth) {
                let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
                if result == .success {
                    return [
                        "clicked": true,
                        "app": app.localizedName ?? app.bundleIdentifier ?? "?",
                        "label": label,
                        "role": role,
                    ]
                }
                throw ExecutorError.actionFailed("AXPress failed (status=\(result.rawValue))")
            }
        }
        throw ExecutorError.actionFailed("element not found: label=\(label) role=\(role)")
    }

    private func findElement(in element: AXUIElement, label: String, role: String, maxDepth: Int, depth: Int = 0) -> AXUIElement? {
        if depth > maxDepth { return nil }

        if depth > 0 {  // skip the app root
            var roleRef: AnyObject?
            AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &roleRef)
            if let elementRole = roleRef as? String, elementRole == role {
                if matchLabel(element: element, target: label) {
                    return element
                }
            }
        }

        var childrenRef: AnyObject?
        AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &childrenRef)
        if let children = childrenRef as? [AXUIElement] {
            for child in children {
                if let found = findElement(in: child, label: label, role: role, maxDepth: maxDepth, depth: depth + 1) {
                    return found
                }
            }
        }
        return nil
    }

    private func matchLabel(element: AXUIElement, target: String) -> Bool {
        let attrs: [CFString] = [
            kAXTitleAttribute as CFString,
            kAXDescriptionAttribute as CFString,
            kAXValueAttribute as CFString,
            "AXLabel" as CFString,
        ]
        for attr in attrs {
            var ref: AnyObject?
            AXUIElementCopyAttributeValue(element, attr, &ref)
            if let str = ref as? String,
               str.localizedCaseInsensitiveCompare(target) == .orderedSame {
                return true
            }
        }
        return false
    }
}
