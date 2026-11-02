// Delayed picture of another app's window (ADR-0012).
//
// A2A cannot delay pixels another process draws, so it shows the picture itself:
// ScreenCaptureKit captures the chosen window, VideoToolbox hardware-encodes each frame
// into a bounded in-memory ring (raw frames would be gigabytes for a few seconds, and holding
// ScreenCaptureKit's own surfaces stalls its small pool), and an AVSampleBufferDisplayLayer
// decodes them on a control timebase that runs `delay` behind the mach host clock. The audio
// pipeline anchors capture to the same clock, so `a2a-pipeline --presentation-delay-ms` with
// the same delay puts each translated phrase on its own picture. Frames never leave memory.

import AVFoundation
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit
import VideoToolbox

public enum DelayedMirrorError: Error, CustomStringConvertible {
    case encoder(OSStatus)
    case timebase(OSStatus)
    case emptySource

    public var description: String {
        switch self {
        case .encoder(let status): "The video encoder could not start (\(status))."
        case .timebase(let status): "The picture clock could not start (\(status))."
        case .emptySource: "The chosen window has no visible size."
        }
    }
}

/// Compressed sample buffers are immutable once the encoder hands them over.
private struct EncodedFrame: @unchecked Sendable {
    let sample: CMSampleBuffer
}

