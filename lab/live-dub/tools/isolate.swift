// Spike: offline AUSoundIsolation over a WAV. Writes the isolated voice, the dry path through
// the same unit (same latency), and residual = dry - voice (the candidate background bed).
// usage: isolate in.wav outdir [hq|voice]
import AVFoundation
import AudioToolbox

func render(_ file: AVAudioFile, wet: Float, kind: UInt32) throws -> (AVAudioPCMBuffer, Double, Double) {
    file.framePosition = 0
    let format = file.processingFormat
    let engine = AVAudioEngine()
    let player = AVAudioPlayerNode()
    let desc = AudioComponentDescription(componentType: kAudioUnitType_Effect,
                                         componentSubType: kAudioUnitSubType_AUSoundIsolation,
                                         componentManufacturer: kAudioUnitManufacturer_Apple,
                                         componentFlags: 0, componentFlagsMask: 0)
    let unit = AVAudioUnitEffect(audioComponentDescription: desc)
    engine.attach(player)
    engine.attach(unit)
    engine.connect(player, to: unit, format: format)
    engine.connect(unit, to: engine.mainMixerNode, format: format)
    try engine.enableManualRenderingMode(.offline, format: format, maximumFrameCount: 4096)
    var status = AudioUnitSetParameter(unit.audioUnit, AudioUnitParameterID(kAUSoundIsolationParam_SoundToIsolate),
                                       kAudioUnitScope_Global, 0, Float(kind), 0)
    if status != noErr { print("SoundToIsolate status \(status)") }
    status = AudioUnitSetParameter(unit.audioUnit, AudioUnitParameterID(kAUSoundIsolationParam_WetDryMixPercent),
                                   kAudioUnitScope_Global, 0, wet, 0)
    if status != noErr { print("WetDry status \(status)") }
    try engine.start()
    var latency: Double = 0
    var size = UInt32(MemoryLayout<Double>.size)
    AudioUnitGetProperty(unit.audioUnit, kAudioUnitProperty_Latency, kAudioUnitScope_Global, 0, &latency, &size)
    player.scheduleFile(file, at: nil)
    player.play()
    let total = AVAudioFrameCount(file.length) + AVAudioFrameCount(format.sampleRate)  // 1 s tail
    let out = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat, frameCapacity: total)!
    let chunk = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat, frameCapacity: 4096)!
    let started = Date()
    while out.frameLength < total {
        let n = min(4096, total - out.frameLength)
        let result = try engine.renderOffline(n, to: chunk)
        guard result == .success else { print("render result \(result.rawValue)"); break }
        let ch = Int(format.channelCount)
        for c in 0..<ch {
            memcpy(out.floatChannelData![c] + Int(out.frameLength), chunk.floatChannelData![c], Int(chunk.frameLength) * 4)
        }
        out.frameLength += chunk.frameLength
    }
    let seconds = Date().timeIntervalSince(started)
    engine.stop()
    return (out, latency, seconds)
}

func rms(_ x: UnsafePointer<Float>, _ n: Int) -> Double {
    var s = 0.0
    for i in 0..<n { s += Double(x[i]) * Double(x[i]) }
    return (s / Double(max(1, n))).squareRoot()
}

let args = CommandLine.arguments
let input = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
let outDir = URL(fileURLWithPath: args[2])
let kind = (args.count > 3 && args[3] == "voice") ? UInt32(kAUSoundIsolationSoundType_Voice) : UInt32(kAUSoundIsolationSoundType_HighQualityVoice)
print("format", input.processingFormat)
let (voice, latency, wetSeconds) = try render(input, wet: 100, kind: kind)
let (dry, _, drySeconds) = try render(input, wet: 0, kind: kind)
let n = Int(min(voice.frameLength, dry.frameLength))
let residual = AVAudioPCMBuffer(pcmFormat: voice.format, frameCapacity: AVAudioFrameCount(n))!
residual.frameLength = AVAudioFrameCount(n)
for i in 0..<n { residual.floatChannelData![0][i] = dry.floatChannelData![0][i] - voice.floatChannelData![0][i] }
let duration = Double(input.length) / input.processingFormat.sampleRate
print(String(format: "latency_s %.4f  wet_rtf %.3f  dry_rtf %.3f", latency, wetSeconds / duration, drySeconds / duration))
print(String(format: "rms dry %.4f voice %.4f residual %.4f", rms(dry.floatChannelData![0], n), rms(voice.floatChannelData![0], n), rms(residual.floatChannelData![0], n)))
// Alignment check: lag maximizing correlation between input and dry output over 5..15 s.
input.framePosition = 0
let raw = AVAudioPCMBuffer(pcmFormat: input.processingFormat, frameCapacity: AVAudioFrameCount(input.length))!
try input.read(into: raw)
let a = raw.floatChannelData![0], b = dry.floatChannelData![0]
let sr = Int(input.processingFormat.sampleRate)
var best = (0, -Double.infinity)
for lag in stride(from: 0, through: 4800, by: 1) {
    var s = 0.0
    for i in stride(from: 5 * sr, to: 15 * sr, by: 4) { s += Double(a[i]) * Double(b[i + lag]) }
    if s > best.1 { best = (lag, s) }
}
print("dry_lag_samples", best.0)
for (name, buffer) in [("voice", voice), ("dry", dry), ("residual", residual)] {
    let file = try AVAudioFile(forWriting: outDir.appendingPathComponent("\(name).wav"), settings: buffer.format.settings)
    try file.write(from: buffer)
}
