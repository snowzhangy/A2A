// Speaker voiceprints for the DSP speaker tracker: the mel cepstrum and pitch of voiced
// frames in 16 kHz mono speech. Non-RT; traces carry speaker IDs, never these features.
// The opt-in research voice memory may retain a reference's print in its private store.
//
// Every 10 ms frame above -40 dBFS is tested for voicing by normalized autocorrelation
// (40 ms Hann window, 60–350 Hz). A voiced frame contributes a 16-coefficient cepstrum
// (25 ms Hamming window, 512-point FFT, 32 mel bands from 60 to 7600 Hz) and its pitch.
// Only voiced frames count: vowels carry the speaker's timbre, and silence, noise, and
// fricatives would only blur it.

import Accelerate
import Foundation

/// Voiced-frame statistics of one stretch of speech, or of a speaker cluster.
public struct Voiceprint: Sendable, Equatable, Codable {
    public static let coefficients = 16
    /// Typical spread of each voiced-frame cepstral coefficient in speech, measured on a
    /// 120 s drama excerpt. Distances are expressed in these units.
    static let spread: [Double] = [12.05, 17.11, 15.91, 11.35, 8.44, 8.28, 7.72, 7.2,
                                   6.91, 6.26, 6.07, 5.02, 4.93, 4.62, 4.45, 4.18]
    /// Pitch histogram: 1/24-octave bins from 60 Hz.
    static let pitchFloorHz = 60.0
    static let pitchBinsPerOctave = 24.0
    static let pitchBins = 64

    /// Sum of spread-normalized cepstra over voiced frames, and the (possibly capped) frame weight.
    public private(set) var sum = [Double](repeating: 0, count: Voiceprint.coefficients)
    public private(set) var weight = 0.0
    private var pitch = [Double](repeating: 0, count: Voiceprint.pitchBins)

    public init() {}

    /// Voiced frames behind this print (after any cap).
    public var voicedFrames: Int { Int(weight.rounded()) }
    /// Validate persisted research features before arithmetic or indexing.
    public var isValid: Bool {
        sum.count == Self.coefficients && pitch.count == Self.pitchBins
            && weight.isFinite && weight > 0 && weight <= 10_000
            && sum.allSatisfy(\.isFinite) && pitch.allSatisfy { $0.isFinite && $0 >= 0 }
    }

    public var mean: [Double] { weight > 0 ? sum.map { $0 / weight } : sum }

    /// Median pitch of the voiced frames.
    public var medianPitchHz: Double? {
        let total = pitch.reduce(0, +)
        guard total > 0 else { return nil }
        var acc = 0.0
        for (bin, count) in pitch.enumerated() {
            acc += count
            if acc >= total / 2 {
                return Self.pitchFloorHz * pow(2, (Double(bin) + 0.5) / Self.pitchBinsPerOctave)
            }
        }
        return nil
    }

    mutating func addFrame(cepstrum: UnsafeBufferPointer<Float>, pitchHz: Double) {
        for i in 0..<Self.coefficients { sum[i] += Double(cepstrum[i]) / Self.spread[i] }
        weight += 1
        let bin = Int((log2(pitchHz / Self.pitchFloorHz) * Self.pitchBinsPerOctave).rounded(.down))
        pitch[min(Self.pitchBins - 1, max(0, bin))] += 1
    }

    public mutating func add(_ other: Voiceprint) {
        for i in 0..<Self.coefficients { sum[i] += other.sum[i] }
        for i in 0..<Self.pitchBins { pitch[i] += other.pitch[i] }
        weight += other.weight
    }

    /// Bounds the weight so a cluster keeps adapting to its speaker's recent speech.
    public mutating func cap(frames: Int) {
        guard weight > Double(frames) else { return }
        let f = Double(frames) / weight
        sum = sum.map { $0 * f }
        pitch = pitch.map { $0 * f }
        weight = Double(frames)
    }

    /// RMS difference of the mean normalized cepstra.
    public func distance(to other: Voiceprint) -> Double {
        guard weight > 0, other.weight > 0 else { return .infinity }
        var acc = 0.0
        for i in 0..<Self.coefficients {
            let d = sum[i] / weight - other.sum[i] / other.weight
            acc += d * d
        }
        return (acc / Double(Self.coefficients)).squareRoot()
    }
}

