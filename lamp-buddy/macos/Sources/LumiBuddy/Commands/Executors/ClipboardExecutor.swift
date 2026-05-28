import AppKit
import Foundation

struct ReadClipboardExecutor: Executor {
    let action = "read_clipboard"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let pb = NSPasteboard.general
        let text = pb.string(forType: .string) ?? ""
        return [
            "text": text,
            "has_text": !text.isEmpty,
            "chars": text.count,
        ]
    }
}

struct WriteClipboardExecutor: Executor {
    let action = "write_clipboard"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let text = params["text"] as? String else {
            throw ExecutorError.missingParam("text")
        }
        let pb = NSPasteboard.general
        pb.clearContents()
        let ok = pb.setString(text, forType: .string)
        return ["written": ok, "chars": text.count]
    }
}
