// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LumiBuddy",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "LumiBuddy", targets: ["LumiBuddy"])
    ],
    targets: [
        .executableTarget(
            name: "LumiBuddy",
            path: "Sources/LumiBuddy"
        )
    ]
)
