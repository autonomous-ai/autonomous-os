import AppKit
import CoreGraphics
import Foundation

struct TypeTextExecutor: Executor {
    let action = "type_text"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let text = params["text"] as? String else { throw ExecutorError.missingParam("text") }
        let delayMs = (params["delay_ms"] as? Int) ?? 15

        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for typing")
        }
        guard let source = CGEventSource(stateID: .hidSystemState) else {
            throw ExecutorError.actionFailed("could not create event source")
        }

        for scalar in text.unicodeScalars {
            let utf16 = Array(String(scalar).utf16)
            guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
                  let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
                continue
            }
            keyDown.flags = []
            keyUp.flags = []
            utf16.withUnsafeBufferPointer { buf in
                if let base = buf.baseAddress {
                    keyDown.keyboardSetUnicodeString(stringLength: buf.count, unicodeString: base)
                    keyUp.keyboardSetUnicodeString(stringLength: buf.count, unicodeString: base)
                }
            }
            keyDown.post(tap: .cghidEventTap)
            keyUp.post(tap: .cghidEventTap)
            if delayMs > 0 {
                try? await Task.sleep(nanoseconds: UInt64(delayMs) * 1_000_000)
            }
        }
        return ["typed_chars": text.count]
    }
}

struct KeyComboExecutor: Executor {
    let action = "key_combo"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        guard let keys = params["keys"] as? [String], !keys.isEmpty else {
            throw ExecutorError.invalidParam("keys")
        }
        guard AccessibilityCheck.isTrusted() else {
            AccessibilityCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Accessibility access required for key combos")
        }

        var flags: CGEventFlags = []
        var keyCode: CGKeyCode?
        for raw in keys {
            let k = raw.lowercased()
            switch k {
            case "cmd", "command", "meta": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "opt", "option", "alt": flags.insert(.maskAlternate)
            case "ctrl", "control": flags.insert(.maskControl)
            case "fn": flags.insert(.maskSecondaryFn)
            default:
                guard let code = KeyMap.code(for: k) else {
                    throw ExecutorError.invalidParam("unknown key: \(raw)")
                }
                keyCode = code
            }
        }
        guard let code = keyCode else {
            throw ExecutorError.invalidParam("no non-modifier key in combo")
        }
        guard let source = CGEventSource(stateID: .hidSystemState),
              let keyDown = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
              let keyUp = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else {
            throw ExecutorError.actionFailed("could not create event")
        }
        keyDown.flags = flags
        keyUp.flags = flags
        keyDown.post(tap: .cghidEventTap)
        keyUp.post(tap: .cghidEventTap)
        return ["dispatched": true]
    }
}

enum KeyMap {
    static let map: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
        "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
        "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
        "return": 36, "enter": 36, "ret": 36,
        "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
        ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
        "tab": 48, "space": 49, "spc": 49, "`": 50,
        "delete": 51, "backspace": 51, "bs": 51,
        "escape": 53, "esc": 53,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118,
        "f5": 96, "f6": 97, "f7": 98, "f8": 100,
        "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        "left": 123, "right": 124, "down": 125, "up": 126,
        "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
        "fwd_delete": 117, "forward_delete": 117,
    ]

    static func code(for key: String) -> CGKeyCode? {
        return map[key]
    }
}
