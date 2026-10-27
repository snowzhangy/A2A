// a2a-tapctl: process-tap capture, mute/re-render, and lifecycle tests (A1–A3).
//
//   a2a-tapctl permission
//   a2a-tapctl list [--all] [--json]
//   a2a-tapctl devices
//   a2a-tapctl resolve (--bundle ID | --pid N)
//   a2a-tapctl taps
//   a2a-tapctl destroy-tap --uid UID     (only for taps a killed a2a-tapctl left behind)
//   a2a-tapctl capture (--bundle ID | --pid N) --out x.wav [--seconds S] [--mute unmuted|muted|muted-when-tapped] [--trace t.jsonl]
//   a2a-tapctl monitor (--bundle ID | --pid N) [--seconds S] [--delay-ms D] [--crossfade-every S] [--trace t.jsonl]
//   a2a-tapctl fidelity --reference ref.wav --capture x.wav [--max-offset-seconds 30] [--window-seconds 10]
//   a2a-tapctl lifecycle (--bundle ID | --pid N) [--cycles 50] [--kill-cycles 5] [--json out.json]
//
// Capture and mute need the "System Audio Recording" permission for the responsible
// process (normally the terminal); `permission` reports it. Run hardware tests on the
// interactive runner.

import A2ACore
import A2AAudio
import A2AMedia
import A2ARTShim
import A2ATrace
import CoreAudio
import Darwin
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a2a-tapctl: \(message)\n".utf8))
    exit(1)
}

func info(_ message: String) {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
}

