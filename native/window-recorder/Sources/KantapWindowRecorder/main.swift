import AppKit
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

enum RecorderError: Error, CustomStringConvertible {
    case invalidArguments(String)
    case windowNotFound(CGWindowID)
    case writerCreationFailed
    case cannotAddWriterInput
    case writerFailed(String)

    var description: String {
        switch self {
        case .invalidArguments(let message):
            return message
        case .windowNotFound(let windowID):
            return "No ScreenCaptureKit window found for window ID \(windowID)"
        case .writerCreationFailed:
            return "Could not create AVAssetWriter"
        case .cannotAddWriterInput:
            return "Could not add video input to AVAssetWriter"
        case .writerFailed(let message):
            return "Video writer failed: \(message)"
        }
    }
}

struct Arguments {
    let windowID: CGWindowID
    let outputURL: URL
    let fps: Int
    let includeCursor: Bool

    static func parse() throws -> Arguments {
        let args = CommandLine.arguments
        var windowID: CGWindowID?
        var outputPath: String?
        var fps = 30
        var includeCursor = false

        var index = 1

        while index < args.count {
            switch args[index] {
            case "--window-id":
                index += 1

                guard
                    index < args.count,
                    let value = UInt32(args[index])
                else {
                    throw RecorderError.invalidArguments(
                        "--window-id requires an integer"
                    )
                }

                windowID = value

            case "--output":
                index += 1

                guard index < args.count else {
                    throw RecorderError.invalidArguments(
                        "--output requires a file path"
                    )
                }

                outputPath = args[index]

            case "--fps":
                index += 1

                guard
                    index < args.count,
                    let value = Int(args[index]),
                    value > 0
                else {
                    throw RecorderError.invalidArguments(
                        "--fps requires a positive integer"
                    )
                }

                fps = value

            case "--include-cursor":
                includeCursor = true

            default:
                throw RecorderError.invalidArguments(
                    "Unknown argument: \(args[index])"
                )
            }

            index += 1
        }

        guard let windowID else {
            throw RecorderError.invalidArguments(
                "Missing --window-id"
            )
        }

        guard let outputPath else {
            throw RecorderError.invalidArguments(
                "Missing --output"
            )
        }

        return Arguments(
            windowID: windowID,
            outputURL: URL(fileURLWithPath: outputPath),
            fps: fps,
            includeCursor: includeCursor
        )
    }
}

final class StreamOutput: NSObject, SCStreamOutput {
    private let writer: AVAssetWriter
    private let writerInput: AVAssetWriterInput
    private let pixelBufferAdaptor: AVAssetWriterInputPixelBufferAdaptor

    private var started = false
    private var firstTimestamp: CMTime?
    private var appendedFrameCount = 0
    private var droppedFrameCount = 0

    private let lock = NSLock()

    init(
        outputURL: URL,
        width: Int,
        height: Int,
        fps: Int
    ) throws {
        try? FileManager.default.removeItem(at: outputURL)

        writer = try AVAssetWriter(
            outputURL: outputURL,
            fileType: .mp4
        )

        let compressionProperties: [String: Any] = [
            AVVideoAverageBitRateKey: 8_000_000,
            AVVideoExpectedSourceFrameRateKey: fps,
            AVVideoMaxKeyFrameIntervalKey: fps * 2,
            AVVideoProfileLevelKey:
                AVVideoProfileLevelH264HighAutoLevel
        ]

        let outputSettings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: compressionProperties
        ]

        writerInput = AVAssetWriterInput(
            mediaType: .video,
            outputSettings: outputSettings
        )

        writerInput.expectsMediaDataInRealTime = true

        guard writer.canAdd(writerInput) else {
            throw RecorderError.cannotAddWriterInput
        }

        writer.add(writerInput)

