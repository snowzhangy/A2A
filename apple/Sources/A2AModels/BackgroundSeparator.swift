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
    public func process(_ samples: [Float]) throws -> [Float] {
        guard samples.allSatisfy(\.isFinite) else { throw AdapterError.unsupported("non-finite background input") }
        var result: [Float] = []
        result.reserveCapacity(samples.count)
        for start in stride(from: 0, to: samples.count, by: 4096) {
            let block = samples[start..<min(samples.count, start + 4096)]
            let vocals = try voice.render(block)
            let original = try dry.render(block)
            let bed = zip(original, vocals).map { $0 - $1 }
            guard bed.allSatisfy(\.isFinite) else { throw AdapterError.unsupported("non-finite background output") }
            result += bed.map { max(-1, min(1, $0)) }
        }
        return result
    }
}
