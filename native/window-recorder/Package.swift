// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "KatnipWindowRecorder",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "katnip-window-recorder",
            targets: ["KatnipWindowRecorder"]
        )
    ],
    targets: [
        .executableTarget(
            name: "KatnipWindowRecorder"
        )
    ]
)