import A2ARTShim
import Foundation
import Testing

@Suite struct RingTests {
    @Test func wrapsAroundAndCountsOverflow() throws {
        let ring = try #require(a2a_ring_create(5, 2))   // rounds up to 8 frames
        defer { a2a_ring_destroy(ring) }
        #expect(a2a_ring_capacity(ring) == 8)
        var input = (0..<12).map { Float($0) }            // 6 stereo frames
        #expect(a2a_ring_write(ring, &input, 6) == 6)
        var out = [Float](repeating: -1, count: 8)
        #expect(a2a_ring_read(ring, &out, 4) == 4)
        #expect(out == [0, 1, 2, 3, 4, 5, 6, 7])
        var more = (100..<116).map { Float($0) }         // 8 frames, only 6 fit
        #expect(a2a_ring_write(ring, &more, 8) == 6)
        #expect(a2a_ring_overflow_frames(ring) == 2)
        var all = [Float](repeating: 0, count: 16)
        #expect(a2a_ring_read(ring, &all, 8) == 8)
        #expect(Array(all[0..<4]) == [8, 9, 10, 11])
        #expect(Array(all[4..<16]) == (100..<112).map { Float($0) })
        #expect(a2a_ring_total_written(ring) == 12)
        #expect(a2a_ring_total_read(ring) == 12)
    }

    /// Producer and consumer on separate threads; every frame arrives once, in order.
    @Test func concurrentProducerConsumerPreservesSequence() async throws {
        let ring = try #require(a2a_ring_create(1_024, 1))
        defer { a2a_ring_destroy(ring) }
        let total = 400_000
        let ringBits = UInt(bitPattern: ring)
        let producer = Thread {
            let r = OpaquePointer(bitPattern: ringBits)!
            var next = 0
            var block = [Float](repeating: 0, count: 256)
            while next < total {
                let n = min(256, total - next, Int(a2a_ring_writable(r)))
                if n == 0 { continue }
                for i in 0..<n { block[i] = Float(next + i) }
                next += Int(a2a_ring_write(r, &block, UInt32(n)))
            }
        }
        producer.start()
        var expected = 0
        var buf = [Float](repeating: 0, count: 300)
        var mismatches = 0
        let deadline = Date().addingTimeInterval(20)
        while expected < total, Date() < deadline {
            let n = Int(a2a_ring_read(ring, &buf, 300))
            for i in 0..<n where buf[i] != Float(expected + i) { mismatches += 1 }
            expected += n
        }
        #expect(expected == total)
        #expect(mismatches == 0)
        #expect(a2a_ring_overflow_frames(ring) == 0)
    }
}

/// `fade: 0` keeps hard translated-bus edges, so exact-sample tests see the mixer alone.
private func makeEngine(channels: UInt32 = 1, maxBlock: UInt32 = 512, maxDelay: UInt32 = 4_800,
                        fade: UInt32 = 0) -> OpaquePointer? {
    var cfg = a2a_engine_config(channels: channels, capture_ring_frames: 8_192, max_delay_frames: maxDelay,
                                translated_ring_frames: 8_192, max_block_frames: maxBlock, record_capacity: 1_024,
                                rendered_ring_frames: 8_192, fade_frames: fade)
    return a2a_engine_create(&cfg)
}

/// Renders `blocks` output blocks of `frames` with no input and returns the concatenated output.
private func render(_ e: OpaquePointer, blocks: Int, frames: Int = 64, channels: Int = 1) -> [Float] {
    var out = [Float](repeating: 0, count: frames * channels)
    var all: [Float] = []
    for _ in 0..<blocks {
        a2a_engine_process(e, nil, 0, nil, &out, UInt32(frames), nil)
        all += out
    }
    return all
}

private func maxStep(_ x: [Float]) -> Float {
    zip(x.dropFirst(), x).map { abs($0 - $1) }.max() ?? 0
}

private func timestamp(_ host: UInt64) -> AudioTimeStamp {
    var ts = AudioTimeStamp()
    ts.mHostTime = host
    ts.mFlags = [.hostTimeValid]
    return ts
}

