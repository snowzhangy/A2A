import A2ARTShim
import CoreAudio
import Foundation
import Testing

@Suite struct AudioBoundaryTests {
    @Test func unrepresentableBufferSizesAreRejected() {
        #expect(a2a_ring_create(.max, 1) == nil)
        #expect(a2a_record_ring_create(.max) == nil)
        var config = a2a_engine_config(channels: 1, capture_ring_frames: 64, max_delay_frames: .max,
                                       translated_ring_frames: 64, max_block_frames: 32, record_capacity: 64,
                                       rendered_ring_frames: 0, fade_frames: 0)
        #expect(a2a_engine_create(&config) == nil)
    }

    @Test(arguments: [false, true])
    func shortResumptionKeepsTheExistingFadeTail(partialBlock: Bool) throws {
        var config = a2a_engine_config(channels: 1, capture_ring_frames: 256, max_delay_frames: 0,
                                       translated_ring_frames: 256, max_block_frames: 64, record_capacity: 64,
                                       rendered_ring_frames: 0, fade_frames: 32)
        let engine = try #require(a2a_engine_create(&config))
        defer { a2a_engine_destroy(engine) }
        a2a_engine_set_render_output(engine, true)
        a2a_engine_set_gains(engine, 0, 1, 0)
        func render(_ count: Int) -> [Float] {
            var out = [Float](repeating: 0, count: count)
            a2a_engine_process(engine, nil, 0, nil, &out, UInt32(count), nil)
            return out
        }
        var signal = [Float](repeating: 0.6, count: 64)
        _ = a2a_ring_write(a2a_engine_translated_ring(engine), &signal, 64)
        var output = render(64)
        output += render(4) // the 32-frame dry-bus tail is still sounding
        _ = a2a_ring_write(a2a_engine_translated_ring(engine), &signal, 1)
        output += render(partialBlock ? 16 : 1)
        output += render(64)
        let step = zip(output, output.dropFirst()).map { abs($0 - $1) }.max() ?? 0
        #expect(step < 0.04)
        #expect(output.allSatisfy { $0.isFinite && $0 >= 0 && $0 <= 0.601 })
        #expect(output.suffix(16).allSatisfy { $0 == 0 })
    }

    @Test(arguments: [false, true])
    func oversizedIOCycleSilencesEveryOutputBuffer(oversizedInput: Bool) throws {
        var config = a2a_engine_config(channels: 2, capture_ring_frames: 256, max_delay_frames: 0,
                                       translated_ring_frames: 256, max_block_frames: 32, record_capacity: 64,
                                       rendered_ring_frames: 0, fade_frames: 0)
        let engine = try #require(a2a_engine_create(&config))
        defer { a2a_engine_destroy(engine) }
        a2a_engine_set_render_output(engine, true)
        var inputData = [Float](repeating: 0.4, count: (oversizedInput ? 64 : 16) * 2)
        var left = [Float](repeating: 7, count: oversizedInput ? 16 : 64)
        var right = left
        let input = AudioBufferList.allocate(maximumBuffers: 1)
        let output = AudioBufferList.allocate(maximumBuffers: 2)
        defer { free(input.unsafeMutablePointer); free(output.unsafeMutablePointer) }
        inputData.withUnsafeMutableBytes { source in
            left.withUnsafeMutableBytes { l in
                right.withUnsafeMutableBytes { r in
                    input[0] = AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(source.count), mData: source.baseAddress)
                    output[0] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(l.count), mData: l.baseAddress)
                    output[1] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(r.count), mData: r.baseAddress)
                    var timestamp = AudioTimeStamp()
                    _ = a2a_engine_ioproc(0, &timestamp, input.unsafePointer, &timestamp,
                                          output.unsafeMutablePointer, &timestamp, UnsafeMutableRawPointer(engine))
                }
            }
        }
        #expect(left.allSatisfy { $0 == 0 } && right.allSatisfy { $0 == 0 })
        #expect(a2a_engine_rejected_blocks(engine) == 1)
        #expect(a2a_engine_callbacks(engine) == 1)
        #expect(a2a_engine_captured_frames(engine) == 0)
    }
}