        let pixelBufferAttributes: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String:
                kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String:
                width,
            kCVPixelBufferHeightKey as String:
                height,
            kCVPixelBufferIOSurfacePropertiesKey as String:
                [:]
        ]

        pixelBufferAdaptor = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: writerInput,
            sourcePixelBufferAttributes: pixelBufferAttributes
        )
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .screen else {
            return
        }

        guard CMSampleBufferIsValid(sampleBuffer) else {
            logMessage("Dropping invalid sample buffer")
            return
        }

        guard CMSampleBufferDataIsReady(sampleBuffer) else {
            logMessage("Dropping sample buffer whose data is not ready")
            return
        }

        guard frameIsComplete(sampleBuffer) else {
            return
        }

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            logMessage("Screen frame did not contain a CVPixelBuffer")
            return
        }

        let pixelWidth = CVPixelBufferGetWidth(pixelBuffer)
        let pixelHeight = CVPixelBufferGetHeight(pixelBuffer)
        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)

        let timestamp = CMSampleBufferGetPresentationTimeStamp(
            sampleBuffer
        )

        lock.lock()
        defer { lock.unlock() }

        if !started {
            guard writer.startWriting() else {
                logWriterFailure(prefix: "startWriting failed")
                return
            }

            writer.startSession(atSourceTime: timestamp)

            firstTimestamp = timestamp
            started = true

            logMessage(
                "CAPTURE_STARTED " +
                "width=\(pixelWidth) " +
                "height=\(pixelHeight) " +
                "pixelFormat=\(fourCC(pixelFormat))"
            )
        }

        guard writer.status == .writing else {
            logWriterFailure(
                prefix: "Writer is no longer writing"
            )
            return
        }

        guard writerInput.isReadyForMoreMediaData else {
            droppedFrameCount += 1

            if droppedFrameCount == 1 ||
                droppedFrameCount.isMultiple(of: 100) {
                logMessage(
                    "Writer input not ready; " +
                    "droppedFrames=\(droppedFrameCount)"
                )
            }

            return
        }

        let appended = pixelBufferAdaptor.append(
            pixelBuffer,
            withPresentationTime: timestamp
        )

        if appended {
            appendedFrameCount += 1

            if appendedFrameCount == 1 ||
                appendedFrameCount.isMultiple(of: 300) {
                logMessage(
                    "Frames appended: \(appendedFrameCount)"
                )
            }
        } else {
            logWriterFailure(
                prefix: "Pixel-buffer append failed " +
                    "frame=\(appendedFrameCount + 1) " +
                    "buffer=\(pixelWidth)x\(pixelHeight) " +
                    "format=\(fourCC(pixelFormat))"
            )
        }
    }

    private func frameIsComplete(
        _ sampleBuffer: CMSampleBuffer
    ) -> Bool {
        guard
            let attachmentsArray =
                CMSampleBufferGetSampleAttachmentsArray(
                    sampleBuffer,
                    createIfNecessary: false
                ) as? [[SCStreamFrameInfo: Any]],
            let attachments = attachmentsArray.first,
            let rawStatus =
                attachments[SCStreamFrameInfo.status] as? Int,
            let status = SCFrameStatus(rawValue: rawStatus)
        else {
            logMessage(
                "Dropping frame with no ScreenCaptureKit status"
            )
            return false
        }

        guard status == .complete else {
            droppedFrameCount += 1

            if droppedFrameCount == 1 ||
                droppedFrameCount.isMultiple(of: 100) {
                logMessage(
                    "Dropping incomplete frame; " +
                    "status=\(status.rawValue) " +
                    "droppedFrames=\(droppedFrameCount)"
                )
            }

            return false
        }

        return true
    }

    private func logWriterFailure(prefix: String) {
        let statusDescription: String

        switch writer.status {
        case .unknown:
            statusDescription = "unknown"
        case .writing:
            statusDescription = "writing"
        case .completed:
            statusDescription = "completed"
        case .failed:
            statusDescription = "failed"
        case .cancelled:
            statusDescription = "cancelled"
        @unknown default:
            statusDescription = "unrecognized"
        }

        guard let error = writer.error as NSError? else {
            logMessage(
                "\(prefix); " +
                "writerStatus=\(statusDescription); " +
                "writerError=nil"
            )
            return
        }

        logMessage(
            "\(prefix); " +
            "writerStatus=\(statusDescription); " +
            "domain=\(error.domain); " +
            "code=\(error.code); " +
            "description=\(error.localizedDescription); " +
            "failureReason=\(error.localizedFailureReason ?? "nil"); " +
            "userInfo=\(error.userInfo)"
        )
    }

    private func logMessage(_ message: String) {
        fputs(message + "\n", stderr)
        fflush(stderr)
    }

    private func fourCC(_ value: OSType) -> String {
        let characters: [CChar] = [
            CChar((value >> 24) & 0xff),
            CChar((value >> 16) & 0xff),
            CChar((value >> 8) & 0xff),
            CChar(value & 0xff),
            0
        ]

        return String(cString: characters)
    }

    func finish() async throws {
        let (wasStarted, frameCount) = lock.withLock {
            (started, appendedFrameCount)
        }

        guard wasStarted else {
            throw RecorderError.writerFailed(
                "No complete ScreenCaptureKit frames were received"
            )
        }

        guard frameCount > 0 else {
            throw RecorderError.writerFailed(
                "Capture started, but no video frames were appended"
            )
        }

        writerInput.markAsFinished()

        await withCheckedContinuation { continuation in
            writer.finishWriting {
                continuation.resume()
            }
        }

        guard writer.status == .completed else {
            let error = writer.error as NSError?

            throw RecorderError.writerFailed(
                "finishWriting failed: " +
                "status=\(writer.status.rawValue), " +
                "domain=\(error?.domain ?? "nil"), " +
                "code=\(error?.code ?? 0), " +
                "description=\(error?.localizedDescription ?? "nil"), " +
                "userInfo=\(error?.userInfo ?? [:])"
            )
        }

        logMessage(
            "CAPTURE_FINALIZED frames=\(frameCount)"
        )
    }
}

