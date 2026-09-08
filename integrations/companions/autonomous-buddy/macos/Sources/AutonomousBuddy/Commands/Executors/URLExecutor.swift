import AppKit
import Foundation

struct OpenURLExecutor: Executor {
    let action = "open_url"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let urlString = params["url"] as? String,
              let url = URL(string: urlString), let scheme = url.scheme, !scheme.isEmpty else {
            throw ExecutorError.invalidParam("url must be absolute")
        }
        let browser = try Self.browserSelection(params["browser"])
        try Task.checkCancellation()
        return try await open(url, browser: browser)
    }

    @MainActor
    private func open(_ url: URL, browser: (name: String, bundleID: String?)) async throws -> [String: Any] {
        try Task.checkCancellation()
        if let bundleID = browser.bundleID {
            guard let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
                throw ExecutorError.actionFailed("requested browser is not installed: \(browser.name)")
            }
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            try Task.checkCancellation()
            let app = try await NSWorkspace.shared.open([url], withApplicationAt: appURL, configuration: config)
            try Task.checkCancellation()
            var result = OpenAppExecutor.activationResult(app)
            result["opened"] = true
            result["browser"] = browser.name
            return result
        }

        // The default handler can be a browser or an app registered for a custom
        // URL scheme. A successful request does not establish page loading/focus.
        let handlerURL = NSWorkspace.shared.urlForApplication(toOpen: url)
        let handlerBundleID = handlerURL.flatMap { Bundle(url: $0)?.bundleIdentifier }
        try Task.checkCancellation()
        var result = try Self.defaultOpenResult(accepted: NSWorkspace.shared.open(url))
        let app = handlerBundleID.flatMap { id in
            NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == id }
        }
        result.merge(OpenAppExecutor.activationResult(app)) { _, new in new }
        if app == nil, let handlerBundleID { result["bundle_id"] = handlerBundleID }
        return result
    }

    static func defaultOpenResult(accepted: Bool) throws -> [String: Any] {
        guard accepted else { throw ExecutorError.actionFailed("macOS rejected opening URL with its default handler") }
        return ["opened": true, "browser": "default"]
    }

    static func browserSelection(_ value: Any?) throws -> (name: String, bundleID: String?) {
        guard let value else { return ("default", nil) }
        guard let name = value as? String else { throw ExecutorError.invalidParam("browser") }
        let browser = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch browser {
        case "default": return (browser, nil)
        case "chrome", "google chrome": return (browser, "com.google.Chrome")
        case "safari": return (browser, "com.apple.Safari")
        case "firefox": return (browser, "org.mozilla.firefox")
        case "arc": return (browser, "company.thebrowser.Browser")
        case "edge", "microsoft edge": return (browser, "com.microsoft.edgemac")
        case "brave": return (browser, "com.brave.Browser")
        default: throw ExecutorError.invalidParam("unsupported browser: \(name)")
        }
    }
}
