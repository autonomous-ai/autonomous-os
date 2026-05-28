// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LumiBuddy",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "LumiBuddy", targets: ["LumiBuddy"])
    ],
    dependencies: [
        // URLSessionWebSocketTask on macOS Ventura (Darwin 22.x / CFNetwork 1410)
        // misreads incoming WS frames as HTTP and retries the upgrade on the
        // same TCP socket. Starscream sidesteps this by doing WS framing itself
        // over raw TCP. Verified via tcpdump 2026-05-22.
        .package(url: "https://github.com/daltoniam/Starscream", from: "4.0.8")
    ],
    targets: [
        .executableTarget(
            name: "LumiBuddy",
            dependencies: ["Starscream"],
            path: "Sources/LumiBuddy"
        )
    ]
)
