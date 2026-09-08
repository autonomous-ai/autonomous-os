import CoreFoundation
import Foundation

// Validate JSON numbers before conversion to integer or CoreGraphics types.
enum ExecutorParameters {
    static func number(_ params: [String: Any], _ key: String, default fallback: Double? = nil,
                       range: ClosedRange<Double>) throws -> Double {
        guard let raw = params[key] else {
            if let fallback { return fallback }
            throw ExecutorError.missingParam(key)
        }
        guard let number = raw as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else {
            throw ExecutorError.invalidParam(key)
        }
        let value = number.doubleValue
        guard value.isFinite, range.contains(value) else { throw ExecutorError.invalidParam(key) }
        return value
    }

    static func integer(_ params: [String: Any], _ key: String, default fallback: Int? = nil,
                        range: ClosedRange<Int>) throws -> Int {
        let value = try number(params, key, default: fallback.map(Double.init),
                               range: Double(range.lowerBound)...Double(range.upperBound))
        guard value.rounded(.towardZero) == value, let integer = Int(exactly: value) else {
            throw ExecutorError.invalidParam(key)
        }
        return integer
    }
}