/// Streams 16 kHz samples and adds each voiced frame to a caller-owned voiceprint.
public struct VoiceprintAnalyzer: Sendable {
    public static let sampleRate = 16_000
    static let hop = 160
    static let pitchWindow = 640
    static let cepstrumWindow = 400
    static let fftSize = 512
    static let bands = 32
    /// Autocorrelation lags searched (400 Hz down to 60 Hz); peaks above 350 Hz are
    /// rejected as octave errors rather than replaced by a lower peak.
    static let minLag = 40
    static let maxLag = 266
    static let minVoicedLag = 46
    static let voicing: Float = 0.5
    static let levelDB: Float = -40

    private let tables: Tables
    /// Samples from absolute index `bufferStart`; frame `next` is analyzed once its pitch
    /// window is complete.
    private var buffer: [Float] = []
    private var bufferStart = 0
    private var next = 2
    private var total = 0
    private var scratch: Scratch

    public init() {
        tables = Tables.shared
        scratch = Scratch()
    }

    /// Consumes `samples`. Voiced frames are added to `print`; with nil, frames are only
    /// skipped (nothing is analyzed outside speech).
    public mutating func process(_ samples: [Float], into print: inout Voiceprint?) {
        buffer.append(contentsOf: samples)
        total += samples.count
        while next * Self.hop + Self.hop / 2 + Self.pitchWindow / 2 <= total {
            if print != nil { analyzeFrame(next, into: &print!) }
            next += 1
        }
        // Keep what the next frame's pitch window needs.
        let keepFrom = next * Self.hop + Self.hop / 2 - Self.pitchWindow / 2
        if keepFrom - bufferStart > 4_096 {
            buffer.removeFirst(keepFrom - bufferStart)
            bufferStart = keepFrom
        }
    }

    private mutating func analyzeFrame(_ i: Int, into print: inout Voiceprint) {
        let base = i * Self.hop - bufferStart
        let center = base + Self.hop / 2
        let tables = self.tables
        buffer.withUnsafeBufferPointer { x in
            scratch.analyze(x: x, hopStart: base, center: center, tables: tables, into: &print)
        }
    }

    /// Windows, filterbank, and FFT setup shared by every analyzer.
    final class Tables: @unchecked Sendable {
        static let shared = Tables()
        let hann: [Float]
        let hamming: [Float]
        /// Mel filterbank as (first bin, weights) per band, over 257 power bins.
        let filters: [(start: Int, weights: [Float])]
        /// DCT-II rows for cepstral coefficients 1...16 over the 32 log-mel bands.
        let dct: [Float]
        let fft: FFTSetup

        private init() {
            let n = VoiceprintAnalyzer.pitchWindow, m = VoiceprintAnalyzer.cepstrumWindow
            hann = (0..<n).map { Float(0.5 - 0.5 * cos(2 * Double.pi * Double($0) / Double(n - 1))) }
            hamming = (0..<m).map { Float(0.54 - 0.46 * cos(2 * Double.pi * Double($0) / Double(m - 1))) }
            let bands = VoiceprintAnalyzer.bands, size = VoiceprintAnalyzer.fftSize
            func mel(_ f: Double) -> Double { 2595 * log10(1 + f / 700) }
            func hz(_ m: Double) -> Double { 700 * (pow(10, m / 2595) - 1) }
            let lo = mel(60), hi = mel(7600)
            let edges = (0...bands + 1).map { hz(lo + (hi - lo) * Double($0) / Double(bands + 1)) }
            let bins = edges.map { Int((Double(size + 1) * $0 / Double(VoiceprintAnalyzer.sampleRate)).rounded(.down)) }
            filters = (1...bands).map { b in
                let l = bins[b - 1], c = bins[b], r = bins[b + 1]
                var w: [Float] = []
                for k in l..<c { w.append(Float(k - l) / Float(max(1, c - l))) }
                for k in c..<r { w.append(Float(r - k) / Float(max(1, r - c))) }
                return (l, w)
            }
            var d = [Float](repeating: 0, count: Voiceprint.coefficients * bands)
            for q in 1...Voiceprint.coefficients {
                for b in 0..<bands {
                    d[(q - 1) * bands + b] = Float(cos(Double.pi * Double(q) * (Double(b) + 0.5) / Double(bands)))
                }
            }
            dct = d
            fft = vDSP_create_fftsetup(9, FFTRadix(kFFTRadix2))!
        }

        deinit { vDSP_destroy_fftsetup(fft) }
    }

