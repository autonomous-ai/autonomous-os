import AppKit

// Generate the Dock icon from the same SF Symbol used by the native menu bar.
// Only source is checked in; PNG/iconset/ICNS outputs remain build artifacts.
guard CommandLine.arguments.count == 2 else {
    fatalError("Usage: swift generate-icon.swift <output-directory>")
}
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let iconset = output.appendingPathComponent("AutonomousBuddy.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
guard let symbol = NSImage(systemSymbolName: "lightbulb.fill", accessibilityDescription: nil)?
    .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 700, weight: .regular)) else {
    fatalError("macOS lightbulb.fill symbol is unavailable")
}

func render(pixels: Int) throws -> Data {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        fatalError("Cannot allocate icon bitmap")
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let scale = CGFloat(pixels) / 1024
    context.cgContext.scaleBy(x: scale, y: scale)
    NSColor(calibratedRed: 0.085, green: 0.09, blue: 0.115, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824),
                 xRadius: 184, yRadius: 184).fill()

    let ratio = min(470 / symbol.size.width, 620 / symbol.size.height)
    let size = NSSize(width: symbol.size.width * ratio, height: symbol.size.height * ratio)
    let rect = NSRect(x: (1024 - size.width) / 2, y: (1024 - size.height) / 2,
                      width: size.width, height: size.height)
    // Tint inside a transparency layer so sourceIn never recolors the backdrop.
    context.cgContext.beginTransparencyLayer(auxiliaryInfo: nil)
    symbol.draw(in: rect)
    NSColor.systemYellow.setFill()
    rect.fill(using: .sourceIn)
    context.cgContext.endTransparencyLayer()
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    guard let data = bitmap.representation(using: .png, properties: [:]) else {
        fatalError("Cannot encode icon PNG")
    }
    return data
}

for points in [16, 32, 128, 256, 512] {
    for multiplier in [1, 2] {
        let suffix = multiplier == 2 ? "@2x" : ""
        try render(pixels: points * multiplier).write(
            to: iconset.appendingPathComponent("icon_\(points)x\(points)\(suffix).png")
        )
    }
}
try render(pixels: 1024).write(to: output.appendingPathComponent("AutonomousBuddy.png"))
