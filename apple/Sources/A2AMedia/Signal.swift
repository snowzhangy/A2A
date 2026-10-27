// Signal measurements for capture fidelity (A1), crossfade checks (A2), and fixtures.

import Accelerate
import Foundation

public enum Signal {
    public static func rms(_ x: ArraySlice<Float>) -> Float {
        guard !x.isEmpty else { return 0 }
        var acc: Double = 0
        for v in x { acc += Double(v) * Double(v) }
        return Float((acc / Double(x.count)).squareRoot())
    }

    public static func rms(_ x: [Float]) -> Float { rms(x[...]) }

    public static func dbfs(_ rms: Float) -> Float { rms > 0 ? 20 * log10(rms) : -.infinity }

    public static func peak(_ x: [Float]) -> Float { x.reduce(0) { max($0, abs($1)) } }

    /// Pearson correlation of equal-length slices.
    public static func correlation(_ a: ArraySlice<Float>, _ b: ArraySlice<Float>) -> Double {
        let n = min(a.count, b.count)
        guard n > 1 else { return 0 }
        var sa = 0.0, sb = 0.0
        for i in 0..<n { sa += Double(a[a.startIndex + i]); sb += Double(b[b.startIndex + i]) }
        let ma = sa / Double(n), mb = sb / Double(n)
        var num = 0.0, da = 0.0, db = 0.0
        for i in 0..<n {
            let x = Double(a[a.startIndex + i]) - ma
            let y = Double(b[b.startIndex + i]) - mb
            num += x * y; da += x * x; db += y * y
        }
        return da > 0 && db > 0 ? num / (da * db).squareRoot() : 0
    }

    /// Lag (in samples) of `signal` relative to `reference` maximizing correlation,
    /// searched over `0...maxLag` using a probe window from the reference.
    ///
    /// One vDSP pass computes the dot product of the zero-mean probe with every candidate
    /// window; prefix sums give each window's variance. A 30-minute capture searched over
    /// several seconds of lag stays within seconds instead of a per-lag Pearson loop.
    public static func bestLag(reference: [Float], signal: [Float], probe: Range<Int>, maxLag: Int) -> (lag: Int, correlation: Double) {
        let m = probe.count
        let lags = min(maxLag + 1, signal.count - probe.upperBound + 1)
        guard m > 1, lags > 0 else { return (0, -2) }
        let window = reference[probe]
        let mean = window.reduce(0.0) { $0 + Double($1) } / Double(m)
        let centered = window.map { Float(Double($0) - mean) }
        let probeEnergy = centered.reduce(0.0) { $0 + Double($1) * Double($1) }
        guard probeEnergy > 0 else { return (0, 0) }

        var dots = [Float](repeating: 0, count: lags)
        signal.withUnsafeBufferPointer { s in
            vDSP_conv(s.baseAddress! + probe.lowerBound, 1, centered, 1, &dots, 1, vDSP_Length(lags), vDSP_Length(m))
        }
        // Prefix sums over the searched span: window variance = Σx² − (Σx)²/m.
        let span = lags + m - 1
        var sum = [Double](repeating: 0, count: span + 1)
        var sumSquares = [Double](repeating: 0, count: span + 1)
        for i in 0..<span {
            let v = Double(signal[probe.lowerBound + i])
            sum[i + 1] = sum[i] + v
            sumSquares[i + 1] = sumSquares[i] + v * v
        }
        var bestLag = 0
        var bestScore = -Double.infinity
        for lag in 0..<lags {
            let s = sum[lag + m] - sum[lag]
            let variance = sumSquares[lag + m] - sumSquares[lag] - s * s / Double(m)
            guard variance > 0 else { continue }
            let score = Double(dots[lag]) / (probeEnergy * variance).squareRoot()
            if score > bestScore { (bestLag, bestScore) = (lag, score) }
        }
        let start = probe.lowerBound + bestLag
        return (bestLag, correlation(window, signal[start..<start + m]))
    }

    /// Runs of samples exactly equal to zero longer than `minRun`, a dropout signature in
    /// captured audio whose source has no digital silence.
    public static func zeroRuns(_ x: [Float], minRun: Int) -> [Range<Int>] {
        var runs: [Range<Int>] = []
        var start: Int?
        for (i, v) in x.enumerated() {
            if v == 0 {
                if start == nil { start = i }
            } else if let s = start {
                if i - s >= minRun { runs.append(s..<i) }
                start = nil
            }
        }
        if let s = start, x.count - s >= minRun { runs.append(s..<x.count) }
        return runs
    }

    /// Largest absolute sample-to-sample step: a click detector for gain ramps.
    public static func maxStep(_ x: [Float]) -> Float {
        var m: Float = 0
        for i in 1..<max(1, x.count) { m = max(m, abs(x[i] - x[i - 1])) }
        return m
    }
}

/// Deterministic test signals.
public enum TestSignal {
    /// Sum of sines plus a slow chirp: no digital silence, distinctive for alignment.
    public static func fidelityProbe(sampleRate: Int, seconds: Double, amplitude: Float = 0.25) -> [Float] {
        let n = Int(Double(sampleRate) * seconds)
        var out = [Float](repeating: 0, count: n)
        let sr = Double(sampleRate)
        for i in 0..<n {
            let t = Double(i) / sr
            let chirp = sin(2 * .pi * (220 * t + 40 * t * t / max(seconds, 1)))
            let v = 0.5 * sin(2 * .pi * 440 * t) + 0.3 * sin(2 * .pi * 1_234.5 * t) + 0.2 * chirp
            out[i] = Float(v) * amplitude
        }
        return out
    }

    public static func sine(frequency: Double, sampleRate: Int, seconds: Double, amplitude: Float) -> [Float] {
        let n = Int(Double(sampleRate) * seconds)
        return (0..<n).map { Float(sin(2 * .pi * frequency * Double($0) / Double(sampleRate))) * amplitude }
    }

    /// Short clicks every `interval` seconds for loopback latency measurement.
    public static func clickTrack(sampleRate: Int, seconds: Double, interval: Double, amplitude: Float = 0.8) -> [Float] {
        var out = [Float](repeating: 0, count: Int(Double(sampleRate) * seconds))
        let step = Int(Double(sampleRate) * interval)
        var i = step / 2
        while i < out.count - 48 {
            for k in 0..<48 { out[i + k] = amplitude * (k.isMultiple(of: 2) ? 1 : -1) * Float(48 - k) / 48 }
            i += step
        }
        return out
    }
}
