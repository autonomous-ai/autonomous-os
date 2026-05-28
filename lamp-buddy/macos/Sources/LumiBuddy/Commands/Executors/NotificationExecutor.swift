import Foundation
import UserNotifications

struct NotificationExecutor: Executor {
    let action = "notification"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let title = params["title"] as? String, !title.isEmpty else {
            throw ExecutorError.missingParam("title")
        }
        let body = (params["body"] as? String) ?? ""

        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            let granted = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
            if !granted {
                throw ExecutorError.permissionDenied("notification authorization denied")
            }
        } else if settings.authorizationStatus == .denied {
            throw ExecutorError.permissionDenied("notification authorization denied")
        }

        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        try await center.add(request)
        return ["delivered": true]
    }
}
