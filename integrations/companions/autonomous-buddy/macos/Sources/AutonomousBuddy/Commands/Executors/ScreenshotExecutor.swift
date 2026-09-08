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
        try Task.checkCancellation()
        let displayID = CGDirectDisplayID(try ExecutorParameters.integer(
            params, "display_id", default: Int(CGMainDisplayID()), range: 1...Int(UInt32.max)))
        let scale = try ExecutorParameters.number(params, "scale", default: 1, range: 0.01...1)
        let returnFormat = (params["return_format"] as? String) ?? "path"
        guard ["path", "base64", "both"].contains(returnFormat),
              params["return_format"] == nil || params["return_format"] is String else {
            throw ExecutorError.invalidParam("return_format")
        }
        let displayResult = try await ListDisplaysExecutor().execute(params: [:])
        guard let displays = displayResult["displays"] as? [[String: Any]],
              let display = displays.first(where: { ($0["id"] as? Int) == Int(displayID) }),
              let originX = display["x"] as? Int, let originY = display["y"] as? Int,
              let pointWidth = display["width"] as? Int, let pointHeight = display["height"] as? Int,
              pointWidth > 0, pointHeight > 0 else {
            throw ExecutorError.invalidParam("display_id is not an active display")
        }

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

        try Task.checkCancellation()
        let (w, h) = try Self.outputSize(width: rawImage.width, height: rawImage.height, scale: scale)
        let image: CGImage
        if w != rawImage.width || h != rawImage.height {
            let colorSpace = rawImage.colorSpace ?? CGColorSpaceCreateDeviceRGB()
            let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue
            guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                                      bytesPerRow: 0, space: colorSpace, bitmapInfo: bitmapInfo) else {
                throw ExecutorError.actionFailed("could not allocate scaled screenshot")
            }
            ctx.interpolationQuality = .high
            ctx.draw(rawImage, in: CGRect(x: 0, y: 0, width: w, height: h))
            guard let scaled = ctx.makeImage() else {
                throw ExecutorError.actionFailed("could not scale screenshot")
            }
            image = scaled
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

        try Task.checkCancellation()
        let saveURL = try await ScreenshotStore.shared.save(data: jpegData as Data)

        let displayScale = Double(rawImage.width) / Double(pointWidth)

        var result: [String: Any] = [
            "path": saveURL.path,
            "width": image.width,
            "height": image.height,
            "display_id": Int(displayID),
            "display_scale": displayScale,
            "display_origin_x": originX,
            "display_origin_y": originY,
            "point_width": pointWidth,
            "point_height": pointHeight,
            "capture_scale": Double(image.width) / Double(rawImage.width),
            "image_to_global_points": Self.imageTransform(
                originX: Double(originX), originY: Double(originY),
                pointWidth: Double(pointWidth), pointHeight: Double(pointHeight),
                imageWidth: image.width, imageHeight: image.height),
            "bytes": jpegData.length,
            "mime": "image/jpeg",
        ]
        if returnFormat == "base64" || returnFormat == "both" {
            result["image_b64"] = (jpegData as Data).base64EncodedString()
        }
        return result
    }

    static func outputSize(width: Int, height: Int, scale: Double) throws -> (Int, Int) {
        guard width > 0, height > 0, scale.isFinite, (0.01...1).contains(scale) else {
            throw ExecutorError.invalidParam("screenshot dimensions or scale")
        }
        let w = (Double(width) * scale).rounded(.down)
        let h = (Double(height) * scale).rounded(.down)
        guard w >= 1, h >= 1, w <= 16_384, h <= 16_384, w * h <= 40_000_000 else {
            throw ExecutorError.invalidParam("scaled screenshot must be 1...16384 per axis and at most 40 million pixels")
        }
        return (Int(w), Int(h))
    }

    // Use the actual encoded dimensions, including rounding, and the display's global origin.
    static func imageTransform(originX: Double, originY: Double, pointWidth: Double,
                               pointHeight: Double, imageWidth: Int, imageHeight: Int) -> [String: Double] {
        ["origin_x": originX, "origin_y": originY,
         "scale_x": pointWidth / Double(imageWidth), "scale_y": pointHeight / Double(imageHeight)]
    }
}

// Serialize persistence so concurrent captures cannot overwrite or race retention pruning.
actor ScreenshotStore {
    static let shared = ScreenshotStore()
    private let directory: URL?

    init(directory: URL? = nil) {
        self.directory = directory
    }

    func save(data: Data) throws -> URL {
        let fm = FileManager.default
        guard let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw ExecutorError.actionFailed("application support directory unavailable")
        }
        let dir = (directory ?? support.appendingPathComponent("AutonomousBuddy", isDirectory: true)
            .appendingPathComponent("screenshots", isDirectory: true)).standardizedFileURL
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("capture-\(UUID().uuidString).jpg")
        try data.write(to: url, options: .atomic)
        let captures = try fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])
            .filter { $0.lastPathComponent.hasPrefix("capture-") && $0.pathExtension == "jpg" }
            .sorted {
                let left = (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let right = (try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return left > right
            }
        // Directory enumeration can resolve /var to /private/var while the caller's
        // URL retains the alias. The unique basename identifies this capture under either URL.
        for old in captures.filter({ $0.lastPathComponent != url.lastPathComponent }).dropFirst(19) {
            try fm.removeItem(at: old)
        }
        return url
    }
}
