import A2AMedia
import Foundation
import Testing

@Suite struct WAVTests {
    @Test func float32RoundTrip() throws {
        let audio = WAVAudio(sampleRate: 48_000, channels: 2, samples: [0, 0.5, -0.5, 1, 0.25, -1])
        let decoded = try WAV.decode(WAV.encodeFloat32(audio))
        #expect(decoded.sampleRate == 48_000)
        #expect(decoded.channels == 2)
        #expect(decoded.samples == audio.samples)
        #expect(decoded.mono() == [0.25, 0.25, -0.375])
    }

    @Test func streamWriterPatchesHeader() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("w-\(UUID()).wav")
        defer { try? FileManager.default.removeItem(at: url) }
        let w = try WAVStreamWriter(url: url, sampleRate: 16_000, channels: 1)
        w.append([0.1, 0.2])
        w.append([0.3])
        try w.close()
        let a = try WAV.read(url)
        #expect(a.samples == [0.1, 0.2, 0.3])
        #expect(a.sampleRate == 16_000)
    }

    @Test func rejectsNonRIFF() {
        #expect(throws: WAVError.self) { try WAV.decode(Data("nope".utf8)) }
    }

    @Test func rejectsTruncatedChunksAndPartialFrames() {
        let audio = WAVAudio(sampleRate: 48_000, channels: 2, samples: [0.2, -0.2])
        let valid = WAV.encodeFloat32(audio)
        #expect(throws: WAVError.self) { try WAV.decode(Data(valid.dropLast())) }
        // A complete float sample is still an incomplete stereo frame.
        var partial = WAV.encodeFloat32(WAVAudio(sampleRate: 48_000, channels: 2, samples: [0.2]))
        #expect(throws: WAVError.self) { try WAV.decode(partial) }
        partial = valid
        partial.replaceSubrange(16..<20, with: [2, 0, 0, 0])
        #expect(throws: WAVError.self) { try WAV.decode(partial) }
    }

    @Test func rejectsShortExtensibleFormat() {
        var data = WAV.encodeFloat32(WAVAudio(sampleRate: 48_000, channels: 1, samples: [0]))
        data.replaceSubrange(20..<22, with: [0xFE, 0xFF])
        #expect(throws: WAVError.self) { try WAV.decode(data) }
    }
}

@Suite struct ResamplerTests {
    @Test func equalRatesHaveNoFilterLatency() {
        let resampler = StreamingResampler(inputRate: 48_000, outputRate: 48_000)
        #expect(resampler.latencyInputFrames == 0)
        #expect(resampler.latencySeconds == 0)
        #expect(resampler.process([0, 0.4, -0.7]) == [0, 0.4, -0.7])
        #expect(resampler.process([]).isEmpty)
        #expect(resampler.flush().isEmpty)
    }

    @Test func downsamplePreservesInBandSineAndLength() {
        let input = TestSignal.sine(frequency: 1_000, sampleRate: 48_000, seconds: 1, amplitude: 0.5)
        let out = StreamingResampler.resample(input, from: 48_000, to: 16_000)
        #expect(abs(out.count - 16_000) <= 1)
        let reference = TestSignal.sine(frequency: 1_000, sampleRate: 16_000, seconds: 1, amplitude: 0.5)
        let c = Signal.correlation(out[200..<15_800], reference[200..<15_800])
        #expect(c > 0.999)
    }

    @Test func downsampleRejectsAliasingTone() {
        // 10 kHz is above the 8 kHz Nyquist of the 16 kHz output.
        let input = TestSignal.sine(frequency: 10_000, sampleRate: 48_000, seconds: 0.5, amplitude: 0.5)
        let out = StreamingResampler.resample(input, from: 48_000, to: 16_000)
        #expect(Signal.dbfs(Signal.rms(out[400..<7_600])) < -50)
    }

    @Test func streamingMatchesWholeBuffer() {
        let input = TestSignal.fidelityProbe(sampleRate: 24_000, seconds: 0.5)
        let whole = StreamingResampler.resample(input, from: 24_000, to: 48_000)
        let r = StreamingResampler(inputRate: 24_000, outputRate: 48_000)
        var streamed: [Float] = []
        var i = 0
        while i < input.count {
            let n = min(317, input.count - i)
            streamed += r.process(Array(input[i..<i + n]))
            i += n
        }
        streamed += r.flush()
        #expect(streamed.count == whole.count)
        let maxDiff = zip(streamed, whole).map { abs($0 - $1) }.max() ?? 1
        #expect(maxDiff < 1e-5)
    }
}

@Suite struct SignalTests {
    @Test func bestLagFindsInsertedDelay() {
        let ref = TestSignal.fidelityProbe(sampleRate: 48_000, seconds: 0.5)
        let delayed = [Float](repeating: 0, count: 137) + ref
        let r = Signal.bestLag(reference: ref, signal: delayed, probe: 4_800..<9_600, maxLag: 500)
        #expect(r.lag == 137)
        #expect(r.correlation > 0.9999)
    }

    @Test func bestLagFindsCaptureStartInLongReference() {
        // A1 shape: the capture starts somewhere in the first seconds of a long reference.
        let ref = TestSignal.fidelityProbe(sampleRate: 48_000, seconds: 20)
        let capture = Array(ref[81_234...])
        let r = Signal.bestLag(reference: capture, signal: ref, probe: 48_000..<96_000, maxLag: 10 * 48_000)
        #expect(r.lag == 81_234)
        #expect(r.correlation > 0.9999)
    }

    @Test func zeroRunsDetectDropouts() {
        var x = [Float](repeating: 0.1, count: 1_000)
        for i in 400..<480 { x[i] = 0 }
        #expect(Signal.zeroRuns(x, minRun: 32) == [400..<480])
        #expect(Signal.zeroRuns(x, minRun: 100).isEmpty)
    }
}