func printJSON(_ object: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

struct Args {
    var command = ""
    var values: [String: String] = [:]
    var flags: Set<String> = []

    init(_ argv: [String]) {
        guard argv.count > 1 else { return }
        command = argv[1]
        var i = 2
        while i < argv.count {
            let a = argv[i]
            if a.hasPrefix("--"), i + 1 < argv.count, !argv[i + 1].hasPrefix("--") {
                values[String(a.dropFirst(2))] = argv[i + 1]
                i += 2
            } else {
                flags.insert(String(a.dropFirst(2)))
                i += 1
            }
        }
    }

    func double(_ k: String, _ d: Double) -> Double { values[k].flatMap(Double.init) ?? d }
    func int(_ k: String, _ d: Int) -> Int { values[k].flatMap(Int.init) ?? d }
}

let args = Args(CommandLine.arguments)

func resolveSource() throws -> ResolvedSource {
    let processes = try AudioProcessList.current()
    if let pid = args.values["pid"].flatMap(Int32.init) {
        return try SourceResolver.resolve(pid: pid, processes: processes)
    }
    guard let bundle = args.values["bundle"] else { fail("pass --bundle ID or --pid N") }
    return try SourceResolver.resolve(bundleID: bundle, processes: processes)
}

func describe(_ source: ResolvedSource) -> [String: Any] {
    [
        "bundle_id": source.bundleID,
        "app_root": source.appRoot ?? NSNull(),
        "members": source.members.map { ["object_id": $0.process.objectID, "pid": $0.process.pid,
                                         "bundle_id": $0.process.bundleID, "path": $0.process.executablePath,
                                         "running_output": $0.process.isRunningOutput, "reason": $0.reason] },
        "excluded": source.excluded.map { ["pid": $0.process.pid, "reason": $0.reason] },
    ]
}

/// Runs a session, draining the capture ring (to WAV) and timing records (to trace).
func runSession(_ session: TapSession, seconds: Double, wav: WAVStreamWriter?, trace: TraceWriter?,
                every: ((Double) -> Void)? = nil) -> [String: Any] {
    let drain = session.recordRing.map { RTRecordDrain(ring: $0) }
    let sink: TraceSink = trace ?? MemoryTraceSink()
    let context = RTTraceContext(session: "tapctl", deviceRate: session.sampleRate)
    var buffer = [Float](repeating: 0, count: 8_192 * Int(session.channels))
    var captured = 0
    var peak: Float = 0
    var sumSquares = 0.0
    let start = Date()
    var lastTick = 0.0
    while true {
        let elapsed = Date().timeIntervalSince(start)
        if elapsed >= seconds { break }
        var n = session.readCapture(into: &buffer)
        while n > 0 {
            let count = n * Int(session.channels)
            buffer.withUnsafeBufferPointer { wav?.append(UnsafeBufferPointer(rebasing: $0[0..<count])) }
            for i in 0..<count {
                peak = max(peak, abs(buffer[i]))
                sumSquares += Double(buffer[i] * buffer[i])
            }
            captured += n
            n = session.readCapture(into: &buffer)
        }
        if trace != nil { drain?.drain(context: context, into: sink) }
        if elapsed - lastTick >= 0.1 {
            lastTick = elapsed
            every?(elapsed)
        }
        usleep(5_000)
    }
    if trace != nil { drain?.drain(context: context, into: sink) }
    let c = session.counters
    let rms = captured > 0 ? (sumSquares / Double(captured * Int(session.channels))).squareRoot() : 0
    return [
        "seconds": seconds, "captured_frames": captured, "expected_frames": Int(seconds * session.sampleRate),
        "callbacks": c.callbacks, "output_frames": c.outputFrames,
        "capture_overflow_frames": c.captureOverflowFrames, "rejected_blocks": c.rejectedBlocks,
        "record_drops": c.recordDrops, "invalid_timestamps": drain?.invalidTimestamps ?? -1,
        "peak": Double(peak), "rms_dbfs": rms > 0 ? 20 * log10(rms) : -999,
        "silent_capture": peak == 0,
        "latency": session.latencyReport, "tap_muted": session.tapIsMuted,
        "input_channel_offset": session.inputChannelOffset,
    ]
}

/// AudioDeviceStart blocks inside coreaudiod until the user answers the System Audio
/// Recording consent prompt. Say so instead of hanging silently.
func startWithConsentHint(_ session: TapSession) throws {
    switch CapturePermission.status() {
    case .denied:
        fail("System Audio Recording is denied for the responsible app (the terminal that launched this tool). "
             + "Enable it in System Settings › Privacy & Security › Screen & System Audio Recording, "
             + "or run `tccutil reset AudioCapture` to be asked again")
    case .undetermined:
        info("System Audio Recording is not decided yet: macOS should now ask for the responsible app")
    case .granted, .unknown:
        break
    }
    let started = Locked(false)
    DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
        if !started.value {
            info("waiting for AudioDeviceStart: approve System Audio Recording for the responsible app "
                 + "(System Settings › Privacy & Security › Screen & System Audio Recording)")
        }
    }
    try session.start()
    started.value = true
}

final class Locked<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var v: T
    init(_ v: T) { self.v = v }
    var value: T {
        get { lock.lock(); defer { lock.unlock() }; return v }
        set { lock.lock(); v = newValue; lock.unlock() }
    }
}

func sessionOptions(mute: TapMute, render: Bool) -> TapSessionOptions {
    var o = TapSessionOptions()
    o.mute = mute
    o.renderOutput = render
    o.delayMs = args.int("delay-ms", 0)
    return o
}

