import AppKit
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct ListDisplaysExecutor: Executor {
    let action = "list_displays"

    func execute(params: [String: Any]) async throws -> [String: Any] {
        var count: UInt32 = 0
        CGGetActiveDisplayList(0, nil, &count)
        var ids = Array<CGDirectDisplayID>(repeating: 0, count: Int(count))
        CGGetActiveDisplayList(count, &ids, &count)

        let main = CGMainDisplayID()
        var list: [[String: Any]] = []
        for id in ids {
            let bounds = CGDisplayBounds(id)
            let mode = CGDisplayCopyDisplayMode(id)
            let pixelW = mode?.pixelWidth ?? Int(bounds.width)
            let pointW = mode?.width ?? Int(bounds.width)
            let scale = pointW > 0 ? Double(pixelW) / Double(pointW) : 1.0
            list.append([
                "id": Int(id),
                "is_main": id == main,
                "x": Int(bounds.origin.x),
                "y": Int(bounds.origin.y),
                "width": Int(bounds.width),
                "height": Int(bounds.height),
                "pixel_width": pixelW,
                "pixel_height": mode?.pixelHeight ?? Int(bounds.height),
                "scale": scale,
            ])
        }
        return ["displays": list, "count": list.count]
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

        let pngData = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(pngData, UTType.png.identifier as CFString, 1, nil) else {
            throw ExecutorError.actionFailed("could not create PNG destination")
        }
        CGImageDestinationAddImage(dest, image, nil)
        if !CGImageDestinationFinalize(dest) {
            throw ExecutorError.actionFailed("could not encode PNG")
        }

        let saveURL = try saveDefaultPath(data: pngData as Data)

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
            "bytes": pngData.length,
        ]
        if returnFormat == "base64" || returnFormat == "both" {
            result["image_b64"] = (pngData as Data).base64EncodedString()
        }
        return result
    }

    private func saveDefaultPath(data: Data) throws -> URL {
        let fm = FileManager.default
        let dir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("LumiBuddy", isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("latest.png")
        try data.write(to: url, options: .atomic)
        return url
    }
}