final class SignalWaiter {
    private let signalQueue = DispatchQueue(
        label: "com.katnip.window-recorder.signals"
    )

    private var interruptSource: DispatchSourceSignal?
    private var terminateSource: DispatchSourceSignal?

    func wait() async {
        await withCheckedContinuation { continuation in
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)

            let stateLock = NSLock()
            var resumed = false

            let resumeOnce = {
                let shouldResume = stateLock.withLock {
                    if resumed {
                        return false
                    }

                    resumed = true
                    return true
                }

                if shouldResume {
                    continuation.resume()
                }
            }

            let interruptSource = DispatchSource.makeSignalSource(
                signal: SIGINT,
                queue: signalQueue
            )

            let terminateSource = DispatchSource.makeSignalSource(
                signal: SIGTERM,
                queue: signalQueue
            )

            interruptSource.setEventHandler {
                fputs("SIGINT_RECEIVED\n", stderr)
                fflush(stderr)
                resumeOnce()
            }

            terminateSource.setEventHandler {
                fputs("SIGTERM_RECEIVED\n", stderr)
                fflush(stderr)
                resumeOnce()
            }

            self.interruptSource = interruptSource
            self.terminateSource = terminateSource

            interruptSource.activate()
            terminateSource.activate()

            fputs("SIGNAL_HANDLERS_READY\n", stderr)
            fflush(stderr)
        }
    }

    func cancel() {
        interruptSource?.cancel()
        terminateSource?.cancel()

        interruptSource = nil
        terminateSource = nil
    }
}

@main
struct KatnipWindowRecorder {
    static func main() async {
        do {
            let arguments = try Arguments.parse()

            let shareableContent =
                try await SCShareableContent.excludingDesktopWindows(
                    true,
                    onScreenWindowsOnly: true
                )

            guard let window = shareableContent.windows.first(
                where: { $0.windowID == arguments.windowID }
            ) else {
                throw RecorderError.windowNotFound(arguments.windowID)
            }

            let scaleFactor = window.owningApplication == nil
                ? 2.0
                : NSScreen.main?.backingScaleFactor ?? 2.0

            var width = Int(window.frame.width * scaleFactor)
            var height = Int(window.frame.height * scaleFactor)

            width -= width % 2
            height -= height % 2

            let configuration = SCStreamConfiguration()
            configuration.width = max(width, 2)
            configuration.height = max(height, 2)
            configuration.minimumFrameInterval = CMTime(
                value: 1,
                timescale: CMTimeScale(arguments.fps)
            )
            configuration.queueDepth = 5
            configuration.showsCursor = arguments.includeCursor
            configuration.capturesAudio = false
            configuration.pixelFormat = kCVPixelFormatType_32BGRA

            let filter = SCContentFilter(
                desktopIndependentWindow: window
            )

            let output = try StreamOutput(
                outputURL: arguments.outputURL,
                width: configuration.width,
                height: configuration.height,
                fps: arguments.fps
            )

            let stream = SCStream(
                filter: filter,
                configuration: configuration,
                delegate: nil
            )

            let outputQueue = DispatchQueue(
                label: "com.katnip.window-recorder.video"
            )

            try stream.addStreamOutput(
                output,
                type: .screen,
                sampleHandlerQueue: outputQueue
            )

            try await stream.startCapture()

            let signalWaiter = SignalWaiter()

            print("RECORDER_WAITING_FOR_STOP")
            fflush(stdout)

            await signalWaiter.wait()

            print("STOP_SIGNAL_RECEIVED")
            fflush(stdout)

            try await stream.stopCapture()

            print("STREAM_CAPTURE_STOPPED")
            fflush(stdout)

            // Wait until all pending frame callbacks have completed.
            await withCheckedContinuation { continuation in
                outputQueue.async {
                    continuation.resume()
                }
            }

            print("OUTPUT_QUEUE_DRAINED")
            fflush(stdout)

            try await output.finish()

            print("CAPTURE_STOPPED")
            fflush(stdout)

            signalWaiter.cancel()

        } catch {
            fputs("Recorder error: \(error)\n", stderr)
            exit(1)
        }
    }
}