import Foundation

actor AuditLog {
    private let url: URL

    // fileURL is the canonical on-disk audit log location, exposed so the menu
    // bar's "Show audit log…" item can open it without instantiating an actor.
    static var fileURL: URL {
        let fm = FileManager.default
        let dir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("LampBuddy", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("audit.log")
    }

    init() {
        self.url = Self.fileURL
    }

    func append(action: String, summary: String, ok: Bool, error: String?) {
        let record: [String: Any] = [
            "ts": ISO8601DateFormatter().string(from: Date()),
            "action": action,
            "summary": summary,
            "ok": ok,
            "error": error ?? NSNull(),
        ]
        guard var line = try? JSONSerialization.data(withJSONObject: record, options: []) else { return }
        line.append(0x0A)

        let fm = FileManager.default
        if !fm.fileExists(atPath: url.path) {
            fm.createFile(atPath: url.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: line)
        } catch {
            // Silently fail; audit log is best-effort.
        }
    }
}
