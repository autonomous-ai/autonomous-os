import Foundation

actor Reconnect {
    private var attempt: Int = 0
    private let cap: TimeInterval = 30.0

    func nextDelay() -> TimeInterval {
        let delay = min(pow(2.0, Double(attempt)), cap)
        attempt = min(attempt + 1, 10)
        return delay
    }

    func reset() { attempt = 0 }
}
