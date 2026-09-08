import AppKit
import Foundation

// Resolve paths on the Mac, never on the device sending the command. This lets
// a device agent open ~/Downloads without guessing the Mac account name.
struct OpenPathExecutor: Executor {
    let action = "open_path"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        try Task.checkCancellation()
        let url = try Self.resolvePath(params["path"], home: FileManager.default.homeDirectoryForCurrentUser)
        let mode = try Self.mode(params["mode"])
        let app: String?
        if let value = params["app"] {
            guard let name = value as? String, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw ExecutorError.invalidParam("app")
            }
            guard mode == "open" else { throw ExecutorError.invalidParam("app is only supported with mode open") }
            app = name
        } else {
            app = nil
        }
        return try await open(url, mode: mode, app: app)
    }

    static func mode(_ value: Any?) throws -> String {
        guard let value else { return "open" }
        guard let mode = value as? String, ["open", "reveal"].contains(mode) else {
            throw ExecutorError.invalidParam("mode must be open or reveal")
        }
        return mode
    }

    static func resolvePath(_ value: Any?, home: URL) throws -> URL {
        guard let path = value as? String, !path.isEmpty, path.utf8.count <= 16_384, !path.utf8.contains(0) else {
            throw ExecutorError.invalidParam("path must be a nonempty filesystem path")
        }
        let url: URL
        if path == "~" {
            url = home
        } else if path.hasPrefix("~/") {
            url = home.appendingPathComponent(String(path.dropFirst(2)))
        } else if path.hasPrefix("/") {
            url = URL(fileURLWithPath: path)
        } else {
            throw ExecutorError.invalidParam("path must be absolute or begin with ~/")
        }
        let resolved = url.standardizedFileURL
        guard FileManager.default.fileExists(atPath: resolved.path) else {
            throw ExecutorError.actionFailed("path does not exist on the Mac: \(resolved.path)")
        }
        return resolved
    }

    @MainActor
    private func open(_ url: URL, mode: String, app: String?) async throws -> [String: Any] {
        try Task.checkCancellation()
        var directory = ObjCBool(false)
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) else {
            throw ExecutorError.actionFailed("path no longer exists on the Mac: \(url.path)")
        }
        let application: NSRunningApplication?
        if mode == "reveal" {
            NSWorkspace.shared.activateFileViewerSelecting([url])
            application = NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.apple.finder" }
        } else if let app {
            guard let appURL = OpenAppExecutor.resolveAppURL(named: app) else {
                throw ExecutorError.actionFailed("app not found: \(app)")
            }
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            try Task.checkCancellation()
            application = try await NSWorkspace.shared.open([url], withApplicationAt: appURL, configuration: configuration)
        } else {
            let handlerBundleID = NSWorkspace.shared.urlForApplication(toOpen: url).flatMap { Bundle(url: $0)?.bundleIdentifier }
            try Task.checkCancellation()
            guard NSWorkspace.shared.open(url) else { throw ExecutorError.actionFailed("macOS rejected opening path: \(url.path)") }
            application = handlerBundleID.flatMap { id in NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == id } }
        }
        try Task.checkCancellation()
        var result = OpenAppExecutor.activationResult(application)
        result["path"] = url.path
        result["is_directory"] = directory.boolValue
        result["mode"] = mode
        result[mode == "reveal" ? "reveal_requested" : "opened"] = true
        return result
    }
}
