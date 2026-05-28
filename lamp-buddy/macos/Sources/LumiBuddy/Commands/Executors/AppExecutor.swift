import AppKit
import Foundation

struct OpenAppExecutor: Executor {
    let action = "open_app"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let appName = params["app"] as? String, !appName.isEmpty else {
            throw ExecutorError.missingParam("app")
        }
        guard let url = Self.resolveAppURL(named: appName) else {
            throw ExecutorError.actionFailed("app not found: \(appName)")
        }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        let runningApp = try await NSWorkspace.shared.openApplication(at: url, configuration: config)
        return [
            "pid": Int(runningApp.processIdentifier),
            "bundle_id": runningApp.bundleIdentifier ?? "",
        ]
    }

    static func resolveAppURL(named name: String) -> URL? {
        // 1. Bundle ID (e.g. "com.google.Chrome")
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: name) {
            return url
        }
        // 2. Standard locations
        let suffix = name.hasSuffix(".app") ? name : "\(name).app"
        let bases = ["/Applications", "/System/Applications", "/Applications/Utilities", "/System/Applications/Utilities"]
        for base in bases {
            let path = "\(base)/\(suffix)"
            if FileManager.default.fileExists(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        // 3. ~/Applications
        let home = FileManager.default.homeDirectoryForCurrentUser
        let homeApps = home.appendingPathComponent("Applications").appendingPathComponent(suffix)
        if FileManager.default.fileExists(atPath: homeApps.path) {
            return homeApps
        }
        return nil
    }
}

struct CloseAppExecutor: Executor {
    let action = "close_app"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let appName = params["app"] as? String, !appName.isEmpty else {
            throw ExecutorError.missingParam("app")
        }
        let force = (params["force"] as? Bool) ?? false
        let script: String
        if force {
            script = "tell application \"\(appName)\" to quit saving no"
        } else {
            script = "tell application \"\(appName)\" to quit"
        }
        guard let appleScript = NSAppleScript(source: script) else {
            throw ExecutorError.actionFailed("could not build AppleScript")
        }
        var errorDict: NSDictionary?
        appleScript.executeAndReturnError(&errorDict)
        if let errorDict {
            let msg = (errorDict[NSAppleScript.errorMessage] as? String) ?? "AppleScript failed"
            throw ExecutorError.actionFailed(msg)
        }
        return ["closed": true]
    }
}
