// The delayed picture's hardware encoder with its exact settings (ADR-0012). Opt-in, like the
// Apple runtime tests: virtualized CI Macs may have no hardware HEVC encoder.
//   A2A_RUN_APPLE_TESTS=1 swift test --package-path apple --filter EncoderConfigurationTests

@testable import A2AVideo
import CoreMedia
import CoreVideo
import Foundation
import Synchronization
import Testing
import VideoToolbox

private let enabled = ProcessInfo.processInfo.environment["A2A_RUN_APPLE_TESTS"] == "1"

private func pixelBuffer(width: Int, height: Int, luma: UInt8) -> CVPixelBuffer {
    var buffer: CVPixelBuffer?
    let attributes = [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
    CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, attributes, &buffer)
    let b = buffer!
    CVPixelBufferLockBaseAddress(b, [])
    for plane in 0..<CVPixelBufferGetPlaneCount(b) {
        memset(CVPixelBufferGetBaseAddressOfPlane(b, plane), plane == 0 ? Int32(luma) : 128,
               CVPixelBufferGetBytesPerRowOfPlane(b, plane) * CVPixelBufferGetHeightOfPlane(b, plane))
    }
    CVPixelBufferUnlockBaseAddress(b, [])
    return b
}

@Suite(.enabled(if: enabled, "set A2A_RUN_APPLE_TESTS=1")) struct EncoderConfigurationTests {
    @Test(arguments: [(1920, 1080), (3840, 2160)])
    func encodesInPresentationOrderWithOneKeyframePerSecond(size: (Int, Int)) throws {
        let session = try DelayedMirror.compressionSession(width: size.0, height: size.1)
        defer { VTCompressionSessionInvalidate(session) }
        let out = Mutex<[(status: OSStatus, pts: CMTime, key: Bool)]>([])
        let base = CMClockGetTime(CMClockGetHostTimeClock())
        let frames = 75   // 1.25 s at 60 fps: a second keyframe must appear
        for i in 0..<frames {
            let pts = CMTimeAdd(base, CMTime(value: CMTimeValue(i), timescale: 60))
            let status = VTCompressionSessionEncodeFrame(
                session, imageBuffer: pixelBuffer(width: size.0, height: size.1, luma: UInt8(16 + i % 200)),
                presentationTimeStamp: pts, duration: .invalid, frameProperties: nil, infoFlagsOut: nil
            ) { status, _, sample in
                out.withLock { $0.append((status, sample?.presentationTimeStamp ?? .invalid,
                                          sample.map(DelayedMirror.isKeyframe) ?? false)) }
            }
            #expect(status == noErr)
        }
        #expect(VTCompressionSessionCompleteFrames(session, untilPresentationTimeStamp: .invalid) == noErr)
        let results = out.withLock { $0 }
        #expect(results.count == frames)
        #expect(results.allSatisfy { $0.status == noErr })
        // No reordering: output order and timestamps equal the host-clock input.
        for (i, r) in results.enumerated() {
            #expect(r.pts == CMTimeAdd(base, CMTime(value: CMTimeValue(i), timescale: 60)))
        }
        #expect(results.first?.key == true)
        let keyframes = results.filter(\.key).count
        #expect(keyframes >= 2 && keyframes < frames / 4)
    }
}
