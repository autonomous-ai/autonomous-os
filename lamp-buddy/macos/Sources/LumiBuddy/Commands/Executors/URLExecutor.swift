import AppKit
import Foundation

struct OpenURLExecutor: Executor {
    let action = "open_url"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let urlString = params["url"] as? String,
              let url = URL(string: urlString) else {
            throw ExecutorError.invalidParam("url")
        }
        let browser = (params["browser"] as? String)?.lowercased()

        if let browser, browser != "default",
           let bundleID = Self.bundleID(for: browser),
           let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            _ = try await NSWorkspace.shared.open([url], withApplicationAt: appURL, configuration: config)
            return ["opened": true, "browser": browser]
        }

        NSWorkspace.shared.open(url)
        return ["opened": true, "browser": "default"]
    }

    private static func bundleID(for browser: String) -> String? {
        switch browser {
        case "chrome", "google chrome": return "com.google.Chrome"
        case "safari": return "com.apple.Safari"
        case "firefox": return "org.mozilla.firefox"
        case "arc": return "company.thebrowser.Browser"
        case "edge", "microsoft edge": return "com.microsoft.edgemac"
        case "brave": return "com.brave.Browser"
        default: return nil
        }
    }
}
