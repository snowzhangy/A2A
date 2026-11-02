// a2a-loopprobe: does a global tap hear what WE render? The feedback check behind `--tap-global`.
//
// The app captures the machine's whole output mix, so the one thing that would poison it is our own
// translated playback coming back around: the pipeline would answer itself, forever. A global tap is built
// from a list of processes to EXCLUDE and we exclude our own — this proves that on the real device instead
// of trusting the API, and fails loudly if Apple ever changes the meaning.
//
//   a2a-loopprobe [--frequency 12500] [--seconds 3] [--json out.json]
//
// A tone above the speech band is played twice while the global tap runs: once into our own translated bus
// (which goes out of the default output device) and once through `afplay`, another process. The other
// process's tone must be clearly visible in the capture and ours must sit at the floor. Capture permission
// and a logged-in session with a working output device are required: this is an interactive-runner check,
// never a headless one. Exit code 1 means our own output is audible to the tap.

import A2AAudio
import A2AMedia
import A2ARTShim
import Foundation

func argument(_ name: String, _ default: Double) -> Double {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: name), i + 1 < args.count, let value = Double(args[i + 1]) else { return `default` }
    return value
}

let frequency = argument("--frequency", 12_500)          // well above speech; nothing else plays it
let phaseSeconds = argument("--seconds", 3)
let jsonPath = CommandLine.arguments.firstIndex(of: "--json").flatMap {
    CommandLine.arguments.indices.contains($0 + 1) ? CommandLine.arguments[$0 + 1] : nil
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("a2a-loopprobe: \(message)\n".data(using: .utf8)!)
    exit(2)
}

// The tone the probe plays, to a file `afplay` can read for the control phase.
func writeTone(to url: URL, rate: Int) throws {
    let writer = try WAVStreamWriter(url: url, sampleRate: rate, channels: 2)
    let frames = rate / 10
    var block = [Float](repeating: 0, count: frames * 2)
    for second in 0..<4 {
        for i in 0..<frames {
            let t = Double(second) + Double(i) / Double(rate)
            let sample: Float = 0.25 * Float(sin(2.0 * Double.pi * frequency * t))
            block[i * 2] = sample
            block[i * 2 + 1] = sample
        }
        writer.append(block)
    }
    try writer.close()
}

let toneURL = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("a2a-loopprobe-tone.wav")
let processes = (try? AudioProcessList.current()) ?? []
let source = SourceResolver.systemAudio(processes: processes)
guard source.global else { fail("the system-audio source is not a global tap") }

var options = TapSessionOptions()
options.mute = .unmuted        // leave whatever the machine is playing alone: we only listen
options.renderOutput = true    // and we do render, because that is the path that could loop back
options.name = "A2A Loop Probe"

let session: TapSession
do { session = try TapSession(source: source, options: options) } catch { fail("\(error)") }
do { try session.start() } catch { fail("start: \(error)") }
guard let engine = session.engine else { fail("no engine") }
defer { session.teardown() }

let rate = session.sampleRate
let channels = Int(session.channels)
try writeTone(to: toneURL, rate: Int(rate))
a2a_engine_set_gains(engine, 0, 1, 0)   // silence the original bus; only our tone reaches the device

/// The loudest 12.5 kHz magnitude and the loudest RMS seen while listening.
func listen(_ seconds: Double, ownTone: Bool, otherProcess: Bool) -> (tone: Double, rms: Double) {
    let slice = Int(rate * 0.05) * channels
    var captured = [Float](repeating: 0, count: slice)
    var tone = [Float](repeating: 0, count: slice)
    var best = (tone: 0.0, rms: 0.0)
    var child: Process?
    if otherProcess {
        child = Process()
        child?.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
        child?.arguments = [toneURL.path]
        try? child?.run()
    }
    var written = 0
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        if ownTone, let translated = session.translatedRing {
            let frames = slice / channels
            for i in 0..<frames {
                let phase = Double(written + i) / rate
                let sine = sin(2.0 * Double.pi * frequency * phase)
                let sample: Float = 0.25 * Float(sine)
                for channel in 0..<channels { tone[i * channels + channel] = sample }
            }
            written += frames
            a2a_ring_write(translated, &tone, UInt32(frames))
        }
        usleep(20_000)
        guard let capture = session.captureRing else { continue }
        let readable = min(Int(a2a_ring_readable(capture)) / channels * channels, slice)
        guard readable > 0 else { continue }
        let got = Int(a2a_ring_read(capture, &captured, UInt32(readable)))
        guard got > 0 else { continue }
        // Goertzel power at the probe frequency over the mono sum, plus plain RMS.
        var s0 = 0.0, s1 = 0.0, s2 = 0.0, energy = 0.0
        let coeff = 2.0 * cos(2.0 * Double.pi * frequency / rate)
        for i in 0..<(got / channels) {
            let value = (Double(captured[i * channels]) + Double(captured[i * channels + 1])) / 2
            s0 = value + coeff * s1 - s2
            s2 = s1; s1 = s0
            energy += value * value
        }
        let count = Double(got / channels)
        let magnitude = (s1 * s1 + s2 * s2 - coeff * s1 * s2).squareRoot() / count
        best.tone = max(best.tone, magnitude)
        best.rms = max(best.rms, (energy / count).squareRoot())
    }
    child?.terminate()
    return best
}

_ = listen(1.5, ownTone: false, otherProcess: false)      // let the device settle
let own = listen(phaseSeconds, ownTone: true, otherProcess: false)
let floor = listen(1.5, ownTone: false, otherProcess: false)
let control = listen(phaseSeconds, ownTone: false, otherProcess: true)
try? FileManager.default.removeItem(at: toneURL)

func db(_ value: Double) -> Double { value > 0 ? 20 * log10(value) : -120 }
let ownAboveFloor = db(own.tone) - db(floor.tone)
let controlAboveFloor = db(control.tone) - db(floor.tone)
// The control must be unmistakable, or the probe proves nothing (silent device, no output route).
let conclusive = controlAboveFloor > 12
let feedback = ownAboveFloor > controlAboveFloor * 0.2
let report: [String: Any] = [
    "frequency_hz": frequency, "rate": rate, "phase_seconds": phaseSeconds,
    "own_tone_db": db(own.tone), "floor_db": db(floor.tone), "control_tone_db": db(control.tone),
    "own_above_floor_db": ownAboveFloor, "control_above_floor_db": controlAboveFloor,
    "capture_rms_peak": own.rms, "conclusive": conclusive, "own_output_audible_to_tap": feedback,
    "source": ["bundle_id": source.bundleID, "global": source.global,
               "excluded_pids": source.excluded.map { $0.process.pid }],
]
let data = try! JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
if let jsonPath { try? data.write(to: URL(fileURLWithPath: jsonPath)) }
if !conclusive {
    let note = "inconclusive: the tone was not clearly audible even from another process "
        + "(check the output device and capture permission)"
    FileHandle.standardError.write(Data((note + "\n").utf8))
    exit(2)
}
let verdict = feedback
    ? "FAIL: our own rendered audio re-enters the global mix — the pipeline would translate itself"
    : "ok: our own output stays out of the global mix (control \(Int(controlAboveFloor)) dB up, ours \(Int(ownAboveFloor)) dB)"
FileHandle.standardError.write(Data((verdict + "\n").utf8))
exit(feedback ? 1 : 0)
