import AppKit
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct ListDisplaysExecutor: Executor {
    let action = "list_displays"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        // Use NSScreen rather than CGDisplayBounds. CGDisplayBounds reports each display's
        // top-left in the "global display coordinate space", but for bottom-aligned multi-display
        // setups it doesn't reflect the actual arrangement that CGEvent dispatches against.
        // NSScreen.frame DOES carry arrangement (in bottom-left-origin space), and we y-flip
        // ourselves using the primary (menu-bar / origin==.zero) screen as pivot.
        return try await MainActor.run {
            let screens = NSScreen.screens
            guard !screens.isEmpty else {
                throw ExecutorError.actionFailed("no screens available")
            }
            // Primary = the screen whose NSScreen origin is (0,0). That's the menu-bar screen
            // which defines CGEvent's global y origin (top-left of it = CGEvent (0,0)).
            let primary = screens.first(where: { $0.frame.origin == .zero }) ?? screens[0]
            let primaryTopY = primary.frame.origin.y + primary.frame.size.height
            let mainID = CGMainDisplayID()

            var list: [[String: Any]] = []
            for screen in screens {
                guard let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32 else {
                    continue
                }
                let id = CGDirectDisplayID(n)
                let scale = screen.backingScaleFactor
                let cgTopY = primaryTopY - (screen.frame.origin.y + screen.frame.size.height)
                let pointW = screen.frame.size.width
                let pointH = screen.frame.size.height
                list.append([
                    "id": Int(id),
                    "is_main": id == mainID,
                    "x": Int(screen.frame.origin.x),
                    "y": Int(cgTopY),
                    "width": Int(pointW),
                    "height": Int(pointH),
                    "pixel_width": Int(pointW * scale),
                    "pixel_height": Int(pointH * scale),
                    "scale": Double(scale),
                ])
            }
            return ["displays": list, "count": list.count]
        }
    }
}

struct ScreenshotExecutor: Executor {
    let action = "screenshot"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        let displayID: CGDirectDisplayID
        if let id = params["display_id"] as? Int {
            displayID = CGDirectDisplayID(id)
        } else {
            displayID = CGMainDisplayID()
        }
        let scale = (params["scale"] as? Double) ?? 1.0
        let returnFormat = (params["return_format"] as? String) ?? "path"  // "path", "base64", or "both"

        if !ScreenRecordingCheck.isTrusted() {
            ScreenRecordingCheck.requestPrompt()
            throw ExecutorError.permissionDenied("Screen Recording access required — grant in System Settings → Privacy & Security, then re-run")
        }

        // CGDisplayCreateImage is deprecated in macOS 14.4 in favor of ScreenCaptureKit, but still works.
        // We accept the deprecation warning for now — ScreenCaptureKit's SCScreenshotManager.captureImage
        // is macOS 14+ only and our minimum is macOS 13.
        guard let rawImage = CGDisplayCreateImage(displayID) else {
            throw ExecutorError.actionFailed("could not capture display \(displayID) — permission granted but capture failed")
        }

        let image: CGImage
        if scale != 1.0 && scale > 0 {
            let w = Int(Double(rawImage.width) * scale)
            let h = Int(Double(rawImage.height) * scale)
            let colorSpace = rawImage.colorSpace ?? CGColorSpaceCreateDeviceRGB()
            let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue
            if let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: colorSpace, bitmapInfo: bitmapInfo) {
                ctx.interpolationQuality = .high
                ctx.draw(rawImage, in: CGRect(x: 0, y: 0, width: w, height: h))
                image = ctx.makeImage() ?? rawImage
            } else {
                image = rawImage
            }
        } else {
            image = rawImage
        }

        // JPEG q=0.8 gives ~5-10× smaller payload than PNG for typical desktop
        // screenshots, with negligible perceptual loss for vision LLM input.
        let jpegData = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(jpegData, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw ExecutorError.actionFailed("could not create JPEG destination")
        }
        let jpegOptions: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.8]
        CGImageDestinationAddImage(dest, image, jpegOptions as CFDictionary)
        if !CGImageDestinationFinalize(dest) {
            throw ExecutorError.actionFailed("could not encode JPEG")
        }

        let saveURL = try saveDefaultPath(data: jpegData as Data)

        // Retina point↔pixel scale of the source display.
        let mode = CGDisplayCopyDisplayMode(displayID)
        let pixelW = mode?.pixelWidth ?? image.width
        let pointW = mode?.width ?? image.width
        let displayScale = pointW > 0 ? Double(pixelW) / Double(pointW) : 1.0

        var result: [String: Any] = [
            "path": saveURL.path,
            "width": image.width,
            "height": image.height,
            "display_id": Int(displayID),
            "display_scale": displayScale,
            "bytes": jpegData.length,
            "mime": "image/jpeg",
        ]
        if returnFormat == "base64" || returnFormat == "both" {
            result["image_b64"] = (jpegData as Data).base64EncodedString()
        }
        return result
    }

    private func saveDefaultPath(data: Data) throws -> URL {
        let fm = FileManager.default
        let dir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("LampBuddy", isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("latest.jpg")
        try data.write(to: url, options: .atomic)
        return url
    }
}