do {
    switch args.command {
    case "permission":
        printJSON(["audio_capture": CapturePermission.status().rawValue,
                   "note": "status of the responsible app (the terminal or app that launched this tool)"])

    case "list":
        let processes = try AudioProcessList.current()
        let shown = args.flags.contains("all") ? processes : processes.filter { $0.isRunningOutput || $0.isRunningInput }
        if args.flags.contains("json") {
            printJSON(shown.map { ["object_id": $0.objectID, "pid": $0.pid, "bundle_id": $0.bundleID,
                                   "running_output": $0.isRunningOutput, "running_input": $0.isRunningInput,
                                   "path": $0.executablePath, "ppid": $0.parentPID] })
        } else {
            for p in shown { print(p) }
            info("\(shown.count) of \(processes.count) audio processes (use --all for every process object)")
        }

    case "devices":
        let defaultID = try AudioDevice.defaultOutput().id
        for d in try AudioDevice.all() {
            let outs = d.channelCount(scope: kAudioObjectPropertyScopeOutput)
            let ins = d.channelCount(scope: kAudioObjectPropertyScopeInput)
            print("\(d.id == defaultID ? "*" : " ") \(d) in=\(ins) out=\(outs) buffer=\(d.bufferFrameSize) "
                  + "latency_out=\(d.latencyFrames(scope: kAudioObjectPropertyScopeOutput)) "
                  + "safety_out=\(d.safetyOffsetFrames(scope: kAudioObjectPropertyScopeOutput))")
        }

    case "resolve":
        printJSON(describe(try resolveSource()))

    case "taps":
        // Live process taps system-wide; after a host crash its tap must disappear.
        printJSON(TapList.current().map { ["object_id": $0, "uid": TapList.uid($0)] })

    case "destroy-tap":
        guard let uid = args.values["uid"] else { fail("destroy-tap needs --uid UID") }
        guard let status = TapList.destroy(uid: uid) else { fail("no live tap with uid \(uid)") }
        printJSON(["uid": uid, "status": Int(status), "released": waitForRelease(uid, seconds: 2) != nil])

    case "capture":
        guard let out = args.values["out"] else { fail("capture needs --out x.wav") }
        let muteName = args.values["mute"] ?? TapMute.unmuted.rawValue
        guard let mute = TapMute(rawValue: muteName) else {
            fail("unknown --mute \(muteName); use \(TapMute.allCases.map(\.rawValue).joined(separator: "|"))")
        }
        let source = try resolveSource()
        let session = try TapSession(source: source, options: sessionOptions(mute: mute, render: false))
        let wav = try WAVStreamWriter(url: URL(fileURLWithPath: out), sampleRate: Int(session.sampleRate),
                                      channels: Int(session.channels))
        let trace = try args.values["trace"].map { try TraceWriter(url: URL(fileURLWithPath: $0)) }
        try startWithConsentHint(session)
        var report = runSession(session, seconds: args.double("seconds", 5), wav: wav, trace: trace)
        session.teardown()
        try wav.close()
        trace?.close()
        report["source"] = describe(source)
        report["out"] = out
        if report["silent_capture"] as? Bool == true {
            report["warning"] = "all captured samples are zero: the source was silent or capture permission is missing"
        }
        printJSON(report)

    case "monitor":
        // A2: mute the source while tapped and render everything through our mixer.
        let source = try resolveSource()
        let session = try TapSession(source: source, options: sessionOptions(mute: .mutedWhenTapped, render: true))
        let trace = try args.values["trace"].map { try TraceWriter(url: URL(fileURLWithPath: $0)) }
        let wav = try args.values["out"].map {
            try WAVStreamWriter(url: URL(fileURLWithPath: $0), sampleRate: Int(session.sampleRate), channels: Int(session.channels))
        }
        session.setGains(original: 1, translated: 0, rampMs: 0)
        try startWithConsentHint(session)
        let every = args.double("crossfade-every", 0)
        var lastToggle = 0.0
        var originalOn = true
        var toggles = 0
        var report = runSession(session, seconds: args.double("seconds", 10), wav: wav, trace: trace) { t in
            guard every > 0, t - lastToggle >= every else { return }
            lastToggle = t
            originalOn.toggle()
            toggles += 1
            session.setGains(original: originalOn ? 1 : 0, translated: 0, rampMs: 30)
        }
        session.teardown()
        try wav?.close()
        trace?.close()
        report["crossfade_toggles"] = toggles
        report["source"] = describe(source)
        printJSON(report)

    case "fidelity":
        // A1 DoD: align a capture of FixturePlayer --probe against the reference signal.
        guard let refPath = args.values["reference"], let capPath = args.values["capture"] else {
            fail("fidelity needs --reference probe.wav --capture capture.wav")
        }
        let ref = try WAV.read(URL(fileURLWithPath: refPath))
        let cap = try WAV.read(URL(fileURLWithPath: capPath))
        var refMono = ref.mono()
        if ref.sampleRate != cap.sampleRate {
            refMono = StreamingResampler.resample(refMono, from: ref.sampleRate, to: cap.sampleRate)
        }
        let capMono = cap.mono()
        let probe = min(cap.sampleRate, capMono.count / 4)..<min(cap.sampleRate * 2, capMono.count / 2)
        // The capture starts at an unknown point near the start of the reference: find where
        // its second second lands. A lost or duplicated block later shifts the alignment, which
        // the per-window correlations below expose.
        let maxOffset = Int(args.double("max-offset-seconds", 30) * Double(cap.sampleRate))
        let search = Signal.bestLag(reference: capMono, signal: refMono, probe: probe,
                                    maxLag: min(maxOffset, max(0, refMono.count - probe.upperBound)))
        let offset = search.lag
        let n = max(0, min(capMono.count, refMono.count - offset))
        let corr = n > 0 ? Signal.correlation(capMono[0..<n], refMono[offset..<offset + n]) : 0
        let window = max(1, Int(args.double("window-seconds", 10) * Double(cap.sampleRate)))
        var windows: [Double] = []
        for start in stride(from: 0, to: n - window + 1, by: window) {
            windows.append(Signal.correlation(capMono[start..<start + window], refMono[offset + start..<offset + start + window]))
        }
        let weak = windows.enumerated().filter { $0.element <= 0.99 }
        let dropouts = Signal.zeroRuns(Array(capMono[0..<n]), minRun: 32)
        printJSON(["correlation": corr, "aligned_frames": n, "capture_frames": capMono.count,
                   "unaligned_capture_frames": capMono.count - n, "reference_offset_frames": offset,
                   "sample_rate": cap.sampleRate, "windows": windows.count,
                   "min_window_correlation": windows.min() ?? NSNull(),
                   "windows_at_or_below_0_99": weak.map { ["index": $0.offset, "start_s": Double($0.offset * window) / Double(cap.sampleRate),
                                                           "correlation": $0.element] },
                   "dropouts": dropouts.count, "dropout_frames": dropouts.reduce(0) { $0 + $1.count },
                   "pass": corr > 0.99 && weak.isEmpty && dropouts.isEmpty && n == capMono.count && n > 0])

    case "lifecycle":
        try lifecycle()

    case "_hold":
        // Child process for crash tests: hold a muted tap until killed. The tap is public so
        // the supervising process can see it in kAudioHardwarePropertyTapList.
        let source = try resolveSource()
        var options = sessionOptions(mute: .mutedWhenTapped, render: true)
        options.privateTap = false
        let session = try TapSession(source: source, options: options)
        try startWithConsentHint(session)
        print(session.tapUUID.uuidString)
        fflush(stdout)
        RunLoop.main.run(until: Date().addingTimeInterval(args.double("seconds", 30)))
        session.teardown()

    default:
        print("""
            usage: a2a-tapctl permission|list|devices|resolve|taps|capture|monitor|fidelity|lifecycle [options]
              see the header of Tools/a2a-tapctl/main.swift
            """)
        exit(args.command.isEmpty ? 0 : 2)
    }
} catch {
    fail("\(error)")
}

