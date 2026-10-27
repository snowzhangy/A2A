// FixturePlayer: the harness tap target. Plays a WAV (or a generated probe) through the
// default output so capture tests never depend on Zoom. Packaged by
// scripts/build_fixture_player.sh as dev.a2a.FixturePlayer.app (stable bundle ID).
//
//   FixturePlayer --file probe.wav [--loop] [--seconds N] [--start-delay S] [--gain G]
//   FixturePlayer --probe [--seconds N]          deterministic fidelity probe signal
//   FixturePlayer --silence [--seconds N]        opens output but renders digital silence
//   --write-reference ref.wav                    also save exactly what is played (mono), the
//                                                reference for `a2a-tapctl fidelity`

import A2AMedia
import AVFoundation
import Foundation

var file: String?
var loop = false
var seconds: Double?
var startDelay = 0.0
var gain: Float = 1.0
var mode = "file"
var referencePath: String?

var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    switch a {
    case "--file": file = args.next()
    case "--loop": loop = true
    case "--seconds": seconds = args.next().flatMap(Double.init)
    case "--start-delay": startDelay = args.next().flatMap(Double.init) ?? 0
    case "--gain": gain = args.next().flatMap(Float.init) ?? 1
    case "--probe": mode = "probe"
    case "--silence": mode = "silence"
    case "--write-reference": referencePath = args.next()
    default:
        FileHandle.standardError.write(Data("FixturePlayer: unknown argument \(a)\n".utf8))
        exit(2)
    }
}

let samples: [Float]
let rate: Int
switch mode {
case "probe":
    rate = 48_000
    samples = TestSignal.fidelityProbe(sampleRate: rate, seconds: seconds ?? 30)
case "silence":
    rate = 48_000
    samples = [Float](repeating: 0, count: Int(Double(rate) * (seconds ?? 30)))
default:
    guard let file else {
        FileHandle.standardError.write(Data("FixturePlayer: pass --file, --probe, or --silence\n".utf8))
        exit(2)
    }
    let wav = try WAV.read(URL(fileURLWithPath: file))
    rate = wav.sampleRate
    samples = wav.mono()
}

let engine = AVAudioEngine()
let player = AVAudioPlayerNode()
let format = AVAudioFormat(standardFormatWithSampleRate: Double(rate), channels: 1)!
let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))!
buffer.frameLength = AVAudioFrameCount(samples.count)
for i in 0..<samples.count { buffer.floatChannelData![0][i] = samples[i] * gain }
if let referencePath {
    let played = UnsafeBufferPointer(start: buffer.floatChannelData![0], count: samples.count)
    try WAV.write(WAVAudio(sampleRate: rate, channels: 1, samples: Array(played)), to: URL(fileURLWithPath: referencePath))
}

engine.attach(player)
engine.connect(player, to: engine.mainMixerNode, format: format)
try engine.start()
print("{\"pid\":\(getpid()),\"rate\":\(rate),\"frames\":\(samples.count),\"mode\":\"\(mode)\"}")
fflush(stdout)

if startDelay > 0 { Thread.sleep(forTimeInterval: startDelay) }
player.scheduleBuffer(buffer, at: nil, options: loop ? .loops : []) {}
player.play()

let duration = seconds ?? (loop ? .infinity : Double(samples.count) / Double(rate))
let signals = [SIGINT, SIGTERM].map { sig -> DispatchSourceSignal in
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler { engine.stop(); exit(0) }
    s.resume()
    return s
}
_ = signals
if duration.isFinite {
    RunLoop.main.run(until: Date().addingTimeInterval(duration + 0.25))
    engine.stop()
} else {
    RunLoop.main.run()
}
