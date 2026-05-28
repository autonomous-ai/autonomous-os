import AppKit
import CoreGraphics
import Foundation

// Coordinate system note:
//   All x/y values are in the GLOBAL DISPLAY COORDINATE SPACE used by CGEvent —
//   top-left origin, units = points (NOT pixels). On Retina, the screenshot is
//   at pixel resolution; divide returned pixel coords by `display_scale` (in
//   screenshot result) to get point coords for click_at / mouse_move.

struct ClickAtExecutor: Executor {
    let action = "click_at"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let x = try requireCGFloat(params, key: "x")
        let y = try requireCGFloat(params, key: "y")
        let buttonName = (params["button"] as? String) ?? "left"
        let clicks = max(1, (params["clicks"] as? Int) ?? 1)

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for mouse click")
        }

        let (button, downType, upType): (CGMouseButton, CGEventType, CGEventType)
        switch buttonName.lowercased() {
        case "right":  (button, downType, upType) = (.right,  .rightMouseDown, .rightMouseUp)
        case "middle": (button, downType, upType) = (.center, .otherMouseDown, .otherMouseUp)
        default:       (button, downType, upType) = (.left,   .leftMouseDown,  .leftMouseUp)
        }

        let pt = CGPoint(x: x, y: y)
        for n in 0..<clicks {
            guard let down = CGEvent(mouseEventSource: nil, mouseType: downType, mouseCursorPosition: pt, mouseButton: button),
                  let up = CGEvent(mouseEventSource: nil, mouseType: upType, mouseCursorPosition: pt, mouseButton: button) else {
                throw ExecutorError.actionFailed("could not create mouse event")
            }
            down.setIntegerValueField(.mouseEventClickState, value: Int64(n + 1))
            up.setIntegerValueField(.mouseEventClickState, value: Int64(n + 1))
            down.post(tap: .cghidEventTap)
            up.post(tap: .cghidEventTap)
            if n < clicks - 1 {
                try? await Task.sleep(nanoseconds: 60_000_000)
            }
        }
        return ["clicked": true, "x": Int(x), "y": Int(y), "button": buttonName, "clicks": clicks]
    }
}

struct ScrollExecutor: Executor {
    let action = "scroll"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let dy = (params["delta_y"] as? Int) ?? 0
        let dx = (params["delta_x"] as? Int) ?? 0

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for scroll")
        }

        // Optionally move cursor first so scroll lands on the right element
        if let x = params["x"] as? Double, let y = params["y"] as? Double {
            if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) {
                move.post(tap: .cghidEventTap)
            }
        } else if let x = params["x"] as? Int, let y = params["y"] as? Int {
            if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) {
                move.post(tap: .cghidEventTap)
            }
        }

        guard let scroll = CGEvent(
            scrollWheelEvent2Source: nil,
            units: .pixel,
            wheelCount: 2,
            wheel1: Int32(dy),
            wheel2: Int32(dx),
            wheel3: 0
        ) else {
            throw ExecutorError.actionFailed("could not create scroll event")
        }
        scroll.post(tap: .cghidEventTap)
        return ["scrolled": true, "delta_y": dy, "delta_x": dx]
    }
}

struct MouseMoveExecutor: Executor {
    let action = "mouse_move"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let x = try requireCGFloat(params, key: "x")
        let y = try requireCGFloat(params, key: "y")
        let smooth = (params["smooth"] as? Bool) ?? false

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for mouse move")
        }

        if smooth {
            // Use NSEvent.mouseLocation (bottom-left origin) and convert to CGEvent space (top-left).
            let cur = NSEvent.mouseLocation
            let screenH = NSScreen.main?.frame.height ?? 0
            let from = CGPoint(x: cur.x, y: screenH - cur.y)
            let to = CGPoint(x: x, y: y)
            let steps = 24
            for i in 1...steps {
                let t = CGFloat(i) / CGFloat(steps)
                let pt = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
                if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left) {
                    move.post(tap: .cghidEventTap)
                }
                try? await Task.sleep(nanoseconds: 8_000_000)
            }
        } else if let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left) {
            move.post(tap: .cghidEventTap)
        }
        return ["moved": true, "x": Int(x), "y": Int(y), "smooth": smooth]
    }
}

struct DragExecutor: Executor {
    let action = "drag"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let from = params["from"] as? [String: Any],
              let to = params["to"] as? [String: Any] else {
            throw ExecutorError.missingParam("from/to")
        }
        let x1 = try requireCGFloat(from, key: "x")
        let y1 = try requireCGFloat(from, key: "y")
        let x2 = try requireCGFloat(to, key: "x")
        let y2 = try requireCGFloat(to, key: "y")
        let durationMs = max(50, (params["duration_ms"] as? Int) ?? 300)

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for drag")
        }

        let start = CGPoint(x: x1, y: y1)
        let end = CGPoint(x: x2, y: y2)

        guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left) else {
            throw ExecutorError.actionFailed("could not create mouse down")
        }
        down.post(tap: .cghidEventTap)

        let steps = max(1, durationMs / 16)
        let stepNs = UInt64(durationMs * 1_000_000 / steps)
        for i in 1...steps {
            let t = CGFloat(i) / CGFloat(steps)
            let pt = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
            if let drag = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: pt, mouseButton: .left) {
                drag.post(tap: .cghidEventTap)
            }
            try? await Task.sleep(nanoseconds: stepNs)
        }

        guard let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: end, mouseButton: .left) else {
            throw ExecutorError.actionFailed("could not create mouse up")
        }
        up.post(tap: .cghidEventTap)
        return ["dragged": true, "from": ["x": Int(x1), "y": Int(y1)], "to": ["x": Int(x2), "y": Int(y2)]]
    }
}

struct CursorPosExecutor: Executor {
    let action = "cursor_pos"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        // NSEvent.mouseLocation: origin bottom-left of the MENU-BAR (primary) screen, in POINTS.
        // CGEvent coords: origin top-left of the same primary screen. Same x, flipped y.
        // Y-flip pivot MUST be the primary screen's height — NOT NSScreen.main (which is the
        // "key window" screen and changes when focus moves between displays).
        return await MainActor.run {
            let pt = NSEvent.mouseLocation
            let screens = NSScreen.screens
            let primary = screens.first(where: { $0.frame.origin == .zero }) ?? screens.first
            let screenH = primary?.frame.size.height ?? 0
            let scale = primary?.backingScaleFactor ?? 1.0
            let cgY = screenH - pt.y
            return [
                "x": Int(pt.x.rounded()),
                "y": Int(cgY.rounded()),
                "screen_height": Int(screenH),
                "backing_scale": Double(scale),
            ]
        }
    }
}

// MARK: - helpers

private func requireCGFloat(_ params: [String: Any], key: String) throws -> CGFloat {
    if let v = params[key] as? Double { return CGFloat(v) }
    if let v = params[key] as? Int { return CGFloat(v) }
    throw ExecutorError.missingParam(key)
}