// MARK: Lifecycle (A3)

func tapUIDs() -> Set<String> { Set(TapList.current().map { TapList.uid($0).uppercased() }) }

/// Milliseconds until `uid` leaves the system tap list, or nil if it is still there after `seconds`.
/// Tap destruction is asynchronous in coreaudiod: an immediate check races it.
func waitForRelease(_ uid: String, seconds: Double, since start: Date = Date()) -> Int? {
    let uid = uid.uppercased()
    while true {
        if !tapUIDs().contains(uid) { return Int(Date().timeIntervalSince(start) * 1_000) }
        if Date().timeIntervalSince(start) >= seconds { return nil }
        usleep(5_000)
    }
}

func lifecycle() throws {
    let cycles = args.int("cycles", 50)
    let killCycles = args.int("kill-cycles", 5)
    let source = try resolveSource()
    let baseline = tapUIDs()
    var rows: [[String: Any]] = []

    for i in 0..<cycles {
        let t0 = Date()
        let session = try TapSession(source: source, options: sessionOptions(mute: .mutedWhenTapped, render: true))
        try startWithConsentHint(session)
        usleep(200_000)
        let callbacks = session.counters.callbacks
        let uid = session.tapUUID.uuidString.uppercased()
        let visible = tapUIDs().contains(uid)
        session.teardown()
        let releasedMs = waitForRelease(uid, seconds: 2)
        rows.append(["cycle": i, "kind": "stop_start", "callbacks": callbacks, "tap_visible_before_teardown": visible,
                     "tap_released_ms": releasedMs ?? NSNull(), "tap_leaked": releasedMs == nil || !visible,
                     "ms": Int(Date().timeIntervalSince(t0) * 1_000)])
    }

    let me = CommandLine.arguments[0]
    var target = ["--bundle", source.bundleID]
    if let pid = args.values["pid"] { target = ["--pid", pid] }
    for i in 0..<killCycles {
        let child = Process()
        child.executableURL = URL(fileURLWithPath: me)
        child.arguments = ["_hold"] + target + ["--seconds", "30"]
        let pipe = Pipe()
        child.standardOutput = pipe
        try child.run()
        let line = String(decoding: pipe.fileHandleForReading.readData(ofLength: 37), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        usleep(500_000)
        let presentBefore = tapUIDs().contains(line)
        let killedAt = Date()
        kill(child.processIdentifier, SIGKILL)
        child.waitUntilExit()
        // The dead host cannot restore sound; verify the OS destroys its tap (and mute).
        let releasedMs = waitForRelease(line, seconds: 5, since: killedAt)
        // If it does not, record whether a separate supervisor process can (a public tap only).
        var supervisorStatus: Any = NSNull()
        var supervisorReleased: Any = NSNull()
        if releasedMs == nil, let status = TapList.destroy(uid: line) {
            supervisorStatus = Int(status)
            supervisorReleased = waitForRelease(line, seconds: 2) != nil
        }
        let sourceStillRunning = (try? AudioProcessList.current())?.contains { p in
            source.members.contains { $0.process.pid == p.pid } && p.isRunningOutput
        } ?? false
        rows.append(["cycle": i, "kind": "kill_9_while_muted", "tap_uid": line, "tap_present_before_kill": presentBefore,
                     "tap_released_ms": releasedMs ?? NSNull(), "stranded": releasedMs == nil,
                     "supervisor_destroy_status": supervisorStatus, "released_by_supervisor": supervisorReleased,
                     "source_running_output": sourceStillRunning])
    }

    let after = tapUIDs()
    let stranded = rows.filter { ($0["stranded"] as? Bool) == true || ($0["tap_leaked"] as? Bool) == true }.count
    // A tap the supervisor never saw proves nothing about its release: fail loudly.
    let unobserved = rows.filter { $0["kind"] as? String == "kill_9_while_muted"
        && ($0["tap_present_before_kill"] as? Bool) != true }.count
    let supervisorRecovered = rows.filter { ($0["released_by_supervisor"] as? Bool) == true }.count
    let report: [String: Any] = [
        "source": describe(source), "cycles": cycles, "kill_cycles": killCycles,
        "leftover_taps": Array(after.subtracting(baseline)), "stranded_or_leaked": stranded,
        "kill_rows_unobserved": unobserved, "kill_rows_recovered_by_supervisor": supervisorRecovered,
        "pass": stranded == 0 && unobserved == 0 && after.subtracting(baseline).isEmpty,
        "note": "Kill rows use a public tap so the supervisor can see it; the product uses private taps. "
            + "Tap destruction is the OS-level evidence; audible restoration still needs the loopback check (A3).",
        "rows": rows,
    ]
    if let path = args.values["json"] {
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: URL(fileURLWithPath: path))
    }
    printJSON(report.filter { $0.key != "rows" })
}