@Suite struct EngineTests {
    @Test func independentBackgroundSurvivesTranslationFlushAndNeverUsesOriginalAudio() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        #expect(a2a_engine_enable_background(e, 512))
        let bed = try #require(a2a_engine_background_ring(e))
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        a2a_engine_set_background_gain(e, 0.5, 0)
        var music = [Float](repeating: 0.2, count: 128)
        var speech = [Float](repeating: 0.4, count: 64)
        _ = a2a_ring_write(bed, &music, 128)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &speech, 64)
        #expect(render(e, blocks: 1).allSatisfy { abs($0 - 0.5) < 0.0001 })
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &speech, 64)
        a2a_engine_flush_translated(e)
        #expect(render(e, blocks: 1).allSatisfy { abs($0 - 0.1) < 0.0001 })
        a2a_engine_set_background_gain(e, 0, 0)
        _ = a2a_ring_write(bed, &music, 64)
        #expect(render(e, blocks: 1).allSatisfy { $0 == 0 })
        #expect(!a2a_engine_enable_background(e, 512))
    }

    @Test func backgroundEdgesAndGainChangesHaveBoundedSteps() throws {
        let e = try #require(makeEngine(fade: 240))
        defer { a2a_engine_destroy(e) }
        #expect(a2a_engine_enable_background(e, 2048))
        let bed = try #require(a2a_engine_background_ring(e))
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 0, 0)
        a2a_engine_set_background_gain(e, 0.8, 240)
        var music = [Float](repeating: 0.7, count: 512)
        _ = a2a_ring_write(bed, &music, 512)
        let first = render(e, blocks: 12)
        #expect(maxStep(first) < 0.01)
        #expect(abs(first.last ?? 1) < 0.001)
        _ = a2a_ring_write(bed, &music, 512)
        let resumed = render(e, blocks: 4)
        #expect(maxStep(resumed) < 0.01)
        a2a_engine_set_background_gain(e, 0, 240)
        #expect(maxStep(render(e, blocks: 4)) < 0.01)
        #expect(a2a_engine_current_background_gain(e) == 0)
    }

    @Test func capturesIntoRingAndRecordsTiming() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        var input = [Float](repeating: 0.5, count: 256)
        var ts = timestamp(1_000)
        a2a_engine_process(e, &input, 256, &ts, nil, 0, nil)
        #expect(a2a_ring_readable(a2a_engine_capture_ring(e)) == 256)
        var rec = [a2a_rt_record](repeating: a2a_rt_record(), count: 4)
        let n = a2a_record_ring_pop(a2a_engine_records(e), &rec, 4)
        #expect(n == 1)
        #expect(rec[0].kind == UInt32(A2A_REC_CAPTURE.rawValue))
        #expect(rec[0].frames == 256)
        #expect(rec[0].host_ns > 0)
    }

    @Test func invalidHostTimeIsReportedNotFabricated() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        var input = [Float](repeating: 0, count: 64)
        var ts = AudioTimeStamp()   // no valid flags
        a2a_engine_process(e, &input, 64, &ts, nil, 0, nil)
        var rec = [a2a_rt_record](repeating: a2a_rt_record(), count: 4)
        let n = a2a_record_ring_pop(a2a_engine_records(e), &rec, 4)
        #expect(n == 2)
        #expect(rec[0].host_ns == 0)
        #expect(rec[1].kind == UInt32(A2A_REC_TIMESTAMP_INVALID.rawValue))
    }

    @Test func rendersCapturedOriginalWithConfiguredDelay() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_delay_frames(e, 128)
        var out = [Float](repeating: 0, count: 64)
        var produced: [Float] = []
        var next: Float = 1
        for _ in 0..<8 {
            var block = (0..<64).map { i in next + Float(i) }
            next += 64
            a2a_engine_process(e, &block, 64, nil, &out, 64, nil)
            produced += out
        }
        // The first 128 frames are silence, then the input sequence resumes from 1.
        #expect(produced[0..<128].allSatisfy { $0 == 0 })
        #expect(Array(produced[128..<136]) == (1...8).map { Float($0) })
        #expect(produced.last == Float(512 - 128))
    }

    @Test func captureOnlyModeOutputsSilence() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        var block = [Float](repeating: 0.7, count: 64)
        var out = [Float](repeating: 9, count: 64)
        a2a_engine_process(e, &block, 64, nil, &out, 64, nil)
        #expect(out.allSatisfy { $0 == 0 })
    }

    /// A crossfade between buses must move gains monotonically with bounded per-sample steps.
    @Test func gainCrossfadeIsSmoothAndLandsOnTarget() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        var ones = [Float](repeating: 1, count: 4_800)
        // Translated bus carries a constant -1 so the output reveals both gains.
        var minus = [Float](repeating: -1, count: 4_800)
        #expect(a2a_ring_write(a2a_engine_translated_ring(e), &minus, 4_800) == 4_800)
        a2a_engine_set_gains(e, 0, 1, 1_440)   // 30 ms at 48 kHz
        var out = [Float](repeating: 0, count: 480)
        var series: [Float] = []
        for _ in 0..<5 {
            a2a_engine_process(e, &ones, 480, nil, &out, 480, nil)
            series += out
        }
        let steps = zip(series.dropFirst(), series).map { abs($0 - $1) }
        #expect((steps.max() ?? 1) < 0.002)                 // ≈ 2/1440 per sample
        #expect(abs(series.last! - -1) < 1e-6)
        #expect(a2a_engine_current_original_gain(e) == 0)
        #expect(a2a_engine_current_translated_gain(e) == 1)
    }

    @Test func flushDiscardsQueuedTranslatedAudio() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        var pcm = [Float](repeating: 0.25, count: 2_000)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 2_000)
        a2a_engine_flush_translated(e)
        var out = [Float](repeating: 0, count: 64)
        a2a_engine_process(e, nil, 0, nil, &out, 64, nil)
        #expect(a2a_ring_readable(a2a_engine_translated_ring(e)) == 0)
        #expect(a2a_engine_translated_consumed(e) == 2_000)
    }

    @Test func starvationIsCountedOnlyWhileTranslatedBusIsActive() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        var out = [Float](repeating: 0, count: 64)
        a2a_engine_process(e, nil, 0, nil, &out, 64, nil)
        #expect(a2a_engine_translated_starved_frames(e) == 0)
        a2a_engine_set_gains(e, 0, 1, 0)
        var pcm = [Float](repeating: 0.1, count: 32)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 32)
        a2a_engine_process(e, nil, 0, nil, &out, 64, nil)
        #expect(a2a_engine_translated_starved_frames(e) == 32)
    }

    @Test func oversizedBlocksAreRejectedWithSilence() throws {
        let e = try #require(makeEngine(maxBlock: 128))
        defer { a2a_engine_destroy(e) }
        var input = [Float](repeating: 1, count: 256)
        var out = [Float](repeating: 3, count: 256)
        a2a_engine_process(e, &input, 256, nil, &out, 256, nil)
        #expect(a2a_engine_rejected_blocks(e) == 1)
        #expect(out.allSatisfy { $0 == 0 })
    }

    @Test func renderedMonitorCapturesActualGainsAndSilenceAndExcludesFlushedPCM() throws {
        let e = try #require(makeEngine())
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        var source = [Float](repeating: 0.8, count: 64)
        var translated = [Float](repeating: -0.4, count: 64)
        var out = [Float](repeating: 0, count: 64)
        let monitor = try #require(a2a_engine_rendered_ring(e))
        a2a_engine_set_gains(e, 0, 0.5, 0)
        a2a_engine_process(e, &source, 64, nil, &out, 64, nil)
        #expect(a2a_ring_readable(monitor) == 0) // opt-in only
        a2a_engine_set_capture_rendered(e, true)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &translated, 64)
        a2a_engine_process(e, &source, 64, nil, &out, 64, nil)
        var monitored = [Float](repeating: 9, count: 64)
        #expect(a2a_ring_read(monitor, &monitored, 64) == 64)
        #expect(monitored == out)
        #expect(monitored.allSatisfy { abs($0 + 0.2) < 1e-6 })
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &translated, 64)
        a2a_engine_flush_translated(e)
        a2a_engine_set_gains(e, 0, 0, 0)
        a2a_engine_process(e, &source, 64, nil, &out, 64, nil)
        #expect(a2a_ring_read(monitor, &monitored, 64) == 64)
        #expect(monitored.allSatisfy { $0 == 0 })
    }

    @Test func monitorRingIsOptional() throws {
        var cfg = a2a_engine_config(channels: 2, capture_ring_frames: 1_024, max_delay_frames: 0,
                                    translated_ring_frames: 1_024, max_block_frames: 512, record_capacity: 64,
                                    rendered_ring_frames: 0, fade_frames: 0)
        let e = try #require(a2a_engine_create(&cfg))
        defer { a2a_engine_destroy(e) }
        #expect(a2a_engine_rendered_ring(e) == nil)
        a2a_engine_set_capture_rendered(e, true)
        a2a_engine_set_render_output(e, true)
        _ = render(e, blocks: 2, channels: 2)   // must not touch a missing ring
    }

    /// The translated ring is mono; every output channel carries the same sample.
    @Test func monoTranslatedBusFeedsEveryChannel() throws {
        let e = try #require(makeEngine(channels: 2))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        var pcm = (0..<64).map { Float($0) / 100 }
        #expect(a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 64) == 64)
        let out = render(e, blocks: 1, channels: 2)
        let left = stride(from: 0, to: out.count, by: 2).map { out[$0] }
        let right = stride(from: 1, to: out.count, by: 2).map { out[$0] }
        #expect(left == pcm)
        #expect(right == pcm)
    }

    /// A flush keeps playing nothing it discarded except a short fade-out of the next samples,
    /// so a fallback mid-word never steps from full level to silence.
    @Test func flushFadesOutInsteadOfCutting() throws {
        let fade: UInt32 = 48
        let e = try #require(makeEngine(fade: fade))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        var pcm = [Float](repeating: 0.5, count: 2_000)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 2_000)
        let before = render(e, blocks: 2)
        #expect(before.last == 0.5)
        a2a_engine_flush_translated(e)
        let after = render(e, blocks: 2)
        #expect(a2a_ring_readable(a2a_engine_translated_ring(e)) == 0)
        #expect(a2a_engine_translated_consumed(e) == 2_000)
        #expect(after[0] > 0.45 && after[Int(fade)...].allSatisfy { $0 == 0 })
        #expect(maxStep(before + after) < 0.04)   // ≈ π/2 · 0.5 / 48 at the steepest point
    }

    /// Audio queued after the flush call is the next unit: it must survive.
    @Test func flushDiscardsOnlyAudioQueuedBeforeTheCall() throws {
        let e = try #require(makeEngine(fade: 32))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        var stale = [Float](repeating: 0.25, count: 1_000)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &stale, 1_000)
        a2a_engine_flush_translated(e)
        var fresh = [Float](repeating: 0.75, count: 100)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &fresh, 100)
        let out = render(e, blocks: 4)
        #expect(a2a_engine_translated_consumed(e) == 1_100)
        #expect(out.contains(0.75))
        #expect(!out.contains(0.25))
    }

    /// A bus that runs dry mid-signal ramps to zero; the audio that resumes fades back in.
    @Test func dryBusRampsToZeroAndResumedAudioFadesIn() throws {
        let fade = 32
        let e = try #require(makeEngine(fade: UInt32(fade)))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        var pcm = [Float](repeating: 0.6, count: 100)
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 100)
        let first = render(e, blocks: 3)            // 100 delivered, then dry
        #expect(first[0] < 0.05)                    // the first audio fades in
        #expect(first[fade..<100].allSatisfy { $0 == 0.6 })
        #expect(first[100] > 0.55 && first[(100 + fade)...].allSatisfy { $0 == 0 })
        _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, 100)
        let resumed = render(e, blocks: 1)
        #expect(resumed[0] < 0.05 && resumed[fade..<64].allSatisfy { $0 == 0.6 })
        #expect(maxStep(first + resumed) < 0.06)
        // Starvation accounting is unchanged by the ramp.
        #expect(a2a_engine_translated_starved_frames(e) == 92)
    }

    /// Fades are bounded state: long silences and many edges never accumulate or overflow.
    @Test func edgeSmoothingSurvivesRepeatedDrySpellsAndFlushes() throws {
        let e = try #require(makeEngine(fade: 64))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_render_output(e, true)
        a2a_engine_set_gains(e, 0, 1, 0)
        var out: [Float] = []
        for i in 0..<50 {
            var pcm = (0..<(37 + i)).map { _ in Float(0.3) }
            _ = a2a_ring_write(a2a_engine_translated_ring(e), &pcm, UInt32(pcm.count))
            if i.isMultiple(of: 3) { a2a_engine_flush_translated(e) }
            out += render(e, blocks: 1 + i % 3)
        }
        #expect(out.allSatisfy { $0.isFinite && abs($0) <= 0.6 })
        #expect(maxStep(out) < 0.05)
    }
}
