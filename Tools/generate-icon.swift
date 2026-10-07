import AppKit
import Foundation

// Vector-drawn Pakati logo: black central dot and 16 evenly spaced radial strokes.
// Build: swift generate-icon.swift OUTPUT.png [OUTPUT.icns]
// The optional .icns contains every standard 16–1024 pixel macOS icon image.
guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: swift generate-icon.swift OUTPUT.png [OUTPUT.icns]\n", stderr)
    exit(2)
}

func drawIcon(pixelSize: Int) throws -> Data {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil,
                                       pixelsWide: pixelSize, pixelsHigh: pixelSize,
                                       bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                       isPlanar: false, colorSpaceName: .deviceRGB,
                                       bytesPerRow: 0, bitsPerPixel: 0) else {
        throw NSError(domain: "PakatiIcon", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Could not allocate icon bitmap."])
    }
    NSGraphicsContext.saveGraphicsState()
    defer { NSGraphicsContext.restoreGraphicsState() }
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let context = NSGraphicsContext.current!.cgContext
    context.clear(CGRect(x: 0, y: 0, width: pixelSize, height: pixelSize))
    context.setShouldAntialias(true)
    let scale = CGFloat(pixelSize) / 1024
    context.scaleBy(x: scale, y: scale)

    // White macOS tile; the corners outside the tile remain transparent.
    NSColor.white.setFill()
    NSBezierPath(roundedRect: NSRect(x: 70, y: 70, width: 884, height: 884),
                 xRadius: 198, yRadius: 198).fill()

    let center = NSPoint(x: 512, y: 512)
    let markWidth: CGFloat = 884
    let innerRadius = markWidth * 0.235
    let outerRadius = markWidth * 0.395
    // A slight optical weight adjustment keeps all 16 rays readable at 16/32 px.
    let width = pixelSize <= 32 ? max(markWidth * 0.025, 0.75 / scale) : markWidth * 0.025
    NSColor.black.setStroke()
    let rays = NSBezierPath()
    rays.lineWidth = width
    rays.lineCapStyle = .round
    for index in 0..<16 {
        let angle = CGFloat(index) * .pi / 8
        let direction = NSPoint(x: cos(angle), y: sin(angle))
        rays.move(to: NSPoint(x: center.x + innerRadius * direction.x,
                             y: center.y + innerRadius * direction.y))
        rays.line(to: NSPoint(x: center.x + outerRadius * direction.x,
                             y: center.y + outerRadius * direction.y))
    }
    rays.stroke()
    NSColor.black.setFill()
    let dotRadius = markWidth * 0.1
    NSBezierPath(ovalIn: NSRect(x: center.x - dotRadius, y: center.y - dotRadius,
                              width: dotRadius * 2, height: dotRadius * 2)).fill()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "PakatiIcon", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "Could not encode icon PNG."])
    }
    return png
}

let pngDestination = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: pngDestination.deletingLastPathComponent(),
                                      withIntermediateDirectories: true)
try drawIcon(pixelSize: 1024).write(to: pngDestination, options: .atomic)

if CommandLine.arguments.count > 2 {
    let icnsDestination = URL(fileURLWithPath: CommandLine.arguments[2])
    let scratch = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
        .appendingPathComponent("pakati-icon-\(UUID().uuidString)", isDirectory: true)
    let iconset = scratch.appendingPathComponent("AppIcon.iconset", isDirectory: true)
    try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: scratch) }
    let images = [("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
                  ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
                  ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
                  ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
                  ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024)]
    for (name, size) in images {
        try drawIcon(pixelSize: size).write(to: iconset.appendingPathComponent(name))
    }
    try FileManager.default.createDirectory(at: icnsDestination.deletingLastPathComponent(),
                                          withIntermediateDirectories: true)
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    process.arguments = ["--convert", "icns", "--output", icnsDestination.path, iconset.path]
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        throw NSError(domain: "PakatiIcon", code: Int(process.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: "iconutil could not encode the macOS icon."])
    }
}