    /// Per-analyzer work buffers, allocated once.
    struct Scratch: Sendable {
        var frame = [Float](repeating: 0, count: VoiceprintAnalyzer.pitchWindow + VoiceprintAnalyzer.maxLag)
        var lags = [Float](repeating: 0, count: VoiceprintAnalyzer.maxLag)
        var spectrum = [Float](repeating: 0, count: VoiceprintAnalyzer.fftSize)
        var real = [Float](repeating: 0, count: VoiceprintAnalyzer.fftSize / 2)
        var imag = [Float](repeating: 0, count: VoiceprintAnalyzer.fftSize / 2)
        var power = [Float](repeating: 0, count: VoiceprintAnalyzer.fftSize / 2 + 1)
        var logMel = [Float](repeating: 0, count: VoiceprintAnalyzer.bands)
        var cepstrum = [Float](repeating: 0, count: Voiceprint.coefficients)

        mutating func analyze(x: UnsafeBufferPointer<Float>, hopStart: Int, center: Int, tables: Tables,
                              into print: inout Voiceprint) {
            typealias A = VoiceprintAnalyzer
            // Level gate on the frame's own 10 ms hop.
            var meanSquare: Float = 0
            vDSP_measqv(x.baseAddress! + hopStart, 1, &meanSquare, vDSP_Length(A.hop))
            guard 10 * log10(meanSquare + 1e-12) >= A.levelDB else { return }

            // Voicing and pitch: normalized autocorrelation of the mean-removed Hann frame.
            let n = A.pitchWindow
            let start = center - n / 2
            var mean: Float = 0
            vDSP_meanv(x.baseAddress! + start, 1, &mean, vDSP_Length(n))
            frame.withUnsafeMutableBufferPointer { f in
                for k in 0..<n { f[k] = (x[start + k] - mean) * tables.hann[k] }
                for k in n..<f.count { f[k] = 0 }
                lags.withUnsafeMutableBufferPointer { r in
                    vDSP_conv(f.baseAddress!, 1, f.baseAddress!, 1, r.baseAddress!, 1, vDSP_Length(A.maxLag), vDSP_Length(n))
                }
            }
            guard lags[0] > 0 else { return }
            var peak: Float = 0
            var index: vDSP_Length = 0
            lags.withUnsafeBufferPointer { r in
                vDSP_maxvi(r.baseAddress! + A.minLag, 1, &peak, &index, vDSP_Length(A.maxLag - A.minLag))
            }
            let lag = A.minLag + Int(index)
            guard peak / lags[0] >= A.voicing, lag >= A.minVoicedLag else { return }

            // Cepstrum of the 25 ms Hamming frame.
            let m = A.cepstrumWindow
            let cStart = center - m / 2
            for k in 0..<m { spectrum[k] = x[cStart + k] * tables.hamming[k] }
            for k in m..<A.fftSize { spectrum[k] = 0 }
            let half = A.fftSize / 2
            real.withUnsafeMutableBufferPointer { re in
                imag.withUnsafeMutableBufferPointer { im in
                    var split = DSPSplitComplex(realp: re.baseAddress!, imagp: im.baseAddress!)
                    spectrum.withUnsafeBufferPointer { s in
                        s.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: half) {
                            vDSP_ctoz($0, 2, &split, 1, vDSP_Length(half))
                        }
                    }
                    vDSP_fft_zrip(tables.fft, &split, 1, 9, FFTDirection(FFT_FORWARD))
                }
            }
            // vDSP's packed real FFT is twice the DFT; halve to match it.
            power[0] = (real[0] / 2) * (real[0] / 2)
            power[half] = (imag[0] / 2) * (imag[0] / 2)
            for k in 1..<half { power[k] = (real[k] * real[k] + imag[k] * imag[k]) / 4 }
            for (b, filter) in tables.filters.enumerated() {
                var e: Float = 0
                for (j, w) in filter.weights.enumerated() { e += w * power[filter.start + j] }
                logMel[b] = log(e + 1e-10)
            }
            for q in 0..<Voiceprint.coefficients {
                var c: Float = 0
                for b in 0..<A.bands { c += tables.dct[q * A.bands + b] * logMel[b] }
                cepstrum[q] = c
            }
            cepstrum.withUnsafeBufferPointer {
                print.addFrame(cepstrum: $0, pitchHz: Double(A.sampleRate) / Double(lag))
            }
        }
    }
}
