// Streaming windowed-sinc resampler (Kaiser window, tabulated kernel).
// Output sample j is centered on input time j * inRate/outRate; the processing
// latency is `latencyInputFrames` because the kernel needs that much lookahead.

import Foundation

public final class StreamingResampler {
    public let inputRate: Int
    public let outputRate: Int
    /// Zero crossings on each side of the kernel center (at the lower of the two rates).
    public let halfWidth: Int

    private let step: Double          // input frames per output frame
    private let cutoff: Double        // normalized to the input Nyquist
    private let radius: Int           // kernel radius in input frames
    private let tableResolution = 512
    private let table: [Float]
    private var history: [Float] = []
    private var historyStart: Int64 = 0   // absolute input index of history[0]
    private var position: Double = 0      // absolute input position of next output
    private var inputCount: Int64 = 0

    public init(inputRate: Int, outputRate: Int, halfWidth: Int = 16, beta: Double = 8.0) {
        precondition(inputRate > 0 && outputRate > 0 && halfWidth > 0)
        self.inputRate = inputRate
        self.outputRate = outputRate
        self.halfWidth = halfWidth
        step = Double(inputRate) / Double(outputRate)
        cutoff = min(1.0, Double(outputRate) / Double(inputRate)) * 0.97
        if inputRate == outputRate {
            radius = 0
            table = []
            return
        }
        radius = Int((Double(halfWidth) / cutoff).rounded(.up))
        let n = radius * tableResolution
        var t = [Float](repeating: 0, count: n + 2)
        let i0b = Self.besselI0(beta)
        for i in 0...n {
            let x = Double(i) / Double(tableResolution)     // input-frame distance from center
            let sincArg = x * cutoff
            let sinc = sincArg == 0 ? 1.0 : sin(Double.pi * sincArg) / (Double.pi * sincArg)
            let r = x / Double(radius)
            let w = r >= 1 ? 0 : Self.besselI0(beta * (1 - r * r).squareRoot()) / i0b
            t[i] = Float(cutoff * sinc * w)
        }
        table = t
    }

    /// Lookahead required before an output sample can be produced.
    public var latencyInputFrames: Int { radius }
    public var latencySeconds: Double { Double(radius) / Double(inputRate) }

    public func process(_ input: [Float]) -> [Float] {
        if inputRate == outputRate { inputCount += Int64(input.count); return input }
        history.append(contentsOf: input)
        inputCount += Int64(input.count)
        return drain(final: false)
    }

    /// Emits the remaining output, treating samples beyond the end as silence.
    public func flush() -> [Float] {
        if inputRate == outputRate { return [] }
        return drain(final: true)
    }

    private func drain(final: Bool) -> [Float] {
        var out: [Float] = []
        let end = Double(inputCount)
        let expectedTotal = (Double(inputCount) / step).rounded(.down)
        out.reserveCapacity(Int(Double(history.count) / step) + 2)
        history.withUnsafeBufferPointer { hist in
            table.withUnsafeBufferPointer { tab in
                while true {
                    if final {
                        if (position / step).rounded() >= expectedTotal { break }
                    } else if position + Double(radius) >= end {
                        break
                    }
                    out.append(Self.sample(at: position, history: hist, historyStart: historyStart, table: tab,
                                           radius: radius, resolution: tableResolution))
                    position += step
                }
            }
        }
        // Keep only what future outputs can touch.
        let keepFrom = Int64(position.rounded(.down)) - Int64(radius) - 1
        if keepFrom > historyStart {
            let drop = Int(min(Int64(history.count), keepFrom - historyStart))
            history.removeFirst(drop)
            historyStart += Int64(drop)
        }
        return out
    }

    private static func sample(at pos: Double, history: UnsafeBufferPointer<Float>, historyStart: Int64,
                               table: UnsafeBufferPointer<Float>, radius: Int, resolution: Int) -> Float {
        let center = Int64(pos.rounded(.down))
        let frac = pos - Double(center)
        // Valid taps: k in [-radius, radius + 1] that land inside the history buffer.
        let first = max(-radius, Int(historyStart - center))
        let last = min(radius + 1, Int(historyStart + Int64(history.count) - 1 - center))
        guard first <= last else { return 0 }
        let base = Int(center - historyStart)
        let limit = Double(radius)
        let scale = Double(resolution)
        var acc: Float = 0
        var k = first
        while k <= last {
            let dist = abs(Double(k) - frac)
            if dist < limit {
                let f = dist * scale
                let i = Int(f)
                let w = Float(f - Double(i))
                acc += history[base + k] * (table[i] + (table[i + 1] - table[i]) * w)
            }
            k += 1
        }
        return acc
    }

    static func besselI0(_ x: Double) -> Double {
        var sum = 1.0, term = 1.0, k = 1.0
        while term > 1e-12 * sum {
            term *= (x / (2 * k)) * (x / (2 * k))
            sum += term
            k += 1
        }
        return sum
    }

    /// Convenience for whole buffers.
    public static func resample(_ input: [Float], from inputRate: Int, to outputRate: Int) -> [Float] {
        let r = StreamingResampler(inputRate: inputRate, outputRate: outputRate)
        return r.process(input) + r.flush()
    }
}
