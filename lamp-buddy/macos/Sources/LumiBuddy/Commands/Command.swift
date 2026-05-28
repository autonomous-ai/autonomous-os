import Foundation

struct IncomingCommand {
    let id: String
    let action: String
    let params: [String: Any]
    let timeoutMs: Int?

    static func decode(from data: Data) throws -> IncomingCommand {
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CommandError.malformed("not a JSON object")
        }
        guard let id = obj["id"] as? String else { throw CommandError.malformed("missing id") }
        guard let action = obj["action"] as? String else { throw CommandError.malformed("missing action") }
        let params = (obj["params"] as? [String: Any]) ?? [:]
        let timeoutMs = obj["timeout_ms"] as? Int
        return IncomingCommand(id: id, action: action, params: params, timeoutMs: timeoutMs)
    }
}

struct CommandResponse {
    let id: String
    let ok: Bool
    let result: [String: Any]?
    let error: String?
    let durationMs: Int

    func encode() throws -> Data {
        var obj: [String: Any] = [
            "id": id,
            "ok": ok,
            "duration_ms": durationMs,
        ]
        if let result { obj["result"] = result }
        if let error { obj["error"] = error }
        return try JSONSerialization.data(withJSONObject: obj, options: [])
    }
}

enum CommandError: LocalizedError {
    case malformed(String)

    var errorDescription: String? {
        switch self {
        case .malformed(let s): return "malformed command: \(s)"
        }
    }
}