public final class DelayedMirror: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    /// Shown by the host's window; created on the main thread by the host.
    public let layer: AVSampleBufferDisplayLayer
    public let delaySeconds: Double
    /// Called on an arbitrary queue when capture or decoding stops on its own.
    public var onFailure: (@Sendable (String) -> Void)?

    /// Thread-safe for enqueueing; taken from the layer once, on the main thread.
    private let renderer: AVSampleBufferVideoRenderer
    /// Everything below is owned by `queue`.
    private let queue = DispatchQueue(label: "dev.a2a.mirror", qos: .userInteractive)
    private var stream: SCStream?
    private var encoder: VTCompressionSession?
    private var timebase: CMTimebase?
    private var timer: DispatchSourceTimer?
    private var frames: DelayedFrameQueue<EncodedFrame>
    private var offsetSeconds: Double
    /// Added to every capture timestamp; nonzero only if ScreenCaptureKit stamps frames on a
    /// clock other than the host clock (checked on the first frame, logged once).
    private var stampCorrectionNs: Int64?
    private var stopped = false
    private var failed = false

    /// Decode this far ahead of display so a frame is ready when its time comes.
    private static let lookaheadSeconds = 0.25

    @MainActor public init(delaySeconds: Double, offsetSeconds: Double = 0, maxBytes: Int = 256 << 20) {
        self.delaySeconds = delaySeconds
        self.offsetSeconds = offsetSeconds
        self.frames = DelayedFrameQueue(maxBytes: maxBytes)
        let layer = AVSampleBufferDisplayLayer()
        self.layer = layer
        self.renderer = layer.sampleBufferRenderer
        super.init()
        layer.videoGravity = .resizeAspect
        layer.backgroundColor = CGColor(gray: 0, alpha: 1)
    }

    /// Starts capturing `filter` (from the system content picker). The picture appears once
    /// the first captured frame reaches its delayed time.
    @MainActor public func start(filter: SCContentFilter) async throws {
        let rect = filter.contentRect
        let scale = CGFloat(filter.pointPixelScale)
        guard rect.width >= 2, rect.height >= 2 else { throw DelayedMirrorError.emptySource }
        // Even dimensions for 4:2:0; at most 3840 wide, which the hardware encoder sustains at 60 fps.
        var width = rect.width * scale, height = rect.height * scale
        if width > 3840 { height *= 3840 / width; width = 3840 }
        let w = Int(width) & ~1, h = Int(height) & ~1

        let config = SCStreamConfiguration()
        config.width = w
        config.height = h
        config.minimumFrameInterval = CMTime(value: 1, timescale: 60)
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
        config.showsCursor = false
        config.capturesAudio = false
        config.queueDepth = 6
        config.scalesToFit = true
        config.preservesAspectRatio = true

        let timebase = try queue.sync {
            try makeEncoder(width: w, height: h)
            return try makeTimebase()
        }
        layer.controlTimebase = timebase
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            queue.sync { self.stream = stream }
            try await stream.startCapture()
        } catch {
            await stop()
            throw error
        }
        queue.sync { startPresenting() }
    }

    /// Fine adjustment after measuring a flash-and-click clip; positive shows the picture later.
    public func setOffset(seconds: Double) {
        queue.async { [self] in
            offsetSeconds = seconds
            retime()
        }
    }

    @MainActor public func stop() async {
        let stream: SCStream? = queue.sync {
            stopped = true
            timer?.cancel()
            timer = nil
            frames.removeAll()
            if let encoder {
                VTCompressionSessionInvalidate(encoder)
                self.encoder = nil
            }
            defer { self.stream = nil }
            return self.stream
        }
        try? await stream?.stopCapture()
        renderer.flush(removingDisplayedImage: true, completionHandler: nil)
    }

    /// Picture currently held, for the host's status line.
    public var bufferedSeconds: Double { queue.sync { frames.spanSeconds } }

    // MARK: Capture → encode (queue)

    public func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, !stopped, let encoder, sampleBuffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete,
              let image = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        // Idle frames (nothing changed) carry no image; the last picture simply stays up.
        let pts = hostStamp(sampleBuffer.presentationTimeStamp)
        VTCompressionSessionEncodeFrame(encoder, imageBuffer: image, presentationTimeStamp: pts,
                                        duration: .invalid, frameProperties: nil, infoFlagsOut: nil) { [weak self] status, _, sample in
            guard let self, status == noErr, let sample else { return }
            let frame = EncodedFrame(sample: sample)
            self.queue.async { self.admit(frame) }
        }
    }

    public func stream(_ stream: SCStream, didStopWithError error: any Error) {
        queue.async { [self] in fail("Picture capture stopped: \(error.localizedDescription)") }
    }

    private func admit(_ frame: EncodedFrame) {
        guard !stopped else { return }
        let sample = frame.sample
        let pts = sample.presentationTimeStamp
        guard pts.isValid else { return }
        frames.push(DelayedFrame(presentationNs: UInt64(max(0, pts.seconds) * 1e9), bytes: sample.totalSampleSize,
                                 isKeyframe: Self.isKeyframe(sample), payload: frame))
    }

    /// A sync sample: no NotSync attachment. Group-of-pictures eviction depends on this.
    static func isKeyframe(_ sample: CMSampleBuffer) -> Bool {
        guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[CFString: Any]],
              let first = attachments.first else { return true }
        return !(first[kCMSampleAttachmentKey_NotSync] as? Bool ?? false)
    }

    /// ScreenCaptureKit stamps frames on the host clock. If a frame ever arrives more than a
    /// second away from it, re-stamp on arrival so picture and audio stay on one clock.
    private func hostStamp(_ pts: CMTime) -> CMTime {
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        if stampCorrectionNs == nil {
            let skew = pts.isValid ? (now.seconds - pts.seconds) : .infinity
            stampCorrectionNs = abs(skew) <= 1 ? 0 : Int64(((pts.isValid ? skew : 0) * 1e9).rounded())
            if stampCorrectionNs != 0 { NSLog("A2A mirror: capture clock differs from host time; re-stamping frames") }
        }
        guard pts.isValid else { return now }
        guard let correction = stampCorrectionNs, correction != 0 else { return pts }
        return CMTimeAdd(pts, CMTime(value: correction, timescale: 1_000_000_000))
    }

    // MARK: Present (queue)

    private func startPresenting() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(5), leeway: .milliseconds(1))
        timer.setEventHandler { [weak self] in self?.feed() }
        timer.resume()
        self.timer = timer
    }

    /// Hands the renderer every frame due within the lookahead, while it accepts more.
    private func feed() {
        guard !stopped, !failed, let timebase else { return }
        if renderer.status == .failed {
            fail("The delayed picture could not be decoded: \(renderer.error?.localizedDescription ?? "unknown error")")
            return
        }
        let shown = CMTimebaseGetTime(timebase)
        let through = UInt64(max(0, shown.seconds + Self.lookaheadSeconds) * 1e9)
        while renderer.isReadyForMoreMediaData, let frame = frames.peekDue(throughNs: through) {
            frames.popFirst()
            renderer.enqueue(frame.payload.sample)
        }
    }

    private func retime() {
        guard let timebase else { return }
        let now = CMClockGetTime(CMClockGetHostTimeClock())
        let behind = CMTime(seconds: max(0, delaySeconds + offsetSeconds), preferredTimescale: 1_000_000_000)
        CMTimebaseSetTime(timebase, time: CMTimeSubtract(now, behind))
    }

    private func fail(_ message: String) {
        guard !failed, !stopped else { return }
        failed = true
        timer?.cancel()
        timer = nil
        onFailure?(message)
    }

    // MARK: Setup (queue)

    private func makeEncoder(width: Int, height: Int) throws {
        encoder = try Self.compressionSession(width: width, height: height)
    }

    static func compressionSession(width: Int, height: Int) throws -> VTCompressionSession {
        var session: VTCompressionSession?
        let spec = [kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: kCFBooleanTrue] as CFDictionary
        let status = VTCompressionSessionCreate(allocator: nil, width: Int32(width), height: Int32(height),
                                                codecType: kCMVideoCodecType_HEVC, encoderSpecification: spec,
                                                imageBufferAttributes: nil, compressedDataAllocator: nil,
                                                outputCallback: nil, refcon: nil, compressionSessionOut: &session)
        guard status == noErr, let session else { throw DelayedMirrorError.encoder(status) }
        // Delay hides encode latency, so quality matters more than speed, but frames must keep
        // up in real time. No reordering keeps decode order equal to presentation order, and a
        // keyframe every second bounds what a budget eviction or a late start loses.
        let bitsPerSecond = min(40_000_000, max(4_000_000, width * height * 60 / 10))
        let properties: [CFString: CFTypeRef] = [
            kVTCompressionPropertyKey_RealTime: kCFBooleanTrue,
            kVTCompressionPropertyKey_AllowFrameReordering: kCFBooleanFalse,
            kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration: 1.0 as CFNumber,
            kVTCompressionPropertyKey_ExpectedFrameRate: 60 as CFNumber,
            kVTCompressionPropertyKey_AverageBitRate: bitsPerSecond as CFNumber,
            kVTCompressionPropertyKey_ColorPrimaries: kCVImageBufferColorPrimaries_ITU_R_709_2,
            kVTCompressionPropertyKey_TransferFunction: kCVImageBufferTransferFunction_ITU_R_709_2,
            kVTCompressionPropertyKey_YCbCrMatrix: kCVImageBufferYCbCrMatrix_ITU_R_709_2,
        ]
        for (key, value) in properties {
            let s = VTSessionSetProperty(session, key: key, value: value)
            guard s == noErr else { VTCompressionSessionInvalidate(session); throw DelayedMirrorError.encoder(s) }
        }
        VTCompressionSessionPrepareToEncodeFrames(session)
        return session
    }

    private func makeTimebase() throws -> CMTimebase {
        var timebase: CMTimebase?
        let status = CMTimebaseCreateWithSourceClock(allocator: kCFAllocatorDefault,
                                                     sourceClock: CMClockGetHostTimeClock(), timebaseOut: &timebase)
        guard status == noErr, let timebase else { throw DelayedMirrorError.timebase(status) }
        self.timebase = timebase
        retime()
        CMTimebaseSetRate(timebase, rate: 1)
        return timebase
    }
}
