import Foundation

protocol Executor {
    var action: String { get }
    func execute(params: [String: Any]) async throws -> [String: Any]
}

enum ExecutorError: LocalizedError {
    case missingParam(String)
    case invalidParam(String)
    case permissionDenied(String)
    case actionFailed(String)

    var errorDescription: String? {
        switch self {
        case .missingParam(let k): return "missing param: \(k)"
        case .invalidParam(let k): return "invalid param: \(k)"
        case .permissionDenied(let s): return "permission denied: \(s)"
        case .actionFailed(let s): return s
        }
    }
}
