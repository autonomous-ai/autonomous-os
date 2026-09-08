import Foundation
import CoreFoundation

struct IncomingCommand {
    let id: String
    let action: String
    let params: [String: Any]
    let timeoutMs: Int?

    static func decode(from data: Data) throws -> IncomingCommand {
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw CommandError.malformed("not a JSON object")
        }
        guard let id = obj["id"] as? String, !id.isEmpty, id.utf8.count <= 128 else { throw CommandError.malformed("invalid id") }
        guard let action = obj["action"] as? String, !action.isEmpty, action.utf8.count <= 64 else { throw CommandError.malformed("invalid action") }
        if let params = obj["params"], !(params is [String: Any]) {
            throw CommandError.malformed("params must be an object")
        }
        let params = (obj["params"] as? [String: Any]) ?? [:]
        var timeoutMs: Int?
        if let value = obj["timeout_ms"] {
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite,
                  number.doubleValue.rounded() == number.doubleValue,
                  number.doubleValue == 0 || (500...60000).contains(number.doubleValue) else {
                throw CommandError.malformed("timeout_ms must be 0 (default) or an integer from 500 to 60000")
            }
            timeoutMs = number.intValue == 0 ? nil : number.intValue
        }
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
