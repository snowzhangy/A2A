// Causal, ordinary-thread background extraction. Never call from an audio callback.
import AVFoundation
import AudioToolbox
import Foundation

public final class BackgroundSeparator {
    private final class Path {
        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        let unit: AVAudioUnitEffect
        let format: AVAudioFormat
        let output: AVAudioPCMBuffer
        init(wet: Float) throws {
            format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
            unit = AVAudioUnitEffect(audioComponentDescription: AudioComponentDescription(
                componentType: kAudioUnitType_Effect, componentSubType: kAudioUnitSubType_AUSoundIsolation,
                componentManufacturer: kAudioUnitManufacturer_Apple, componentFlags: 0, componentFlagsMask: 0))
            output = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4096)!
            engine.attach(player)
            engine.attach(unit)
            engine.connect(player, to: unit, format: format)
            engine.connect(unit, to: engine.mainMixerNode, format: format)
            try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 4096)
            for (parameter, value) in [
                (kAUSoundIsolationParam_SoundToIsolate, Float(kAUSoundIsolationSoundType_HighQualityVoice)),
                (kAUSoundIsolationParam_WetDryMixPercent, wet)] {
                let status = AudioUnitSetParameter(unit.audioUnit, AudioUnitParameterID(parameter),
                                                   kAudioUnitScope_Global, 0, value, 0)
                guard status == noErr else { throw AdapterError.unsupported("sound isolation configuration failed") }
            }
            try engine.start()
            player.play()
        }
        deinit { engine.stop() }
        func render(_ samples: ArraySlice<Float>) throws -> [Float] {
            let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))!
            input.frameLength = AVAudioFrameCount(samples.count)
            samples.withUnsafeBufferPointer { src in
                input.floatChannelData![0].update(from: src.baseAddress!, count: samples.count)
            }
            player.scheduleBuffer(input)
            let status = try engine.renderOffline(AVAudioFrameCount(samples.count), to: output)
            guard status == .success, output.frameLength == samples.count else {
                throw AdapterError.unsupported("sound isolation render failed")
            }
            return Array(UnsafeBufferPointer(start: output.floatChannelData![0], count: samples.count))
        }
    }
    private let voice: Path
    private let dry: Path
    public init() throws {
        voice = try Path(wet: 100)
        dry = try Path(wet: 0)
    }
    /// How far the bed lags its input. Both paths run the same unit, so the subtraction stays
    /// aligned and the bed inherits the unit's reported processing latency (0 if unreported).
    public var latencySeconds: Double {
        let latency = voice.unit.auAudioUnit.latency
        return latency.isFinite && latency > 0 ? latency : 0
    }
    /// The voice alone in `samples` (48 kHz mono), for a speaker reference (ADR-0016). A fresh
    /// unit per call, so the result depends only on its input; the unit's latency is flushed
    /// with silence and removed, so output sample i is the voice of input sample i. The first
    /// `preroll` samples only warm the unit up and are not returned.
    public static func isolateVoice(_ samples: [Float], preroll: Int = 0) throws -> [Float] {
        guard preroll >= 0, preroll < samples.count else { throw AdapterError.unsupported("empty voice isolation input") }
        guard samples.allSatisfy(\.isFinite) else { throw AdapterError.unsupported("non-finite voice isolation input") }
        let path = try Path(wet: 100)
        let reported = path.unit.auAudioUnit.latency
        let latency = reported.isFinite && reported > 0 ? Int((reported * 48_000).rounded()) : 0
        let padded = samples + [Float](repeating: 0, count: latency)
        var vocals: [Float] = []
        vocals.reserveCapacity(padded.count)
        for start in stride(from: 0, to: padded.count, by: 4096) {
            vocals += try path.render(padded[start..<min(padded.count, start + 4096)])
        }
        let voice = vocals[(latency + preroll)...]
        guard voice.allSatisfy(\.isFinite) else { throw AdapterError.unsupported("non-finite voice isolation output") }
        return voice.map { max(-1, min(1, $0)) }
    }
    public func process(_ samples: [Float]) throws -> [Float] {
        try separate(samples).bed
    }

    /// The bed and the voice the unit isolated from `samples`, both lagging the input by
    /// `latencySeconds`. The voice finds pauses that music under dialogue hides (ADR-0017).
    public func separate(_ samples: [Float]) throws -> (bed: [Float], voice: [Float]) {
        guard samples.allSatisfy(\.isFinite) else { throw AdapterError.unsupported("non-finite background input") }
        var bed: [Float] = []
        var voiceOut: [Float] = []
        bed.reserveCapacity(samples.count)
        voiceOut.reserveCapacity(samples.count)
        for start in stride(from: 0, to: samples.count, by: 4096) {
            let block = samples[start..<min(samples.count, start + 4096)]
            let vocals = try voice.render(block)
            let original = try dry.render(block)
            let rest = zip(original, vocals).map { $0 - $1 }
            guard rest.allSatisfy(\.isFinite), vocals.allSatisfy(\.isFinite) else {
                throw AdapterError.unsupported("non-finite background output")
            }
            bed += rest.map { max(-1, min(1, $0)) }
            voiceOut += vocals.map { max(-1, min(1, $0)) }
        }
        return (bed, voiceOut)
    }
}
