// a2a-pipeline: WAV or paced stdin in → translated WAV + trace out (H1/M4/E0 driver).
//
// The driver owns I/O and adapters; SessionPipeline (A2ACore) makes every decision.
// Output is rendered through the same A2ARTShim mixer the live host uses.
//
//   --input clip.wav | --stdin [--rate 48000]       source audio (mono; stdin is f32le)
//   --tap-global | --tap-bundle ID | --tap-pid N [--seconds S]
//                                                    live: tap the whole output mix (global) or one app
//                                                    (muted while tapped) and play the translation on the
//                                                    default output
//   --source-out src.wav --translated-out tr.wav     what the pipeline heard / translated audio
//                                                    the mixer consumed (flushed audio excluded)
//   --max-debt-seconds D --max-source-lag-seconds L  health thresholds (default 3 / 4)
//   --max-unit-keys N                                commit-policy forced-split length (experiment
//                                                    parameter recorded in the summary; default 60)
//   --max-unit-seconds S                             source seconds a unit may accumulate before it
//                                                    closes at its last safe clause boundary
//                                                    (0 waits for finalization; default 4)
//   --clock virtual|host                             virtual: time from samples (fixtures only)
//   --pace realtime|asap                             file input pacing (host clock)
//   --asr scripted|audio [--asr-script x.json]
//   --mt dictionary|hy [--mt-dictionary x.json]
//   --tts tone|system|clone
//   --profile-ready-ms N                             0 = prepared profile at arm (tone/system only)
//   --clone-mode icl|xvector                         clone conditioning (default icl)
//   --clone-delivery pinned|line                     speak each line on the actor's own delivery of
//                                                  that line (ADR-0015); research knob, off by default
//   --voice-ref ref.wav [--voice-ref-text ref.txt]   clone: fixed reference prepared before capture;
//                                                    otherwise the reference is acquired from the
//                                                    source's first finalized utterances
//   --clone-python PATH --clone-worker PATH          clone worker (default: research/.venv and
//                                                    research/tts_worker.py above this executable)
//   --clone-fake                                     standard-library tone worker (tests)
//   --trace out.jsonl --out out.wav [--stems stems.wav]
//   --session ID --research-text --tail-seconds S --vad-threshold-db DB
//   --playback original|translated --control-stdin   live selection; stdin commands are mode names, one per line
//   --fluent                                         app playout: streaming clauses, speech paced to the source,
//                                                    phrases never cut; soft pressure skips unheard units
//   --presentation-delay-ms D                        with --fluent: the host shows the picture D ms late; each
//                                                    phrase starts at its source start + D, the original bus and
//                                                    --background bed are delayed by D, lag limits count from the slot
//   --glossary terms.json                            {"source term": "target term"}: replaced before contextual translation
//   --pronounce words.json                           {"word": "respelling"}: spoken form only; captions unchanged
//   --speakers single|auto                           auto: track speakers so each gets their own voice (system:
//                                                    an installed voice cast by pitch; clone: a reference cut
//                                                    from that speaker's speech); --profile-ready-ms is unused

import A2AAudio
import A2ACore
import A2AMedia
import A2AModels
import A2ARTShim
import A2ATrace
import CoreAudio
import Darwin
import Foundation

/// Direct audio translation does not instantiate a speech recognizer.
final class AudioOnlyRecognizer: StreamingRecognizer {
    func accept(_ frame: AudioFrame16k) {}
    func requestFinalize() {}
    func drainHypotheses() -> [ASRHypothesis] { [] }
    func reset() {}
}

// MARK: Arguments

struct Options {
    var input: String?
    var stdin = false
    var rate = 48_000
    var clock = "virtual"
    var pace = "asap"
    var asr = "scripted"
    var asrScript: String?
    var mt = "dictionary"
    var mtDictionary: String?
    var hyServer = "http://127.0.0.1:8082"
    var hyWorker: String?
    var hyModel: String?
    var qwenCloud = false
    var cloudRegion = "ap-southeast-1"
    var tts = "tone"
    var direction = TranslationDirection.chineseToEnglish
    var profileReadyMs = 0
    var cloneMode = CloneConditioning.icl
    var cloneModel = ExperimentalVoiceModel.qwen
    var voiceMemory: String?
    var voiceRef: String?
    var voiceRefText: String?
    var clonePython: String?
    var cloneWorker: String?
    var referenceTranscriber: String?
    var cloneFake = false
    var dualClone = false
    var isolateReferences = true
    var sentenceUnits = true
    var passthrough = true
    var lidModel: String?
    var trace = "trace.jsonl"
    var out: String?
    var stems: String?
    var session = String(UUID().uuidString.prefix(8)).lowercased()
    var researchText = false
    var tailSeconds = 15.0
    var vadThresholdDB: Float = -40
    var tapBundle: String?
    var tapPid: Int32?
    var tapGlobal = false
    var seconds: Double?
    var sourceOut: String?
    var translatedOut: String?
    var renderedOut: String?
    var maxDebtSeconds: Double?
    var maxSourceLagSeconds: Double?
    var maxUnitKeys: Int?
    var maxUnitSeconds: Double?
    var speakerCarry: Double?
    var speakerCarryPause: Double?
    var speakerCarryPiece: Double?
    var shorten = true
    /// --mt hy-local with --asr audio: stream each span to the worker while it is spoken.
    var hyStream = true
    var fillerSilence = true
    var wordCuts = true
    var captionsOnly = false
    var translationOnly = false
    var eventsStdout = false
    var playbackMode = PlaybackMode.translated
    var controlStdin = false
    var fluent = false
    var adaptivePrebuffer = false
    var qualityFirst = false
    var background = false
    /// Picture delay D (ADR-0012): the host shows the source picture this late; 0 = live.
    var presentationDelayMs = 0
    /// Turn fill (ADR-0013): a take shorter than its speech is slowed to end with the actor.
    /// Only ever applies with a picture delay; `--no-turn-fill` turns it off for an A/B listen.
    var turnFill = true
    /// "line" conditions each cloned take on the actor's own audio of that line (ADR-0015).
    var cloneDelivery = "pinned"
    /// Test only (virtual clock): no capture for `seconds` once the source reaches `at`, as when
    /// a paused video leaves the global tap without callbacks.
    var captureGap: (at: Double, seconds: Double)?
    var backgroundOut: String?
    var contextPython: String?
    var contextWorker: String?
    var contextSeries: String?
    var speakers = "single"
    var glossary: String?
    var pronounce: String?
    var parentPID: Int32?

    var isTap: Bool { tapBundle != nil || tapPid != nil || tapGlobal }
    /// Hy-SimulMT translates source audio directly: on a server (`hy`) or on this Mac (`hy-local`).
    var translatesAudio: Bool { mt == "hy" || mt == "hy-local" }

    static func parse(_ args: [String]) -> Options {
        var o = Options()
        var clockSet = false, paceSet = false
        var i = 1
        func value() -> String {
            i += 1
            guard i < args.count else { fail("missing value for \(args[i - 1])") }
            return args[i]
        }
        func integer() -> Int {
            let raw = value()
            guard let result = Int(raw) else { fail("expected an integer for \(args[i - 1])") }
            return result
        }
        func decimal() -> Double {
            let raw = value()
            guard let result = Double(raw) else { fail("expected a number for \(args[i - 1])") }
            return result
        }
        func pid() -> Int32 {
            let raw = integer()
            guard let result = Int32(exactly: raw), result > 1 else { fail("invalid process ID") }
            return result
        }
        while i < args.count {
            switch args[i] {
            case "--input": o.input = value()
            case "--stdin": o.stdin = true
            case "--rate": o.rate = integer()
            case "--clock": o.clock = value(); clockSet = true
            case "--pace": o.pace = value(); paceSet = true
            case "--asr": o.asr = value()
            case "--asr-script": o.asrScript = value()
            case "--mt": o.mt = value()
            case "--hy-server": o.hyServer = value()
            case "--hy-worker": o.hyWorker = value()
            case "--hy-model": o.hyModel = value()
            case "--qwen-cloud": o.qwenCloud = true
            case "--cloud-region": o.cloudRegion = value()
            case "--context-python": o.contextPython = value()
            case "--context-worker": o.contextWorker = value()
            case "--context-series": o.contextSeries = value()
            case "--mt-dictionary": o.mtDictionary = value()
            case "--speaker-carry": o.speakerCarry = decimal()
            case "--speaker-carry-pause": o.speakerCarryPause = decimal()
            case "--speaker-carry-piece": o.speakerCarryPiece = decimal()
            case "--no-shorten": o.shorten = false
            case "--no-hy-stream": o.hyStream = false
            case "--no-filler-silence": o.fillerSilence = false
            case "--tts": o.tts = value()
            case "--direction":
                guard let direction = TranslationDirection(rawValue: value()) else { fail("--direction is zh-en or en-zh") }
                o.direction = direction
            case "--profile-ready-ms": o.profileReadyMs = integer()
            case "--clone-mode":
                guard let mode = CloneConditioning(rawValue: value()) else { fail("--clone-mode is icl or xvector") }
                o.cloneMode = mode
            case "--clone-model":
                guard let model = ExperimentalVoiceModel(rawValue: value()) else { fail("--clone-model is qwen, audio8, omnivoice, or chatterbox") }
                o.cloneModel = model
            case "--voice-memory": o.voiceMemory = value()
            case "--glossary": o.glossary = value()
            case "--pronounce": o.pronounce = value()
            case "--voice-ref": o.voiceRef = value()
            case "--voice-ref-text": o.voiceRefText = value()
            case "--clone-python": o.clonePython = value()
            case "--clone-worker": o.cloneWorker = value()
            case "--reference-transcriber": o.referenceTranscriber = value()
            case "--clone-fake": o.cloneFake = true
            case "--dual-clone": o.dualClone = true
            case "--no-reference-isolation": o.isolateReferences = false
            case "--no-sentence-units": o.sentenceUnits = false
            case "--no-passthrough": o.passthrough = false
            case "--lid-model": o.lidModel = value()
            case "--trace": o.trace = value()
            case "--out": o.out = value()
            case "--stems": o.stems = value()
            case "--session": o.session = value()
            case "--research-text": o.researchText = true
            case "--tail-seconds": o.tailSeconds = decimal()
            case "--vad-threshold-db": o.vadThresholdDB = Float(decimal())
            case "--tap-global": o.tapGlobal = true
            case "--tap-bundle": o.tapBundle = value()
            case "--tap-pid": o.tapPid = pid()
            case "--seconds": o.seconds = decimal()
            case "--source-out": o.sourceOut = value()
            case "--translated-out": o.translatedOut = value()
            case "--rendered-out": o.renderedOut = value()
            case "--max-debt-seconds": o.maxDebtSeconds = decimal()
            case "--max-source-lag-seconds": o.maxSourceLagSeconds = decimal()
            case "--max-unit-keys": o.maxUnitKeys = integer()
            case "--max-unit-seconds": o.maxUnitSeconds = decimal()
            case "--no-word-cuts": o.wordCuts = false
            case "--captions-only": o.captionsOnly = true
            case "--translation-only": o.translationOnly = true
            case "--events-stdout": o.eventsStdout = true
            case "--playback":
                guard let mode = PlaybackMode(rawValue: value()) else { fail("--playback is original or translated") }
                o.playbackMode = mode
            case "--control-stdin": o.controlStdin = true
            case "--fluent": o.fluent = true
            case "--adaptive-prebuffer": o.adaptivePrebuffer = true
            case "--quality-first": o.qualityFirst = true
            case "--background": o.background = true
            case "--simulate-capture-gap":
                let parts = value().split(separator: ":").compactMap { Double($0) }
                guard parts.count == 2, parts[0] >= 0, parts[1] > 0, parts[1] <= 600 else { fail("--simulate-capture-gap takes AT:SECONDS") }
                o.captureGap = (parts[0], parts[1])
            case "--presentation-delay-ms":
                guard let ms = Int(value()), (0...30_000).contains(ms) else { fail("--presentation-delay-ms takes 0–30000") }
                o.presentationDelayMs = ms
            case "--no-turn-fill": o.turnFill = false
            case "--clone-delivery":
                let mode = value()
                guard ["pinned", "line"].contains(mode) else { fail("--clone-delivery is pinned or line") }
                o.cloneDelivery = mode
            case "--background-out": o.backgroundOut = value()
            case "--speakers": o.speakers = value()
            case "--parent-pid": o.parentPID = pid()
            case "-h", "--help":
                print(usage)
                exit(0)
            default: fail("unknown argument \(args[i])\n\(usage)")
            }
            i += 1
        }
        if o.qualityFirst { o.fluent = true }
        if o.qwenCloud && (!o.isTap || !o.controlStdin || !o.translationOnly || !["ap-southeast-1", "cn-beijing"].contains(o.cloudRegion)) {
            fail("Qwen Cloud requires a live tap, --control-stdin, --translation-only, and a supported region")
        }
        if o.backgroundOut != nil && !o.background { fail("--background-out requires --background") }
        if o.captureGap != nil && (o.input == nil || o.clock != "virtual") { fail("--simulate-capture-gap needs --input and --clock virtual") }
        // Slot playout holds PCM until its slot, which only fluent playout does.
        if o.presentationDelayMs > 0 && !o.fluent { fail("--presentation-delay-ms requires --fluent") }
        if o.stdin || o.isTap {
            if !clockSet { o.clock = "host" }
            if !paceSet { o.pace = "realtime" }
        }
        if [o.stdin, o.input != nil, o.isTap].filter({ $0 }).count != 1 {
            fail("pass exactly one of --input, --stdin, or --tap-bundle/--tap-pid")
        }
        if o.isTap, o.clock != "host" { fail("--tap needs --clock host") }
        guard ["virtual", "host"].contains(o.clock), ["realtime", "asap"].contains(o.pace),
              ["scripted", "audio"].contains(o.asr), ["dictionary", "context", "hy", "hy-local"].contains(o.mt),
              ["tone", "system", "clone"].contains(o.tts) else { fail("invalid adapter, clock, or pace") }
        guard ["single", "auto"].contains(o.speakers) else { fail("--speakers is single or auto") }
        if o.speakers == "auto", o.voiceRef != nil { fail("--voice-ref fixes one voice; --speakers auto learns one per speaker") }
        guard (1...192_000).contains(o.rate), o.tailSeconds.isFinite, (0...120).contains(o.tailSeconds), o.profileReadyMs >= 0,
              o.vadThresholdDB.isFinite, (-120...0).contains(o.vadThresholdDB),
              o.seconds.map({ $0.isFinite && $0 > 0 && $0 <= 86_400 }) ?? true,
              o.maxDebtSeconds.map({ $0.isFinite && $0 > 0 && $0 <= 60 }) ?? true,
              o.maxSourceLagSeconds.map({ $0.isFinite && $0 > 0 && $0 <= 120 }) ?? true,
              o.maxUnitKeys.map({ (4...200).contains($0) }) ?? true,
              o.maxUnitSeconds.map({ $0.isFinite && $0 >= 0 && $0 <= 60 }) ?? true,
              o.speakerCarry.map({ $0.isFinite && $0 >= 0 && $0 <= 60 }) ?? true,
              o.speakerCarryPause.map({ $0.isFinite && $0 >= 0 && $0 <= 60 }) ?? true,
              o.speakerCarryPiece.map({ $0.isFinite && $0 > 0 && $0 <= 30 }) ?? true
        else { fail("invalid duration, threshold, or profile delay") }
        if [o.tapBundle != nil, o.tapPid != nil, o.tapGlobal].filter({ $0 }).count > 1 {
            fail("choose one of --tap-global, --tap-bundle or --tap-pid")
        }
        if o.captionsOnly, o.translationOnly { fail("--captions-only and --translation-only are mutually exclusive") }
        if o.controlStdin, o.stdin { fail("--control-stdin cannot share stdin with audio") }
        if o.captionsOnly, o.controlStdin || o.playbackMode == .original {
            fail("playback selection requires the audio mixer, not --captions-only")
        }
        if o.tts != "clone", o.voiceRef != nil || o.voiceRefText != nil || o.clonePython != nil || o.cloneWorker != nil || o.referenceTranscriber != nil || o.cloneFake || o.cloneModel != .qwen || o.voiceMemory != nil {
            fail("--voice-ref and --clone-* options need --tts clone")
        }
        if o.dualClone && (o.tts != "clone" || o.cloneModel != .qwen || !o.fluent || o.clock != "host") {
            fail("--dual-clone requires --tts clone --clone-model qwen --fluent --clock host")
        }
        if o.voiceRefText != nil, o.voiceRef == nil { fail("--voice-ref-text needs --voice-ref") }
        if o.cloneDelivery != "pinned", o.tts != "clone" { fail("--clone-delivery conditions cloned voices; use it with --tts clone") }
        if o.asr == "audio" && (!o.translatesAudio || (o.tts == "clone" && o.cloneMode != .xvector) || o.voiceMemory != nil) {
            fail("--asr audio requires --mt hy or hy-local, audio-only reference conditioning for clone, and no voice memory")
        }
        if o.mt != "hy-local", o.hyWorker != nil || o.hyModel != nil { fail("--hy-worker and --hy-model need --mt hy-local") }
        if o.voiceRef != nil, o.cloneMode == .icl, o.voiceRefText == nil {
            fail("--clone-mode icl needs --voice-ref-text with the reference's exact transcript (or use --clone-mode xvector)")
        }
        if o.isTap, o.out != nil || o.stems != nil { fail("--out/--stems render offline; with --tap use --translated-out") }
        if o.clock == "virtual" {
            if o.stdin { fail("--clock virtual needs --input (stdin is paced by the harness in host time)") }
            if o.asr != "scripted" || o.mt != "dictionary" || o.tts != "tone" {
                fail("--clock virtual only models fixture adapters; real adapters need --clock host")
            }
        }
        return o
    }
}

let usage = """
    usage: a2a-pipeline (--input clip.wav | --stdin [--rate 48000] | --tap-global | --tap-bundle ID | --tap-pid N [--seconds S])
                        [--clock virtual|host] [--pace realtime|asap] [--source-out s.wav] [--translated-out t.wav]
                        [--max-debt-seconds D] [--max-source-lag-seconds L] [--max-unit-keys N]
                        [--max-unit-seconds S (source seconds before a unit closes at its last safe boundary; 0 waits for finalization)]
                        [--no-word-cuts (let a cut fall inside a word; default keeps cuts off words via the system segmenter)]
                        [--speaker-carry S (seconds an unlabeled piece may inherit the previous speaker inside one uninterrupted turn; 0 off)]
                        [--speaker-carry-pause S (…and how close after a pause a brief unlabeled piece may inherit them; default 3)]
                        [--speaker-carry-piece S (…and the longest piece that may inherit across a pause; default 1)]
                        [--no-shorten (speak the full target when it passes the hard length limit)]
                        [--no-filler-silence (count empty or filler answers as failed translations)]
                        [--asr scripted|audio --asr-script x.json] [--mt dictionary|hy|hy-local --mt-dictionary x.json]
                        [--tts tone|system|clone] [--profile-ready-ms N] --trace out.jsonl [--out out.wav] [--stems s.wav]
                        [--mt context --context-python PYTHON --context-worker FILE [--context-series LOCAL_JSON]]
                        [--mt hy --hy-server http://HOST:8082 (sends source audio to the server)]
                        [--mt hy-local [--hy-worker PATH] [--hy-model GGUF] (Hy-SimulMT on this Mac; research-only model)]
                        [--no-hy-stream (hy-local: send each span only once it closes, instead of while it is spoken)]
                        [--direction zh-en|en-zh (default zh-en)]
                        [--clone-model qwen|audio8|omnivoice|chatterbox] [--clone-mode icl|xvector]
                        [--voice-ref ref.wav [--voice-ref-text ref.txt]]
                        [--voice-memory DIR (opt-in private reference store; matched by voice)]
                        [--clone-python PATH --clone-worker PATH | --clone-fake]
                        [--reference-transcriber PATH (local transcript for Audio8/OmniVoice)]
                        [--dual-clone (experimental two-worker Qwen prefetch)]
                        [--no-reference-isolation (audio-only clone: keep music and effects in the acquired reference)]
                        [--no-sentence-units (direct audio with a picture delay: cut spans every 3 s on the mix, as before ADR-0017)]
                        [--no-passthrough (ADR-0018: never put the actor's own voice back) [--lid-model DIR]]
                        [--session ID] [--research-text] [--tail-seconds S] [--vad-threshold-db DB]
                        [--captions-only | --translation-only] [--events-stdout (in-memory captions and state JSONL)]
                        [--playback original|translated] [--control-stdin (one playback mode per line)]
                        [--fluent (streaming clauses, adaptive speech pace; catch-up skips unheard phrases, never cuts one)]
                        [--presentation-delay-ms D (with --fluent: phrases start at source start + D to match a delayed picture)]
                        [--no-turn-fill (with a picture delay: never slow a short line to end with its actor)]
                        [--adaptive-prebuffer (experimental throughput-based streaming reserve)]
                        [--quality-first (complete target and phrase when possible; 20-second debt/lag budget)]
                        [--background (experimental system sound isolation for music/effects) [--background-out FILE]]
                        [--speakers single|auto (dialogue: one voice per detected speaker)]
                        [--glossary terms.json (context MT)] [--pronounce words.json (spoken text only)]
                        [--rendered-out post-gain-device-output.wav]
    """

/// A JSON object of term → replacement (--glossary, --pronounce).
func loadTerms(_ path: String) throws -> [String: String] {
    let data = try Data(contentsOf: URL(fileURLWithPath: path))
    guard let terms = try JSONSerialization.jsonObject(with: data) as? [String: String] else {
        fail("\(path): expected a JSON object of strings")
    }
    return terms
}

/// The live tap, once created: every exit path destroys it so the source never stays muted.
nonisolated(unsafe) var tapToRelease: TapSession?

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a2a-pipeline: \(message)\n".utf8))
    tapToRelease?.teardown()
    exit(2)
}

func log(_ message: String) {
    FileHandle.standardError.write(Data("a2a-pipeline: \(message)\n".utf8))
}

// MARK: Source

/// A bounded, latest-value control channel, read independently of model warm-up.
/// It never runs on an audio callback and cannot queue an unbounded sequence of toggles.
final class PlaybackControls: Sendable {
    private let latest = Locked<PlaybackMode?>(nil)

    init() {
        let latest = self.latest
        Thread {
            var bytes = [UInt8](repeating: 0, count: 256)
            var line: [UInt8] = []
            line.reserveCapacity(32)
            var oversized = false
            while true {
                let count = bytes.withUnsafeMutableBytes { read(STDIN_FILENO, $0.baseAddress!, $0.count) }
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { return }
                for byte in bytes.prefix(count) {
                    if byte == 10 {
                        if !oversized, let mode = PlaybackMode(rawValue: String(decoding: line, as: UTF8.self)) {
                            latest.withLock { $0 = mode }
                        }
                        line.removeAll(keepingCapacity: true)
                        oversized = false
                    } else if line.count < 32, !oversized {
                        line.append(byte)
                    } else {
                        oversized = true
                    }
                }
            }
        }.start()
    }

    func takeLatest() -> PlaybackMode? {
        latest.withLock { value in defer { value = nil }; return value }
    }
}

/// Delivers 20 ms mono chunks. Stdin reading happens on its own thread so the
/// pipeline only ever sees audio that has already arrived; a live tap delivers what the
/// capture ring holds.
final class Source: @unchecked Sendable {
    let rate: Int
    let chunk: Int
    private var file: [Float]?
    private var offset = 0
    private let stream: AsyncStream<[Float]>?
    private var iterator: AsyncStream<[Float]>.Iterator?
    private let tap: TapSession?
    private var tapBuffer: [Float] = []
    private var pendingMono: [Float] = []
    private var toCanonical: StreamingResampler?
    private var deadline = UInt64.max
    private var lastCaptureNs: UInt64 = 0
    private var lastHealthCheckNs: UInt64 = 0
    private var initialOutput: AudioObjectID?
    private let parentPID: Int32?
    var failure: String?
    let stopRequested = Locked(false)

    init(options: Options, tap: TapSession? = nil) throws {
        rate = options.rate
        parentPID = options.parentPID
        self.tap = tap
        if let tap {
            chunk = canonicalRate / 50
            stream = nil
            tapBuffer = [Float](repeating: 0, count: 4_096 * Int(tap.channels))
            if Int(tap.sampleRate) != canonicalRate {
                log("resampling tap \(Int(tap.sampleRate)) → \(canonicalRate) Hz")
                toCanonical = StreamingResampler(inputRate: Int(tap.sampleRate), outputRate: canonicalRate)
            }
        } else if let input = options.input {
            let wav = try WAV.read(URL(fileURLWithPath: input))
            var samples = wav.mono()
            if wav.sampleRate != canonicalRate {
                log("resampling input \(wav.sampleRate) → \(canonicalRate) Hz")
                samples = StreamingResampler.resample(samples, from: wav.sampleRate, to: canonicalRate)
            }
            file = samples
            chunk = canonicalRate / 50
            stream = nil
        } else {
            guard options.rate == canonicalRate else { fail("--stdin expects \(canonicalRate) Hz f32le mono") }
            chunk = canonicalRate / 50
            let (s, continuation) = AsyncStream<[Float]>.makeStream(bufferingPolicy: .unbounded)
            stream = s
            let frames = chunk
            // POSIX reads into one reusable buffer: no Foundation objects, so nothing
            // accumulates in this thread's autorelease pool over a long session.
            let thread = Thread {
                var block = [Float](repeating: 0, count: frames)
                var filled = 0   // bytes
                let capacity = frames * 4
                while true {
                    let n = block.withUnsafeMutableBytes { raw in
                        read(STDIN_FILENO, raw.baseAddress! + filled, capacity - filled)
                    }
                    if n < 0, errno == EINTR { continue }
                    if n <= 0 { break }
                    filled += n
                    if filled == capacity {
                        continuation.yield(block)
                        filled = 0
                    }
                }
                if filled >= 4 { continuation.yield(Array(block[0..<(filled / 4)])) }
                continuation.finish()
            }
            thread.start()
            iterator = s.makeAsyncIterator()
        }
    }

    var totalFrames: Int? { file?.count }

    func startLive(seconds: Double?) {
        lastCaptureNs = HostClock.nowNs()
        initialOutput = try? AudioDevice.defaultOutput().id
        if let seconds { deadline = HostClock.nowNs() + UInt64(seconds * 1e9) }
    }

    /// The tail after --seconds ends by the clock too: an idle global mix delivers no frames to count.
    func startTail(seconds: Double) {
        deadline = HostClock.nowNs() + UInt64(seconds * 1e9)
    }

    /// Live chunks as the capture ring fills; an empty chunk after 20 ms without capture. Returns nil once SIGINT arrives or the deadline passes:
    /// --seconds while live, then the tail's own (`startTail`) as playout finishes over live audio.
    func nextLive() async -> [Float]? {
        guard let tap else { return nil }
        let channels = Int(tap.channels)
        let waitStart = HostClock.nowNs()
        while pendingMono.count < chunk {
            if Task.isCancelled || stopRequested.withLock({ $0 }) { return nil }
            if let parentPID, getppid() != parentPID { return nil }
            let current = HostClock.nowNs()
            if current >= deadline { return nil }
            if current - lastHealthCheckNs >= 250_000_000 {
                lastHealthCheckNs = current
                if tap.source.processesAllExited() {
                    failure = "source app exited; restart the session after reopening it"
                    return nil
                }
                if let initialOutput, (try? AudioDevice.defaultOutput().id) != initialOutput {
                    failure = "output device changed; restart the session"
                    return nil
                }
            }
            let n = tap.readCapture(into: &tapBuffer)
            if n == 0 {
                if current - lastCaptureNs >= 2_000_000_000 {
                    // A global tap gets no callbacks until something on the machine plays: a session started
                    // before the video is not a stall. Look again in 2 s.
                    if let processes = try? AudioProcessList.current(), tap.source.mixIsIdle(processes: processes) {
                        lastCaptureNs = current
                    } else {
                        failure = "capture stalled; the session stopped and released its tap"
                        return nil
                    }
                }
                try? await Task.sleep(nanoseconds: 5_000_000)
                // An empty chunk: the driver keeps playout and phrase slots moving meanwhile.
                if HostClock.nowNs() - waitStart >= 20_000_000 { return [] }
                continue
            }
            lastCaptureNs = current
            var mono = [Float](repeating: 0, count: n)
            for f in 0..<n {
                var acc: Float = 0
                for c in 0..<channels { acc += tapBuffer[f * channels + c] }
                mono[f] = acc / Float(channels)
            }
            pendingMono += toCanonical?.process(mono) ?? mono
        }
        let out = Array(pendingMono[0..<chunk])
        pendingMono.removeFirst(chunk)
        return out
    }

    func next() async -> [Float]? {
        if let file {
            guard offset < file.count else { return nil }
            let n = min(chunk, file.count - offset)
            defer { offset += n }
            return Array(file[offset..<offset + n])
        }
        return await iterator?.next()
    }
}

let canonicalRate = Int(canonicalSampleRate)
/// Translated audio the bus can queue: well above the fallback thresholds plus the longest
/// phrase. Debt and the queue limit cancel work long before it fills.
let translatedRingSeconds = 30.0

// MARK: Driver

enum DriverEvent: Sendable {
    case input(PipelineInput)
    case chunk(SynthesisJob, SynthesizedAudio)
    case finished(SynthesisJob)
    case failed(SynthesisJob, String)
    case prepared(PreparedVoice, referenceSeconds: Double, prepareNs: UInt64)
    case prepareFailed(SpeakerID, version: Int, String)
    case rememberedVoiceReady(PreparedVoice, referenceSeconds: Double, prepareNs: UInt64)
    case rememberedVoiceFailed(SpeakerID)
    /// Multi-speaker tone/system voice for a newly detected speaker.
    case voiceReady(PreparedVoice, prepareNs: UInt64)
    case voiceFailed(SpeakerID, String)
    /// The language identifier's answer for one request (ADR-0018); nil when it failed.
    case languageIdentified(Int, LanguageIdentifier.Verdict?)
}

struct Delivery {
    let at: UInt64
    let seq: Int
    let event: DriverEvent
}

struct PlayRecord {
    let unit: UnitID
    var start: UInt64?
    var end: UInt64 = 0
    var complete = false
    var started = false
    var ended = false
    var underrunReported = false
}

final class Driver {
    let o: Options
    let virtual: Bool
    let playbackControls: PlaybackControls?
    var pipeline: SessionPipeline
    let trace: TraceWriter
    let engine: OpaquePointer
    /// Live mode: the IOProc renders the engine; the driver only feeds it and reads counters.
    let tap: TapSession?
    let outRate: Int
    let outChannels: Int
    let recordDrain: RTRecordDrain?
    var sourceWriter: WAVStreamWriter?
    var translatedWriter: WAVStreamWriter?
    var renderedWriter: WAVStreamWriter?
    var backgroundSeparator: BackgroundSeparator?
    var backgroundWriter: WAVStreamWriter?
    var backgroundResampler: StreamingResampler?
    var backgroundFrames: UInt64 = 0
    var backgroundFailed = false
    var renderedBuffer: [Float] = []
    var renderedFrames: UInt64 = 0
    var lastConsumed: UInt64 = 0
    var lastOut: Int64 = 0
    /// Translated PCM queued on the bus but not yet consumed, for --translated-out. The head
    /// index avoids shifting a long buffer every step.
    var unconsumed: [Float] = []
    var unconsumedHead = 0
    var unconsumedBase: UInt64 = 0
    let recognizer: StreamingRecognizer
    let translator: IncrementalTranslator
    let hyTranslator: (any AudioTranslator)?
    let synthesizer: VoiceSynthesizer
    let translatorLatency: LatencyModel
    var vad: EnergyVAD
    var audioSegments = AudioSegmentPolicy()
    /// ADR-0017: the segmenter's own detector, on the voice the separator isolates (its spans moved
    /// back by the separator's latency) so pauses under music count; nil keeps the session's detector.
    var segmentVAD: EnergyVAD?
    var segmentThreshold: AdaptiveSpeechThreshold?
    var voiceTo16k: StreamingResampler?
    var voiceLagSamples: Int64 = 0
    /// The isolated voice of the current capture chunk, when the separator produced one.
    var voiceChunk: [Float]?
    var segmentFrontier: Int64 = 0
    /// Recently closed direct-audio spans, for the silence that follows a unit (ADR-0018).
    var recentSegments: [SourceSpan] = []
    var unitSpans: [UnitID: SourceSpan] = [:]
    /// ADR-0018 same-language passthrough. The separated bed is held `bedHoldNs` before it is
    /// written to the delayed background bus, so a span found to be in the target language (or
    /// answered with nothing) can be written as bed + its own isolated voice: the original sound,
    /// since both come from the same render, on the delayed picture by construction.
    var restoresVoice = false
    var languageIdentifier: LanguageIdentifier?
    var lidRequests: [Int: SourceSpan] = [:]
    var nextLIDRequest = 1
    var restoreSpans: [SourceSpan] = []
    var bedHold: [(bed: [Float], voice: [Float], at: UInt64)] = []
    var bedHoldNs: UInt64 = 0
    /// Separator output samples already written to the background bus.
    var bedOut: Int64 = 0
    /// The isolated voice on the source timeline (separator latency removed), last 30 s.
    var voiceHistory: [Float] = []
    var voiceHistoryStart: Int64 = 0
    /// The local Hy worker hears each span while it is spoken (direct audio, --mt hy-local), so a
    /// committed span waits only for its last turn.
    let hyStreaming: HyLocalTranslator?
    var audioFeed: AudioStreamFeed
    /// Spans closed to the worker whose answers it is computing, oldest first.
    var hyTickets: [(span: SourceSpan, ticket: HySpanTicket)] = []
    var translationTickets: [UnitID: HySpanTicket] = [:]
    var deferredAudioReferences: [(span: SourceSpan, at: UInt64)] = []
    let to16k = StreamingResampler(inputRate: canonicalRate, outputRate: 16_000)
    let inbox = Inbox<DriverEvent>()
    var deliveries: [Delivery] = []
    var seq = 0
    var tasks: [UnitID: Task<Void, Never>] = [:]
    var translationTasks: [UnitID: Task<Void, Never>] = [:]
    var translationQueue: [TranslationRequest] = []
    var translationAudio: [UnitID: [Float]] = [:]
    var synthesisQueue: [SynthesisJob] = []
    var activeSynthesis: [UnitID: Int] = [:]
    var held: [UnitID: [SynthesizedAudio]] = [:]
    var unitResamplers: [UnitID: StreamingResampler] = [:]
    var plays: [UnitID: PlayRecord] = [:]
    var currentChunk: SynthesizedAudio?
    var outWriter: WAVStreamWriter?
    var stemWriter: WAVStreamWriter?
    var srcFrames: Int64 = 0
    var outFrames: Int64 = 0
    var t0: UInt64 = 0
    var now: UInt64 = 0
    var lastTick: UInt64 = 0
    var lastResource: UInt64 = 0
    /// Virtual time spent without capture (`--simulate-capture-gap`); always 0 on the host clock.
    var idleNs: UInt64 = 0
    /// Synthetic silence fed at a pause (`idleStep`); it is not source time on the virtual clock.
    var syntheticFrames: Int64 = 0
    var idleSince: UInt64?
    var pauseFlushed = false
    /// Silence queued ahead of the separated bed to delay it with the picture (ADR-0012).
    var backgroundDelayFrames = 0
    var profileReady = false
    /// Clone voice: profiles prepared by the synthesizer, keyed by speaker and version.
    var preparedVoices: [String: PreparedVoice] = [:]
    /// Clone voice without --voice-ref: acquires each speaker's reference from the source itself.
    var acquiresReference = false
    var referencePolicies: [SpeakerID: ReferencePolicy] = [:]
    var preparingReference = false
    var pendingReferences: [ReferencePlan] = []
    /// Multi-speaker: finalized utterances waiting for the tracker to label their audio.
    var deferredReferences: [(hypothesis: ASRHypothesis, span: SourceSpan, at: UInt64)] = []
    var voiceMemory: VoiceMemoryStore?
    var memoryPreparing: Set<SpeakerID> = []
    var memoryReused: Set<SpeakerID> = []
    var memoryFailed: Set<SpeakerID> = []
    var rememberedIDs: [SpeakerID: UUID] = [:]
    var profileLog: [[String: Any]] = []
    /// Recent canonical source audio, in memory only, for cutting voice references.
    var history: [Float] = []
    /// Audio of segments each speaker's reference policy accepted, kept until their acquisition ends.
    var referenceAudio: [SpeakerID: [SourceSpan: [Float]]] = [:]
    /// Multi-speaker: labels speech so each speaker gets their own voice.
    var tracker: VoiceprintSpeakerTracker?
    var knownSpeakers: Set<SpeakerID> = []
    var historyStart: Int64 = 0
    var stats: [String: Int] = [:]
    /// `late_ms` of every scheduled phrase under a picture delay (ADR-0012).
    var slotLateMs: [Int] = []

    /// Slot timing for the run summary: phrases scheduled, median and worst lateness, and how
    /// many started within 250 ms of their slot (scheduling time, before the device buffer).
    func slotSummary() -> [String: Any] {
        guard !slotLateMs.isEmpty else { return ["scheduled": 0] }
        let sorted = slotLateMs.sorted()
        return ["scheduled": sorted.count, "median_late_ms": sorted[sorted.count / 2], "max_late_ms": sorted.last!,
                "within_250ms": sorted.filter { abs($0) <= 250 }.count]
    }
    var runtimeFailed = false
    /// Latest gains requested from the mixer (the stop path waits for them to land).
    var gainTarget = BusGains.original
    var backgroundGainTarget: Float = 0
    /// Spoken respellings applied to the synthesizer's text only (--pronounce).
    var pronunciation = TermMap([:], ignoresCase: true)

    init(options: Options, tap: TapSession? = nil, clone: VoiceSynthesizer? = nil, hyLocal: HyLocalTranslator? = nil,
         playbackControls: PlaybackControls? = nil) async throws {
        o = options
        if let path = options.voiceMemory { voiceMemory = try VoiceMemoryStore(directory: URL(fileURLWithPath: path)) }
        self.playbackControls = playbackControls
        virtual = options.clock == "virtual"
        hyStreaming = options.asr == "audio" && options.hyStream && options.clock != "virtual"
            && hyLocal?.info?.streamsSpans == true ? hyLocal : nil
        if options.asr == "audio", options.sentenceUnits, options.presentationDelayMs > 0 {
            audioSegments = .sentences(presentationDelaySeconds: Double(options.presentationDelayMs) / 1_000)
        }
        audioFeed = AudioStreamFeed(maxSamples: audioSegments.streamLimitSamples)
        var config = PipelineConfig(session: options.session, direction: options.direction)
        config.directAudioTranslation = options.asr == "audio"
        config.traceText = options.researchText
        config.emitCaptions = options.eventsStdout
        config.originalAudioEnabled = !options.translationOnly
        config.playbackMode = options.playbackMode
        config.pinSpeakerProfiles = options.tts == "clone"
        config.requireSpeakerAttribution = options.tts == "clone"
        if options.fluent {
            config.commit.streamingClauses = true
            config.finishPhraseBeforeCatchUp = true
            config.health.prebufferSeconds = 0.24
        }
        config.adaptiveStreamingPrebuffer = options.adaptivePrebuffer
        config.backgroundAudioEnabled = options.background
        if options.mt == "context" { config.contextPairs = 8 }
        if options.qualityFirst {
            config.finishPhraseBeforeCatchUp = true
            config.completePhrasesBeforePlayback = true
            config.commit.streamingClauses = false
            config.health.maxDebtSeconds = 20
            config.health.maxSourceLagSeconds = 20
            config.maxPendingUnits = 16
            // Quality-first buys whole phrases with latency: a unit closes early only once it
            // would exceed the session's own source-lag budget.
            config.commit.maxUnitSourceSeconds = config.health.maxSourceLagSeconds
        }
        if let d = options.maxDebtSeconds { config.health.maxDebtSeconds = d }
        if let l = options.maxSourceLagSeconds { config.health.maxSourceLagSeconds = l }
        // With a picture delay the lag limit counts from each unit's slot (ADR-0012).
        config.presentationDelaySeconds = Double(options.presentationDelayMs) / 1_000
        config.turnFillEnabled = options.turnFill
        config.cloneDeliveryLine = options.cloneDelivery == "line"
        // ADR-0018: the actor's own voice can come back only where the separator isolates it and
        // the picture delay leaves room to hold the bed while each line is decided.
        restoresVoice = options.passthrough && options.asr == "audio" && options.background
            && options.tts == "clone" && options.presentationDelayMs >= 4_000
        config.restoresOriginalVoice = restoresVoice
        if restoresVoice {
            // Decide before the oldest part of a unit leaves the held bed. The margin covers
            // driver ticks, separator buffering and scheduling jitter; never guess a voice.
            config.originalVoiceDecisionDeadlineSeconds = max(0.1, min(6, config.presentationDelaySeconds - 1.5) - 0.7)
        }
        if let k = options.maxUnitKeys { config.commit.maxUnitKeys = k }
        if let s = options.maxUnitSeconds { config.commit.maxUnitSourceSeconds = s }
        if let c = options.speakerCarry { config.unlabeledSpeakerCarrySeconds = c }
        if let c = options.speakerCarryPause { config.unlabeledSpeakerCarryAfterPauseSeconds = c }
        if let c = options.speakerCarryPiece { config.unlabeledSpeakerCarryMaxPieceSeconds = c }
        // Quality-first must preserve the words as well as wait for complete PCM. The hard
        // validation cap remains in force for implausible model output.
        config.validator.shortensOverBudgetTargets = options.shorten && !options.qualityFirst
        config.validator.silencesFillers = options.fillerSilence
        if let path = options.glossary { config.glossary = try loadTerms(path) }
        if let path = options.pronounce { pronunciation = TermMap(try loadTerms(path), ignoresCase: true) }
        if options.speakers == "auto" {
            config.multiSpeaker = true
            tracker = VoiceprintSpeakerTracker()
        }
        // Mandarin recognition hands back one key per character, so only a word segmenter
        // can keep a clause cut out of the middle of a word (see WordSegmenter).
        let segmenter: (any WordSegmenter)? = options.wordCuts && options.direction.source == .mandarin
            ? NaturalLanguageWordSegmenter(language: options.direction.source.translationIdentifier)
            : nil
        // A target shortened to its spoken budget obeys the same rule on the other side:
        // English → Mandarin must not cut a Han word in half either.
        let targetSegmenter: (any WordSegmenter)? = options.wordCuts && options.direction.target == .mandarin
            ? NaturalLanguageWordSegmenter(language: options.direction.target.translationIdentifier)
            : nil
        pipeline = SessionPipeline(config: config, wordSegmenter: segmenter, targetWordSegmenter: targetSegmenter)
        trace = try TraceWriter(url: URL(fileURLWithPath: options.trace))
        vad = EnergyVAD(thresholdDB: options.vadThresholdDB)
        self.tap = tap
        if let tap, let e = tap.engine {
            engine = e
            outRate = Int(tap.sampleRate)
            outChannels = Int(tap.channels)
            // An empty translated bus between units is normal; trackPlayout reports real underruns.
            recordDrain = tap.recordRing.map { RTRecordDrain(ring: $0, reportStarvation: false) }
        } else {
            var cfg = a2a_engine_config(channels: 1, capture_ring_frames: UInt32(canonicalRate * 2),
                                        max_delay_frames: 0,
                                        translated_ring_frames: UInt32(Double(canonicalRate) * translatedRingSeconds),
                                        max_block_frames: 4_096, record_capacity: 4_096,
                                        rendered_ring_frames: options.renderedOut == nil ? 0 : UInt32(canonicalRate * 2),
                                        fade_frames: UInt32(Double(canonicalRate) * TapSession.edgeFadeSeconds))
            guard let e = a2a_engine_create(&cfg) else { fail("engine allocation failed") }
            engine = e
            a2a_engine_set_render_output(e, true)
            outRate = canonicalRate
            outChannels = 1
            recordDrain = nil
        }

        switch options.asr {
        case "audio": recognizer = AudioOnlyRecognizer()
        case "scripted":
            guard let path = options.asrScript else { fail("--asr scripted needs --asr-script") }
            recognizer = ScriptedRecognizer(script: try ASRScript.load(URL(fileURLWithPath: path)))
        default: fail("unknown --asr \(options.asr)")
        }
        hyTranslator = options.mt == "hy" ? try HySimulTranslator(server: URL(string: options.hyServer) ?? URL(fileURLWithPath: "/"), direction: options.direction.rawValue) : hyLocal
        switch options.mt {
        case "dictionary":
            let d = try options.mtDictionary.map { try DictionaryTranslator.load(URL(fileURLWithPath: $0)) }
                ?? DictionaryTranslator(table: [:])
            translator = d
            translatorLatency = d.latency
        case "hy", "hy-local":
            guard options.clock != "virtual" else { fail("--mt \(options.mt) requires a real clock") }
            translator = DictionaryTranslator(table: [:])
            translatorLatency = .init()
        case "context":
            guard options.direction == .chineseToEnglish, let python = options.contextPython,
                  let worker = options.contextWorker else {
                fail("--mt context requires zh-en, --context-python and --context-worker")
            }
            let arguments = options.contextSeries.map { ["--series", $0] } ?? []
            let local = try ContextualTranslator(python: URL(fileURLWithPath: python),
                worker: URL(fileURLWithPath: worker), arguments: arguments)
            try await local.waitUntilReady()
            translator = local
            translatorLatency = .init()
        default: fail("unknown --mt \(options.mt)")
        }
        switch options.tts {
        case "tone": synthesizer = ToneSynthesizer()
        case "system":
            let voice = SystemVoiceSynthesizer(language: options.direction.target.speechLocaleIdentifier)
            synthesizer = voice
            // Loads the voice while recognition warms up; the first unit is seconds away.
            Task.detached { await voice.warmUp() }
        case "clone":
            guard let clone else { fail("--tts clone needs a started voice worker") }
            synthesizer = clone
        default: fail("unknown --tts \(options.tts)")
        }
        if options.out != nil { outWriter = try WAVStreamWriter(url: URL(fileURLWithPath: options.out!), sampleRate: canonicalRate, channels: 1) }
        if options.stems != nil { stemWriter = try WAVStreamWriter(url: URL(fileURLWithPath: options.stems!), sampleRate: canonicalRate, channels: 2) }
        if let path = options.sourceOut { sourceWriter = try WAVStreamWriter(url: URL(fileURLWithPath: path), sampleRate: canonicalRate, channels: 1) }
        if let path = options.translatedOut { translatedWriter = try WAVStreamWriter(url: URL(fileURLWithPath: path), sampleRate: outRate, channels: 1) }
        if options.background {
            let delaySeconds = Double(options.presentationDelayMs) / 1_000
            guard a2a_engine_enable_background(engine, UInt32(Double(outRate) * (3 + delaySeconds))) else {
                throw AdapterError.unsupported("background bus allocation failed")
            }
            let separator = try BackgroundSeparator()
            backgroundSeparator = separator
            if options.asr == "audio", options.sentenceUnits, options.presentationDelayMs > 0 {
                // Music is gone from the isolated voice, so a slightly lower threshold still
                // ignores it and keeps quiet lines (ADR-0017).
                segmentVAD = EnergyVAD(thresholdDB: options.vadThresholdDB - 5)
                segmentThreshold = AdaptiveSpeechThreshold(floorDB: options.vadThresholdDB - 5)
                voiceTo16k = StreamingResampler(inputRate: canonicalRate, outputRate: 16_000)
                voiceLagSamples = Int64((separator.latencySeconds * Double(canonicalRate)).rounded())
            }
            if delaySeconds > 0, let ring = a2a_engine_background_ring(engine) {
                // Picture delay: the bed is a FIFO drained at the device rate the tap fills it at,
                // so D seconds of silence queued before callbacks start keep it D behind the
                // capture for the session. The separator already lags its input by its own latency.
                // ADR-0018: part of the delay holds the bed while each line is decided.
                if restoresVoice { bedHoldNs = UInt64(min(6, delaySeconds - 1.5) * 1e9) }
                let queued = delaySeconds - separator.latencySeconds - Double(bedHoldNs) / 1e9
                let frames = Int((max(0, queued) * Double(outRate)).rounded())
                let silence = [Float](repeating: 0, count: frames)
                let written = silence.withUnsafeBufferPointer { a2a_ring_write(ring, $0.baseAddress!, UInt32(frames)) }
                guard Int(written) == frames else { throw AdapterError.unsupported("background delay allocation failed") }
                backgroundDelayFrames = frames
            }
            backgroundResampler = StreamingResampler(inputRate: canonicalRate, outputRate: outRate)
            if restoresVoice {
                voiceLagSamples = Int64((separator.latencySeconds * Double(canonicalRate)).rounded())
                if let python = options.clonePython, let worker = options.cloneWorker,
                   let model = options.lidModel ?? Self.defaultLIDModel(nearWorker: worker) {
                    let script = URL(fileURLWithPath: worker).deletingLastPathComponent()
                        .appendingPathComponent("lid_worker.py").path
                    do {
                        let lid = try LanguageIdentifier(python: python, worker: script, model: model)
                        let load = try await lid.ready()
                        let inbox = self.inbox
                        lid.onResult = { id, verdict in inbox.post(.languageIdentified(id, verdict)) }
                        languageIdentifier = lid
                        log("language identifier ready: load \(load) s")
                    } catch {
                        log("language identifier failed: \(error); lines in the target language are translated")
                    }
                } else {
                    log("language identifier not found; lines in the target language are translated")
                }
            }
            if let path = options.backgroundOut {
                backgroundWriter = try WAVStreamWriter(url: URL(fileURLWithPath: path), sampleRate: canonicalRate, channels: 1)
            }
        }
        if let path = options.renderedOut {
            renderedWriter = try WAVStreamWriter(url: URL(fileURLWithPath: path), sampleRate: outRate, channels: outChannels)
            renderedBuffer = [Float](repeating: 0, count: 4_096 * outChannels)
            a2a_engine_set_capture_rendered(engine, true)
        }
        // Apply before AudioDeviceStart: the engine defaults to original monitoring.
        if options.translationOnly { a2a_engine_set_gains(engine, 0, 0, 0) }
        if options.tts == "clone" {
            if let path = options.voiceRef {
                do { try await prepareFixedReference(path, textPath: options.voiceRefText) }
                catch { fail("voice profile failed: \(error)") }
            } else {
                acquiresReference = true
            }
        }
    }

    // MARK: Voice reference

    /// A fixed reference is prepared before capture starts, so the first unit is voiced.
    func prepareFixedReference(_ path: String, textPath: String?) async throws {
        let wav = try WAV.read(URL(fileURLWithPath: path))
        let pcm = wav.mono()
        let text = try textPath.map { try String(contentsOfFile: $0, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""
        let seconds = Double(pcm.count) / Double(wav.sampleRate)
        guard (1...15).contains(seconds) else { fail("--voice-ref must be 1–15 s (got \(String(format: "%.2f", seconds)) s)") }
        let speaker = pipeline.config.defaultSpeaker
        let profile = VoiceProfile(speakerID: speaker, version: 1, referencePCM: pcm, referenceText: text,
                                   sampleRate: wav.sampleRate, channelCount: 1,
                                   quality: ReferenceQuality(durationSeconds: seconds, snrEstimateDB: .nan,
                                                             clippedFraction: clippedFraction(pcm), overlapSuspected: false))
        let started = HostClock.nowNs()
        let voice = try await synthesizer.prepare(profile)
        inbox.post(.prepared(voice, referenceSeconds: seconds, prepareNs: HostClock.nowNs() - started))
    }

    /// Source audio is kept while some speaker's reference may still be cut from it.
    var keepsHistory: Bool {
        if o.translatesAudio { return true }
        guard acquiresReference else { return false }
        return pipeline.config.multiSpeaker || referencePolicies[pipeline.config.defaultSpeaker]?.isComplete != true
    }

    /// Offers a finalized utterance to its speaker's reference policy and prepares any profile
    /// it makes due.
    func acquireReference(_ h: ASRHypothesis) {
        let multi = pipeline.config.multiSpeaker
        guard acquiresReference, h.isFinal,
              multi || referencePolicies[pipeline.config.defaultSpeaker]?.isComplete != true else { return }
        let policy = ReferencePolicy(speaker: pipeline.config.defaultSpeaker)
        let candidates = policy.referenceCandidates(of: h)
        guard !candidates.isEmpty else {
            stats["reference_rejected_no_timing", default: 0] += 1
            return
        }
        for candidate in candidates {
            guard let span = policy.speechSpan(of: candidate) else { continue }
            if multi {
                // Each cut must independently match a single tracked speaker.
                deferredReferences.append((candidate, span, now))
            } else {
                offerReference(candidate, span: span, speaker: pipeline.config.defaultSpeaker)
            }
        }
        if multi { offerDeferredReferences() }
    }

    /// Multi-speaker: a waiting utterance joins a reference only when one speaker covers it,
    /// so no speaker's cloned voice is learned from someone else's audio.
    func offerDeferredReferences() {
        var waiting: [(hypothesis: ASRHypothesis, span: SourceSpan, at: UInt64)] = []
        for item in deferredReferences {
            let timed = ReferencePolicy(speaker: pipeline.config.defaultSpeaker).tokenSpan(of: item.hypothesis) ?? item.span
            let coverage = pipeline.speakerCoverage(of: timed)
            if let c = coverage, c.labeled >= 0.7 {
                if c.share >= 0.9 * c.labeled { offerReference(item.hypothesis, span: item.span, speaker: c.speaker) }
                else { stats["reference_rejected_mixed_speakers", default: 0] += 1 }
            } else if now - item.at > 3_000_000_000 {
                // Until the tracker labels anyone, references are acquired for S1 as in
                // single-speaker sessions, so unlabeled audio still gets a cloned voice.
                if knownSpeakers.isEmpty { offerReference(item.hypothesis, span: item.span, speaker: pipeline.config.defaultSpeaker) }
                else { stats["reference_rejected_unlabeled", default: 0] += 1 }
            } else {
                waiting.append(item)
            }
        }
        deferredReferences = waiting
    }

    func offerReference(_ h: ASRHypothesis, span: SourceSpan, speaker: SpeakerID) {
        offerReferenceText(h.text, span: span, speaker: speaker)
    }

    func offerReferenceText(_ text: String, span: SourceSpan, speaker: SpeakerID) {
        guard !memoryPreparing.contains(speaker), !memoryReused.contains(speaker) else { return }
        var policy = referencePolicies[speaker] ?? liveReferencePolicy(for: speaker)
        guard !policy.isComplete else { return }
        guard var pcm = historySlice(span) else {
            stats["reference_rejected_expired", default: 0] += 1
            return
        }
        let clipped = clippedFraction(pcm)
        var voiceShareDB: Float?
        if policy.config.minVoiceShareDB != nil {
            // ADR-0016: an audio-only reference is the isolated voice of its segment, and a segment
            // that is mostly music or effects is not a voice at all. Half a second of the audio
            // before the segment warms the unit up. Failure keeps the mix, ungated, and is counted.
            let warm = SourceSpan(start: max(historyStart, span.start - Int64(canonicalRate / 2)), end: span.end)
            if let context = historySlice(warm),
               let vocals = try? BackgroundSeparator.isolateVoice(context, preroll: context.count - pcm.count),
               vocals.count == pcm.count {
                voiceShareDB = Signal.dbfs(Signal.rms(vocals)) - Signal.dbfs(Signal.rms(pcm))
                pcm = vocals
            } else {
                stats["reference_isolation_failed", default: 0] += 1
            }
        }
        let result = policy.offer(span: span, text: text, levelDB: Signal.dbfs(Signal.rms(pcm)),
                                  clippedFraction: clipped, voiceShareDB: voiceShareDB)
        referencePolicies[speaker] = policy
        publishVoiceProgress(speaker, state: "collecting", seconds: policy.acceptedSeconds)
        switch result {
        case .failure(let reason): stats["reference_rejected_\(reason.rawValue)", default: 0] += 1
        case .success(nil):
            referenceAudio[speaker, default: [:]][span] = pcm
        case .success(let plan?):
            referenceAudio[speaker, default: [:]][span] = pcm
            if preparingReference {
                pendingReferences.removeAll { $0.speaker == plan.speaker }
                pendingReferences.append(plan)
            } else {
                prepareReference(plan)
            }
        }
    }

    func liveReferencePolicy(for speaker: SpeakerID) -> ReferencePolicy {
        var config = ReferencePolicyConfig()
        config.allowUpgrade = !pipeline.config.pinSpeakerProfiles
        config.allowEmptyText = o.asr == "audio" && o.cloneMode == .xvector
        // Without a transcript nothing says a segment holds speech; its isolated voice does.
        if config.allowEmptyText, o.isolateReferences { config.minVoiceShareDB = -10 }
        return ReferencePolicy(speaker: speaker, config: config)
    }

    /// Only clean, predominantly single-speaker spans become audio-only references.
    func offerAudioReferences() {
        var waiting: [(span: SourceSpan, at: UInt64)] = []
        for item in deferredAudioReferences {
            if !pipeline.config.multiSpeaker {
                offerReferenceText("", span: item.span, speaker: pipeline.config.defaultSpeaker)
            } else if let coverage = pipeline.speakerCoverage(of: item.span), coverage.labeled >= 0.7 {
                if coverage.share >= 0.9 * coverage.labeled {
                    offerReferenceText("", span: item.span, speaker: coverage.speaker)
                } else { stats["reference_rejected_mixed_speakers", default: 0] += 1 }
            } else if now - item.at <= 3_000_000_000 {
                waiting.append(item)
            } else { stats["reference_rejected_unlabeled", default: 0] += 1 }
        }
        deferredAudioReferences = waiting
    }

    /// Multi-speaker: a speaker's first label prepares their voice. Tone and system voices are
    /// ready at once (the system voice is cast by the speaker's pitch); a clone voice waits for
    /// a reference cut from that speaker's own speech. When S1 already speaks with the default
    /// voice, their cast voice follows as version 2.
    func noteSpeaker(_ event: SpeakerEvent) {
        if case .assign(let speaker, _) = event { tryRememberedVoice(for: speaker) }
        guard case .assign(let speaker, _) = event, knownSpeakers.insert(speaker).inserted else { return }
        log("speaker \(speaker) detected")
        guard o.tts != "clone", !o.captionsOnly else { return }
        let hasDefault = speaker == pipeline.config.defaultSpeaker && profileReady
        if o.tts == "tone" {
            if !hasDefault { send(.profileReady(speaker, version: 1)) }
            return
        }
        let profile = VoiceProfile(speakerID: speaker, version: hasDefault ? 2 : 1, referencePCM: [], referenceText: "",
                                   sampleRate: canonicalRate, channelCount: 1,
                                   quality: ReferenceQuality(durationSeconds: 0, snrEstimateDB: .nan,
                                                             clippedFraction: 0, overlapSuspected: false),
                                   pitchHz: tracker?.pitchHz(of: speaker))
        let synthesizer = self.synthesizer
        let inbox = self.inbox
        Task.detached {
            let started = HostClock.nowNs()
            do {
                let voice = try await synthesizer.prepare(profile)
                inbox.post(.voiceReady(voice, prepareNs: HostClock.nowNs() - started))
            } catch {
                inbox.post(.voiceFailed(speaker, "\(error)"))
            }
        }
    }

    func prepareReference(_ plan: ReferencePlan) {
        // Segments are joined with short pauses; the transcript is their finalized text in order.
        let gap = [Float](repeating: 0, count: canonicalRate * 15 / 100)
        var pcm: [Float] = []
        for (i, segment) in plan.segments.enumerated() {
            guard let slice = referenceAudio[plan.speaker]?[segment.span] else {
                stats["reference_missing_audio", default: 0] += 1
                return
            }
            if i > 0 { pcm += gap }
            pcm += slice
        }
        let profile = VoiceProfile(speakerID: plan.speaker, version: plan.version, referencePCM: pcm,
                                   referenceText: plan.text, sampleRate: canonicalRate, channelCount: 1,
                                   quality: ReferenceQuality(durationSeconds: plan.seconds, snrEstimateDB: .nan,
                                                             clippedFraction: clippedFraction(pcm), overlapSuspected: false))
        preparingReference = true
        publishVoiceProgress(plan.speaker, state: "preparing", seconds: plan.seconds)
        let synthesizer = self.synthesizer
        let inbox = self.inbox
        let memory = voiceMemory
        let memoryID = rememberedIDs[plan.speaker] ?? UUID()
        rememberedIDs[plan.speaker] = memoryID
        let seconds = Double(pcm.count) / Double(canonicalRate)
        Task.detached {
            let started = HostClock.nowNs()
            do {
                let voice = try await synthesizer.prepare(profile)
                memory?.remember(profile, id: memoryID)
                inbox.post(.prepared(voice, referenceSeconds: seconds, prepareNs: HostClock.nowNs() - started))
            } catch {
                inbox.post(.prepareFailed(plan.speaker, version: plan.version, "\(error)"))
            }
        }
    }

    func publishVoiceProgress(_ speaker: SpeakerID, state: String, seconds: Double = 0, reused: Bool = false) {
        guard o.eventsStdout else { return }
        let value: [String: Any] = ["voice_status": state, "speaker": speaker.rawValue,
                                   "seconds": seconds, "required_seconds": ReferencePolicyConfig().minSeconds,
                                   "reused": reused]
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }

    func tryRememberedVoice(for speaker: SpeakerID) {
        guard o.tts == "clone", let memory = voiceMemory,
              !memoryPreparing.contains(speaker), !memoryReused.contains(speaker), !memoryFailed.contains(speaker),
              referencePolicies[speaker]?.hasIssuedProfile != true,
              let print = tracker?.voiceprint(of: speaker), let saved = memory.match(print),
              let profile = saved.profile(for: speaker) else { return }
        memoryPreparing.insert(speaker)
        rememberedIDs[speaker] = saved.identityID
        publishVoiceProgress(speaker, state: "preparing", reused: true)
        let synthesizer = self.synthesizer, inbox = self.inbox
        Task.detached {
            let started = HostClock.nowNs()
            do {
                let voice = try await synthesizer.prepare(profile)
                inbox.post(.rememberedVoiceReady(voice, referenceSeconds: profile.quality.durationSeconds,
                                                prepareNs: HostClock.nowNs() - started))
            } catch { inbox.post(.rememberedVoiceFailed(speaker)) }
        }
    }

    /// The fetched language-ID model beside a repository checkout: research/<worker> lives two
    /// levels below the root, and models under .local/models/<id>/<revision> (ADR-0018).
    static func defaultLIDModel(nearWorker worker: String) -> String? {
        let root = URL(fileURLWithPath: worker).deletingLastPathComponent().deletingLastPathComponent()
        let base = root.appendingPathComponent(".local/models/ecapa-voxlingua107-lid-mlx")
        guard let revisions = try? FileManager.default.contentsOfDirectory(atPath: base.path) else { return nil }
        return revisions.sorted().map { base.appendingPathComponent($0).path }
            .first { FileManager.default.fileExists(atPath: $0 + "/config.json") }
    }

    /// Seconds of silence after `span` as far as the segmenter knows now: up to the next span that
    /// closed or opened since, else up to everything heard so far (ADR-0018).
    func silenceAfter(_ span: SourceSpan) -> Double {
        let next = recentSegments.first { $0.start >= span.end }?.start ?? audioSegments.openStart ?? segmentFrontier
        return Double(max(0, next - span.end)) / Double(canonicalRate)
    }

    /// The isolated voice of `span` on the source timeline, as far as it has been separated.
    func voiceSlice(_ span: SourceSpan) -> [Float] {
        let a = Int(max(span.start, voiceHistoryStart) - voiceHistoryStart)
        let b = Int(min(span.end, voiceHistoryStart + Int64(voiceHistory.count)) - voiceHistoryStart)
        return a < b ? Array(voiceHistory[a..<b]) : []
    }

    /// Asks the language identifier about one closed unit's isolated voice (ADR-0018).
    func requestLanguage(_ span: SourceSpan) {
        guard let lid = languageIdentifier, span.durationSeconds >= 1 else { return }
        let voice = voiceSlice(span)
        guard voice.count >= canonicalRate / 2 else { return }
        let resampler = StreamingResampler(inputRate: canonicalRate, outputRate: 16_000)
        let id = nextLIDRequest
        nextLIDRequest += 1
        lidRequests[id] = span
        if lidRequests.count > 64, let oldest = lidRequests.keys.min() { lidRequests[oldest] = nil }
        lid.identify(id: id, samples16k: resampler.process(voice))
    }

    /// Writes the held bed that is older than the hold to the delayed background bus. A span
    /// marked for its own voice is written as bed + voice (with 10 ms ramps), the original sound.
    func flushBed(now: UInt64) throws {
        guard let ring = a2a_engine_background_ring(engine) else { bedHold.removeAll(); return }
        if backgroundDelayFrames > 0 {
            // The bed normally sits D (less a few chunks of processing) ahead of the mixer.
            // A pause the device rendered through without capture drained it; refill the
            // gap with silence so music and effects stay with the delayed picture.
            let readable = Int(a2a_ring_readable(ring))
            if readable < backgroundDelayFrames - outRate / 4 {
                let silence = [Float](repeating: 0, count: backgroundDelayFrames - outRate / 25 - readable)
                _ = silence.withUnsafeBufferPointer { a2a_ring_write(ring, $0.baseAddress!, UInt32(silence.count)) }
            }
        }
        while let first = bedHold.first, now >= first.at + bedHoldNs {
            bedHold.removeFirst()
            var mixed = first.bed
            let start = bedOut - voiceLagSamples   // source sample of mixed[0]
            let end = start + Int64(mixed.count)
            let ramp: Int64 = 480
            let touching = restoreSpans.filter { $0.end + ramp > start && $0.start - ramp < end }
            if !touching.isEmpty {
                // Adjacent restored spans share their ramps: the voice comes back once, not twice.
                var gain = [Float](repeating: 0, count: mixed.count)
                for span in touching {
                    for i in 0..<mixed.count {
                        let inside = min(start + Int64(i) - (span.start - ramp), (span.end + ramp) - start - Int64(i))
                        if inside > 0 { gain[i] = max(gain[i], Float(min(ramp, inside)) / Float(ramp)) }
                    }
                }
                for i in 0..<mixed.count where gain[i] > 0 {
                    mixed[i] = max(-1, min(1, mixed[i] + gain[i] * first.voice[i]))
                }
            }
            bedOut += Int64(mixed.count)
            let pcm = backgroundResampler?.process(mixed) ?? []
            if !pcm.isEmpty {
                let count = pcm.withUnsafeBufferPointer { a2a_ring_write(ring, $0.baseAddress!, UInt32(pcm.count)) }
                backgroundFrames += UInt64(count)
                if Int(count) != pcm.count { throw AdapterError.unsupported("background bus overflow") }
            }
        }
        if let oldest = restoreSpans.first, oldest.end + 48_000 < bedOut - voiceLagSamples { restoreSpans.removeFirst() }
    }

    func failBackground() {
        // Separation failure mutes only this bus; never substitute original audio.
        backgroundFailed = true
        voiceChunk = nil
        bedHold.removeAll()
        restoreSpans.removeAll()
        if segmentVAD != nil {
            // Back to the mix, as the session's detector hears it; its samples run ahead
            // of the voice's, so the segmenter's timeline only moves forward.
            segmentVAD = EnergyVAD(thresholdDB: o.vadThresholdDB)
            segmentThreshold = AdaptiveSpeechThreshold(floorDB: o.vadThresholdDB)
            voiceTo16k = nil
            voiceLagSamples = 0
        }
        backgroundGainTarget = 0
        a2a_engine_set_background_gain(engine, 0, UInt32(outRate / 100))
        log("background separation failed; music/effects muted")
    }

    /// Closed spans for direct audio translation. With sentence units (ADR-0017) the segmenter has
    /// its own detector: on the isolated voice while the separator runs (a synthetic pause frame is
    /// silence there too), on the mix otherwise. The voice lags its input by the separator's
    /// latency, so its frames are placed that much earlier on the source timeline.
    func segmentSpans(mix frame16: AudioFrame16k, mixEvents: [VADEvent], samples: Int) -> [SourceSpan] {
        guard var detector = segmentVAD else {
            return audioSegments.process(events: mixEvents, frontier: frame16.span.end)
        }
        var frame = frame16
        if let resampler = voiceTo16k {
            let voice = voiceChunk ?? [Float](repeating: 0, count: samples)
            let span = SourceSpan(start: max(0, frame16.span.start - voiceLagSamples),
                                  end: max(0, frame16.span.end - voiceLagSamples))
            frame = AudioFrame16k(span: span, samples: resampler.process(voice))
        }
        voiceChunk = nil
        let level = frame.samples.isEmpty || frame.span.isEmpty
            ? nil : (span: frame.span, db: Signal.dbfs(Signal.rms(frame.samples)))
        if let level, var threshold = segmentThreshold {
            detector.thresholdDB = threshold.update(span: level.span, db: level.db)
            segmentThreshold = threshold
        }
        let events = frame.span.isEmpty ? [] : detector.process(frame)
        segmentVAD = detector
        segmentFrontier = max(segmentFrontier, frame.span.end)
        return audioSegments.process(events: events, frontier: frame.span.end, level: level)
    }

    /// Streams the span being spoken to the local Hy worker and closes spans as the segmenter does.
    func feedHy(_ hy: HyLocalTranslator, closed: [SourceSpan]) {
        let voicedEnd = segmentVAD.map { $0.voicedEnd } ?? vad.voicedEnd
        for action in audioFeed.update(closed: closed, openStart: audioSegments.openStart, voicedEnd: voicedEnd) {
            switch action {
            case .append(let span):
                guard let pcm = historySlice(span), pcm.count == Int(span.end - span.start) else {
                    // Not expected (history holds 30 s): withdraw the span; its request sends it whole.
                    hy.discard()
                    audioFeed = AudioStreamFeed(maxSamples: audioSegments.streamLimitSamples)
                    return
                }
                hy.stream(pcm)
            case .close(let span):
                if hyTickets.count >= 16 { hyTickets.removeFirst() }
                hyTickets.append((span, hy.close(spanSamples: Int(span.end - span.start))))
            case .discard:
                hy.discard()
            }
        }
    }

    func historySlice(_ span: SourceSpan) -> [Float]? {
        guard span.start >= historyStart else { return nil }
        let lo = Int(span.start - historyStart)
        let hi = min(history.count, Int(span.end - historyStart))
        return lo < hi ? Array(history[lo..<hi]) : nil
    }

    func clippedFraction(_ pcm: [Float]) -> Double {
        Double(pcm.reduce(0) { $0 + (abs($1) >= 0.999 ? 1 : 0) }) / Double(max(1, pcm.count))
    }

    deinit {
        for task in tasks.values { task.cancel() }
        for task in translationTasks.values { task.cancel() }
        if tap == nil { a2a_engine_destroy(engine) }
    }

    func clock() -> UInt64 {
        virtual ? t0 + UInt64(srcFrames - syntheticFrames) * 1_000_000_000 / UInt64(canonicalRate) + idleNs : HostClock.nowNs()
    }

    // MARK: Reducer plumbing

    func send(_ input: PipelineInput) {
        execute(pipeline.handle(input, at: now))
    }

    func execute(_ effects: [PipelineEffect]) {
        for effect in effects {
            switch effect {
            case .trace(let e):
                trace.emit(e)
                // Picture delay: how far from its slot each phrase was scheduled (positive = late).
                if e.kind == .playScheduled, case .int(let late)? = e.value("late_ms") { slotLateMs.append(Int(late)) }
                // Run-summary counters: how each line left the session (recovery report).
                if e.kind == .sourceDisposition, e.value("disposition") == .string("silence") {
                    stats["silence_units", default: 0] += 1
                }
                if e.kind == .commitTarget {
                    if e.value("shortened") == .bool(true) { stats["shortened_targets", default: 0] += 1 }
                    if e.value("speaker_carried") == .bool(true) { stats["speaker_carried_units", default: 0] += 1 }
                }
                if o.eventsStdout, [.stateChange, .error, .queueDepth, .fallbackEnter, .fallbackExit, .gainRamp].contains(e.kind) {
                    FileHandle.standardOutput.write(Data((e.jsonLine() + "\n").utf8))
                }
            case .caption(let kind, let id, let text):
                if o.eventsStdout {
                    let caption = TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch,
                                             gen: pipeline.gate.generation, kind: .commitTarget,
                                             fields: [.string("caption", kind), .string("id", id), .string("text", text)])
                    FileHandle.standardOutput.write(Data((caption.jsonLine() + "\n").utf8))
                }
            case .requestFinalize: recognizer.requestFinalize()
            case .translate(let req):
                if let span = req.sourceSpan, o.asr == "audio" {
                    unitSpans[req.unitID] = span
                    if unitSpans.count > 128, let oldest = unitSpans.keys.min() { unitSpans[oldest] = nil }
                }
                startTranslation(req)
            case .synthesize(let job):
                if virtual { startSynthesis(job) } else { synthesisQueue.append(job) }
            case .cancelTranslation(let unit):
                translationTasks.removeValue(forKey: unit)?.cancel()
                translationQueue.removeAll { $0.unitID == unit }
                translationAudio.removeValue(forKey: unit)
                translationTickets.removeValue(forKey: unit)
                deliveries.removeAll { if case .input(.translation(let id, _)) = $0.event { id == unit } else { false } }
            case .cancelSynthesis(let unit):
                tasks[unit]?.cancel()
                tasks[unit] = nil
                synthesisQueue.removeAll { $0.unit == unit }
                activeSynthesis.removeValue(forKey: unit)
                unitResamplers[unit] = nil
                deliveries.removeAll { d in
                    switch d.event {
                    case .chunk(let j, _), .finished(let j), .failed(let j, _): j.unit == unit
                    case .input, .prepared, .prepareFailed, .voiceReady, .voiceFailed,
                         .rememberedVoiceReady, .rememberedVoiceFailed, .languageIdentified: false
                    }
                }
            case .holdPCM(let header):
                if let c = currentChunk { held[header.unitID, default: []].append(c) }
            case .enqueuePCM(let header):
                if let c = currentChunk { write(c, unit: header.unitID) }
            case .releaseHeld(let unit):
                for c in held[unit] ?? [] { write(c, unit: unit) }
                held[unit] = nil
                if plays[unit]?.complete == true { flushResampler(unit) }
            case .discardHeld(let unit):
                held[unit] = nil
                plays[unit] = nil
            case .dropPCM: stats["pcm_dropped", default: 0] += 1
            case .enqueueProviderPCM: break // Used only by the provider streaming driver.
            case .rampGains(let g, let ms):
                gainTarget = g
                a2a_engine_set_gains(engine, g.original, g.translated, UInt32(ms * outRate / 1_000))
            case .backgroundGain(let gain, let ms):
                backgroundGainTarget = backgroundFailed ? 0 : gain
                a2a_engine_set_background_gain(engine, backgroundGainTarget, UInt32(ms * outRate / 1_000))
            case .restoreOriginalVoice(let span):
                guard restoresVoice, backgroundSeparator != nil, !backgroundFailed else { break }
                restoreSpans.append(span)
                restoreSpans.sort { $0.start < $1.start }
                stats["voice_restored_units", default: 0] += 1
                // Bed already written for the start of the span: that part stays without its voice.
                if span.start + voiceLagSamples < bedOut { stats["voice_restored_late", default: 0] += 1 }
            case .flushTranslatedBus:
                a2a_engine_flush_translated(engine)
                plays.removeAll()
                // Whatever the mixer has not consumed yet is discarded, not played (to within
                // the device buffer the RT side may still render before it honors the flush).
                writePlayed()
                unconsumed.removeAll()
                unconsumedHead = 0
                unconsumedBase = a2a_ring_total_written(a2a_engine_translated_ring(engine))
            case .teardown: break
            }
        }
        launchNextTranslation()
        if canStartSynthesis, !synthesisQueue.isEmpty {
            startSynthesis(synthesisQueue.removeFirst())
        }
    }

    func schedule(_ event: DriverEvent, after latency: UInt64) {
        seq += 1
        deliveries.append(Delivery(at: now + latency, seq: seq, event: event))
    }

    func startTranslation(_ req: TranslationRequest) {
        let translator = self.translator
        if hyTranslator != nil {
            if let span = req.sourceSpan, let i = hyTickets.firstIndex(where: { $0.span == span }) {
                // Spans are requested in order: closed spans before this one were never requested.
                translationTickets[req.unitID] = hyTickets[i].ticket
                hyTickets.removeFirst(i + 1)
                translationQueue.append(req)
                launchNextTranslation()
                return
            }
            if hyStreaming != nil {
                // Sending it whole now would interleave with the open span's streamed audio in the
                // worker and shift every later answer by one span.
                inbox.post(.input(.translation(req.unitID, .failure("Hy-SimulMT span was not streamed"))))
                return
            }
            guard let span = req.sourceSpan, span.start >= historyStart,
                  span.end <= historyStart + Int64(history.count), let audio = historySlice(span) else {
                inbox.post(.input(.translation(req.unitID, .failure("Hy-SimulMT source audio unavailable"))))
                return
            }
            translationAudio[req.unitID] = audio
        }
        if virtual {
            let latency = translatorLatency.nanoseconds(units: req.uncommittedSource.count)
            let unit = req.unitID
            // Fixture adapters complete synchronously; delivery waits for the modeled latency.
            let box = Locked<TranslationOutcome?>(nil)
            let done = DispatchSemaphore(value: 0)
            Task.detached {
                let outcome: TranslationOutcome
                do { outcome = .success(try await translator.translate(req)) } catch { outcome = .failure("\(error)") }
                box.withLock { $0 = outcome }
                done.signal()
            }
            done.wait()
            schedule(.input(.translation(unit, box.withLock { $0 }!)), after: latency)
            return
        }
        translationQueue.append(req)
        launchNextTranslation()
    }

    func launchNextTranslation() {
        guard !virtual, translationTasks.isEmpty, !translationQueue.isEmpty else { return }
        let req = translationQueue.removeFirst()
        let translator = self.translator
        let hyTranslator = self.hyTranslator
        let audio = translationAudio.removeValue(forKey: req.unitID)
        let ticket = translationTickets.removeValue(forKey: req.unitID)
        let hyStreaming = self.hyStreaming
        let inbox = self.inbox
        // Fixture adapters model compute time; on the host clock that time is slept, so
        // paced and offline runs see the same latency model.
        let modeled = translator is DictionaryTranslator ? translatorLatency.nanoseconds(units: req.uncommittedSource.count) : 0
        translationTasks[req.unitID] = Task.detached {
            let started = HostClock.nowNs()
            let outcome: TranslationOutcome
            do {
                if let ticket, let hyStreaming { outcome = .success(try await hyStreaming.answer(ticket, unit: req.unitID)) }
                else if let hyTranslator, let audio { outcome = .success(try await hyTranslator.translate(unit: req.unitID, samples: audio)) }
                else { outcome = .success(try await translator.translate(req)) }
            } catch { outcome = .failure("\(error)") }
            let spent = HostClock.nowNs() - started
            if modeled > spent { try? await Task.sleep(nanoseconds: modeled - spent) }
            if Task.isCancelled { return }
            inbox.post(.input(.translation(req.unitID, outcome)))
        }
    }

    /// For dual-worker mode without a picture delay, the back slot starts only once the front is
    /// playing; this preserves order and keeps at most one future phrase buffered, since a phrase
    /// plays as soon as it is ready. With a picture delay every phrase waits for its own slot, so
    /// both workers synthesize ahead as soon as one is free (ADR-0017).
    var canStartSynthesis: Bool {
        if virtual { return true }
        let capacity = o.dualClone ? 2 : 1
        guard activeSynthesis.count < capacity else { return false }
        if o.dualClone && o.presentationDelayMs == 0 {
            guard plays.count < 2 else { return false }
            if !plays.isEmpty && !plays.values.contains(where: { $0.started }) { return false }
        }
        return true
    }

    func startSynthesis(_ job: SynthesisJob) {
        var lane = 0
        if !virtual {
            if !canStartSynthesis { synthesisQueue.append(job); return }
            lane = (0..<(o.dualClone ? 2 : 1)).first { !activeSynthesis.values.contains($0) }!
            activeSynthesis[job.unit] = lane
        }
        let synthesizer = self.synthesizer
        let voice = preparedVoices["\(job.speaker)/\(job.profileVersion)"]
            ?? PreparedVoice(speakerID: job.speaker, profileVersion: job.profileVersion, handle: 0)
        plays[job.unit] = PlayRecord(unit: job.unit)
        if virtual, let tone = synthesizer as? ToneSynthesizer {
            let chunks = Locked<[SynthesizedAudio]>([])
            let done = DispatchSemaphore(value: 0)
            Task.detached {
                var all: [SynthesizedAudio] = []
                do {
                    for try await c in tone.synthesize(unit: job.unit, text: job.text, voice: voice) { all.append(c) }
                } catch {
                    all = []
                }
                chunks.withLock { $0 = all }
                done.signal()
            }
            done.wait()
            var at = tone.firstChunk.nanoseconds(units: 0)
            for c in chunks.withLock({ $0 }) {
                schedule(.chunk(job, c), after: at)
                at += UInt64(Double(c.frames) / Double(c.format.sampleRate) * tone.realTimeFactor * 1e9)
            }
            schedule(.finished(job), after: at)
            return
        }
        let inbox = self.inbox
        let tone = synthesizer as? ToneSynthesizer
        let spoken = pronunciation.apply(job.text)
        let selectedLane = lane
        let fill = job.allowFill
        // ADR-0015: this unit's own source audio, so the cloned take keeps the actor's delivery of
        // this line rather than the average of their session. The adapter resamples it; nothing
        // about the clip is written, traced, or kept past the unit.
        var delivery: DeliveryClip? = nil
        if job.deliveryAllowed, let pcm = historySlice(job.span) {
            delivery = DeliveryClip(pcm: pcm, sampleRate: canonicalRate,
                                    sourceText: pipeline.sourceText(of: job.unit) ?? "")
        }
        tasks[job.unit] = Task.detached {
            do {
                if let tone { try await Task.sleep(nanoseconds: tone.firstChunk.nanoseconds(units: 0)) }
                let stream = if let dual = synthesizer as? DualCloneVoiceSynthesizer {
                    dual.synthesize(unit: job.unit, text: spoken, voice: voice, targetSeconds: job.targetSeconds,
                                    allowFill: fill, delivery: delivery, lane: selectedLane)
                } else {
                    synthesizer.synthesize(unit: job.unit, text: spoken, voice: voice, targetSeconds: job.targetSeconds,
                                           allowFill: fill, delivery: delivery)
                }
                for try await c in stream {
                    if Task.isCancelled { return }
                    inbox.post(.chunk(job, c))
                    if let tone {
                        let seconds = Double(c.frames) / Double(c.format.sampleRate) * tone.realTimeFactor
                        try await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
                    }
                }
                inbox.post(.finished(job))
            } catch {
                inbox.post(.failed(job, "\(error)"))
            }
        }
    }

    func deliver(_ event: DriverEvent) {
        switch event {
        case .input(let input):
            if case .translation(let id, _) = input {
                translationTasks[id] = nil
                // ADR-0018: budget the line into the silence after it, not into the next line.
                if o.asr == "audio", audioSegments.dipWindowSamples > 0, let span = unitSpans.removeValue(forKey: id) {
                    send(.followingSilence(span, seconds: silenceAfter(span)))
                }
            }
            send(input)
        case .languageIdentified(let id, let verdict):
            guard let span = lidRequests.removeValue(forKey: id) else { return }
            guard let verdict else { stats["lid_failed", default: 0] += 1; return }
            let target = o.direction.target == .mandarin ? "zh" : "en"
            let matches = verdict.language == target && verdict.probability >= 0.8
            stats[matches ? "lid_same_language" : "lid_translate", default: 0] += 1
            send(.sourceLanguage(span, matchesTarget: matches))
        case .chunk(let job, let audio):
            // A canceled or completed unit has no play record. This also rejects
            // late task messages without retaining every canceled ID for the session.
            guard plays[job.unit] != nil else { return }
            let header = PCMChunkHeader(unitID: job.unit, speakerID: job.speaker, profileVersion: job.profileVersion,
                                        sessionEpoch: job.epoch, playoutGeneration: job.generation,
                                        format: audio.format, span: job.span, isLast: false)
            currentChunk = audio
            send(.pcm(header, frames: audio.frames))
            currentChunk = nil
        case .finished(let job):
            tasks[job.unit] = nil
            activeSynthesis.removeValue(forKey: job.unit)
            let cloneStats = (synthesizer as? CloneVoiceSynthesizer)?.takeStats(for: job.unit)
                ?? (synthesizer as? DualCloneVoiceSynthesizer)?.takeStats(for: job.unit)
            if cloneStats?.truncated == true { stats["tts_truncated_units", default: 0] += 1 }
            if let retries = cloneStats?.retries, retries > 0 { stats["tts_silent_retries", default: 0] += retries }
            if let done = cloneStats, done.delivery != "absent" {
                // Conditioning is counted, and its refusal is named: a run that quietly stopped
                // conditioning every line would otherwise read as a normal one.
                stats[done.delivery == "line" ? "delivery_used_units" : "delivery_refused_units", default: 0] += 1
                if done.delivery != "line" { stats["delivery_\(done.delivery)", default: 0] += 1 }
                stats["delivery_prep_ms_max", default: 0] = max(stats["delivery_prep_ms_max", default: 0],
                                                                Int((done.deliveryPrepSeconds * 1_000).rounded()))
            }
            if let done = cloneStats {
                let ms = { (seconds: Double) in Int((seconds * 1000).rounded()) }
                var fields: [TraceEvent.Field] = [.unit(job.unit), .double("pace", done.pace),
                                                  .int("native_ms", ms(done.nativeSeconds)), .int("audio_ms", ms(done.audioSeconds)),
                                                  .int("quiet_removed_ms", ms(done.quietRemovedSeconds))]
                if done.budgetSeconds > 0 { fields.append(.int("budget_ms", ms(done.budgetSeconds))) }
                // The character-count cap stopped generation: this line lost its tail.
                if done.truncated { fields.append(.bool("truncated", true)) }
                // A silent first take was discarded and generated again with another seed.
                if done.retries > 0 { fields.append(.int("retries", done.retries)) }
                // Delivery conditioning (ADR-0015): the verdict and its cost, never the audio or
                // the actor's words.
                if done.delivery != "absent" {
                    fields.append(.string("delivery", done.delivery))
                    fields.append(.int("delivery_ms", ms(done.deliverySeconds)))
                    fields.append(.int("delivery_prep_ms", ms(done.deliveryPrepSeconds)))
                }
                if let target = job.targetSeconds { fields.append(.int("target_ms", ms(target))) }
                if !pronunciation.isEmpty { fields.append(.bool("respelled", pronunciation.apply(job.text) != job.text)) }
                trace.emit(TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch, gen: pipeline.gate.generation,
                                      kind: .ttsDone, fields: fields))
            }
            guard plays[job.unit] != nil else { return }
            if plays[job.unit]?.start == nil, held[job.unit]?.isEmpty != false {
                send(.synthesisFailed(job.unit, "synthesizer returned no audio"))
                return
            }
            plays[job.unit]?.complete = true
            if plays[job.unit]?.start != nil { flushResampler(job.unit) }
            send(.synthesisFinished(job.unit, generation: job.generation))
        case .failed(let job, let message):
            tasks[job.unit] = nil
            activeSynthesis.removeValue(forKey: job.unit)
            send(.synthesisFailed(job.unit, message))
        case .prepared(let voice, let seconds, let prepareNs):
            preparingReference = false
            preparedVoices["\(voice.speakerID)/\(voice.profileVersion)"] = voice
            if referencePolicies[voice.speakerID]?.isComplete == true,
               !pendingReferences.contains(where: { $0.speaker == voice.speakerID }) {
                referenceAudio[voice.speakerID] = nil
            }
            profileLog.append(["speaker": voice.speakerID.rawValue, "version": voice.profileVersion,
                               "reference_seconds": seconds, "prepare_seconds": Double(prepareNs) / 1e9,
                               "source_seconds": Double(srcFrames) / Double(canonicalRate)])
            log("voice profile \(pipeline.config.multiSpeaker ? "\(voice.speakerID) " : "")v\(voice.profileVersion) ready: "
                + "\(String(format: "%.1f", seconds)) s reference, prepared in \(String(format: "%.2f", Double(prepareNs) / 1e9)) s")
            send(.profileReady(voice.speakerID, version: voice.profileVersion))
            publishVoiceProgress(voice.speakerID, state: "ready", seconds: seconds)
            if !pendingReferences.isEmpty { prepareReference(pendingReferences.removeFirst()) }
        case .rememberedVoiceReady(let voice, let seconds, let prepareNs):
            memoryPreparing.remove(voice.speakerID)
            memoryReused.insert(voice.speakerID)
            referenceAudio[voice.speakerID] = nil
            referencePolicies[voice.speakerID] = nil
            preparedVoices["\(voice.speakerID)/\(voice.profileVersion)"] = voice
            stats["reference_memory_reused", default: 0] += 1
            profileLog.append(["speaker": voice.speakerID.rawValue, "version": voice.profileVersion,
                               "reference_seconds": seconds, "prepare_seconds": Double(prepareNs) / 1e9,
                               "source_seconds": Double(srcFrames) / Double(canonicalRate), "reused": true])
            publishVoiceProgress(voice.speakerID, state: "ready", seconds: seconds, reused: true)
            send(.profileReady(voice.speakerID, version: voice.profileVersion))
        case .rememberedVoiceFailed(let speaker):
            memoryPreparing.remove(speaker)
            memoryFailed.insert(speaker)
            stats["reference_memory_failed", default: 0] += 1
            publishVoiceProgress(speaker, state: "collecting")
        case .prepareFailed(let speaker, let version, let message):
            preparingReference = false
            if version == 1 {
                referencePolicies[speaker] = liveReferencePolicy(for: speaker)
                referenceAudio[speaker] = nil
                pendingReferences.removeAll { $0.speaker == speaker }
            }
            publishVoiceProgress(speaker, state: "collecting")
            stats["reference_prepare_failed", default: 0] += 1
            log(o.researchText ? "voice profile v\(version) failed: \(message)" : "voice profile v\(version) failed")
            trace.emit(TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch, gen: pipeline.gate.generation,
                                  kind: .error, fields: [.string("where", "profile"), .int("prof", version),
                                                         .string("message", o.researchText ? message : "prepare failed")]))
            if !pendingReferences.isEmpty { prepareReference(pendingReferences.removeFirst()) }
        case .voiceReady(let voice, let prepareNs):
            preparedVoices["\(voice.speakerID)/\(voice.profileVersion)"] = voice
            if let name = (synthesizer as? SystemVoiceSynthesizer)?.voiceName(for: voice) {
                log("speaker \(voice.speakerID) speaks with the \(name) voice")
            }
            profileLog.append(["speaker": voice.speakerID.rawValue, "version": voice.profileVersion,
                               "prepare_seconds": Double(prepareNs) / 1e9,
                               "source_seconds": Double(srcFrames) / Double(canonicalRate)])
            send(.profileReady(voice.speakerID, version: voice.profileVersion))
        case .voiceFailed(let speaker, let message):
            stats["voice_prepare_failed", default: 0] += 1
            log(o.researchText ? "voice for \(speaker) failed: \(message)" : "voice for \(speaker) failed")
        }
    }

    func write(_ audio: SynthesizedAudio, unit: UnitID) {
        let resampler = unitResamplers[unit] ?? StreamingResampler(inputRate: audio.format.sampleRate, outputRate: outRate)
        unitResamplers[unit] = resampler
        let mono = audio.format.channelCount == 1 ? audio.samples
            : WAVAudio(sampleRate: audio.format.sampleRate, channels: audio.format.channelCount, samples: audio.samples).mono()
        writeRing(resampler.process(mono), unit: unit)
    }

    func flushResampler(_ unit: UnitID) {
        guard let r = unitResamplers.removeValue(forKey: unit) else { return }
        writeRing(r.flush(), unit: unit)
    }

    func writeRing(_ samples: [Float], unit: UnitID) {
        let ring = a2a_engine_translated_ring(engine)
        if plays[unit] == nil { plays[unit] = PlayRecord(unit: unit) }
        if plays[unit]?.start == nil { plays[unit]?.start = a2a_ring_total_written(ring) }
        guard !samples.isEmpty else { plays[unit]?.end = a2a_ring_total_written(ring); return }
        // The translated bus is mono; the mixer feeds it to every output channel.
        let written = samples.withUnsafeBufferPointer { a2a_ring_write(ring, $0.baseAddress!, UInt32($0.count)) }
        if translatedWriter != nil { unconsumed += samples[0..<Int(written)] }
        if Int(written) < samples.count {
            trace.emit(TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch, gen: pipeline.gate.generation,
                                  kind: .error, fields: [.string("where", "translated_bus"), .string("message", "overflow"),
                                                         .int("frames", samples.count - Int(written))]))
        }
        plays[unit]?.end = a2a_ring_total_written(ring)
    }

    // MARK: Output

    func render(_ input: [Float]?, frames: Int) {
        if tap != nil {
            // The device IOProc already mixed and played; follow its counters.
            let consumed = a2a_engine_translated_consumed(engine)
            let outNow = Int64(a2a_engine_output_frames(engine))
            trackPlayout(consumedBefore: lastConsumed, consumed: consumed, outBefore: lastOut)
            lastConsumed = consumed
            lastOut = outNow
            writeRendered()
            return
        }
        var out = [Float](repeating: 0, count: frames)
        let consumedBefore = a2a_engine_translated_consumed(engine)
        let outBefore = outFrames
        if let input {
            input.withUnsafeBufferPointer { inp in
                out.withUnsafeMutableBufferPointer { o in
                    a2a_engine_process(engine, inp.baseAddress, UInt32(frames), nil, o.baseAddress, UInt32(frames), nil)
                }
            }
        } else {
            out.withUnsafeMutableBufferPointer { o in
                a2a_engine_process(engine, nil, 0, nil, o.baseAddress, UInt32(frames), nil)
            }
        }
        // Keep the capture ring from filling; the pipeline reads source audio directly.
        _ = a2a_ring_skip(a2a_engine_capture_ring(engine), UInt32(frames))
        outFrames += Int64(frames)
        writeRendered()
        outWriter?.append(out)
        if let stemWriter {
            var interleaved = [Float](repeating: 0, count: frames * 2)
            for i in 0..<frames {
                interleaved[2 * i] = input?[i] ?? 0
                interleaved[2 * i + 1] = out[i]
            }
            stemWriter.append(interleaved)
        }
        trace.emit(TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch, gen: pipeline.gate.generation,
                              kind: .bufOutput, fields: [.int64("out_sample", outBefore), .int("frames", frames),
                                                         .int("rate", canonicalRate)]))

        trackPlayout(consumedBefore: consumedBefore, consumed: a2a_engine_translated_consumed(engine), outBefore: outBefore)
    }

    func writeRendered() {
        guard let renderedWriter, let ring = a2a_engine_rendered_ring(engine) else { return }
        while true {
            let n = renderedBuffer.withUnsafeMutableBufferPointer {
                a2a_ring_read(ring, $0.baseAddress!, UInt32($0.count / outChannels))
            }
            if n == 0 { break }
            renderedBuffer.withUnsafeBufferPointer {
                renderedWriter.append(UnsafeBufferPointer(rebasing: $0[..<(Int(n) * outChannels)]))
            }
            renderedFrames += UInt64(n)
        }
    }

    /// Appends the translated audio the mixer has consumed since the last call to --translated-out.
    func writePlayed() {
        guard let translatedWriter else { return }
        let consumed = a2a_engine_translated_consumed(engine)
        guard consumed > unconsumedBase else { return }
        let n = min(unconsumed.count - unconsumedHead, Int(consumed - unconsumedBase))
        guard n > 0 else { return }
        unconsumed.withUnsafeBufferPointer { translatedWriter.append(UnsafeBufferPointer(rebasing: $0[unconsumedHead..<unconsumedHead + n])) }
        unconsumedHead += n
        unconsumedBase += UInt64(n)
        if unconsumedHead >= 1 << 20 {
            unconsumed.removeFirst(unconsumedHead)
            unconsumedHead = 0
        }
    }

    /// Turns mixer consumption of the translated ring into play.start / play.end / underrun.
    func trackPlayout(consumedBefore: UInt64, consumed: UInt64, outBefore: Int64) {
        writePlayed()
        let ringEmpty = a2a_ring_readable(a2a_engine_translated_ring(engine)) == 0
        func outSample(_ ringPosition: UInt64) -> Int64 {
            outBefore + max(0, Int64(ringPosition) - Int64(consumedBefore))
        }
        for unit in plays.keys.sorted() {
            guard var p = plays[unit], let start = p.start else { continue }
            if !p.started, start < consumed {
                p.started = true
                plays[unit] = p
                send(.playStarted(unit, outSample: outSample(start)))
                p = plays[unit] ?? p
            }
            if p.started, p.complete, !p.ended, p.end <= consumed, unitResamplers[unit] == nil {
                p.ended = true
                plays[unit] = nil
                send(.playEnded(unit, outSample: outSample(p.end)))
                continue
            }
            if p.started, !p.complete, ringEmpty, !p.underrunReported {
                p.underrunReported = true
                plays[unit] = p
                stats["underruns", default: 0] += 1
                send(.underrun)
            }
        }
    }

    // MARK: Loop

    var idle: Bool {
        deliveries.isEmpty && inbox.isEmpty && tasks.isEmpty && translationTasks.isEmpty
            && translationQueue.isEmpty && synthesisQueue.isEmpty
            && plays.isEmpty && held.isEmpty
    }

    /// `synthetic`: silence that is not source time (a pause flush). It stays out of the separated
    /// bed, whose FIFO depth is the picture delay, and renders no output span of its own.
    func step(_ chunk: [Float]?, frames: Int, synthetic: Bool = false) {
        now = clock()
        if chunk != nil {
            idleSince = nil
            pauseFlushed = false
        }
        if let mode = playbackControls?.takeLatest() { send(.playbackMode(mode)) }
        let start = srcFrames
        let samples = chunk ?? [Float](repeating: 0, count: frames)
        if !synthetic, let separator = backgroundSeparator, !backgroundFailed {
            do {
                let (bed, voice) = try separator.separate(samples)
                voiceChunk = voice
                backgroundWriter?.append(bed)
                if restoresVoice {
                    // Voice output i is source sample i - latency.
                    if voiceHistory.isEmpty { voiceHistoryStart = bedOut + Int64(bedHold.reduce(0) { $0 + $1.bed.count }) - voiceLagSamples }
                    voiceHistory += voice
                    let keep = canonicalRate * 30
                    if voiceHistory.count > keep + canonicalRate * 10 {
                        voiceHistoryStart += Int64(voiceHistory.count - keep)
                        voiceHistory.removeFirst(voiceHistory.count - keep)
                    }
                }
                bedHold.append((bed, voice, now))
                try flushBed(now: now)
            } catch {
                failBackground()
            }
        }
        srcFrames += Int64(frames)
        let span = SourceSpan(start: start, end: srcFrames)
        if chunk != nil { send(.captured(span: span)) }

        if !o.captionsOnly, o.tts != "clone", !profileReady, Int(start) * 1_000 / canonicalRate >= o.profileReadyMs {
            profileReady = true
            // Multi-speaker sessions too: speech the tracker never labels still has a voice.
            if !knownSpeakers.contains(pipeline.config.defaultSpeaker) { send(.profileReady(SpeakerID("S1"), version: 1)) }
        }

        if let chunk { sourceWriter?.append(chunk) }
        if keepsHistory {
            history += samples
            let keep = canonicalRate * 30
            if history.count > keep + canonicalRate * 10 {
                historyStart += Int64(history.count - keep)
                history.removeFirst(history.count - keep)
            }
        } else if !history.isEmpty {
            history.removeAll()
        }
        if let recordDrain {
            _ = recordDrain.drain(context: RTTraceContext(session: o.session, epoch: pipeline.gate.epoch,
                                                          generation: pipeline.gate.generation, deviceRate: Double(outRate)),
                                  into: trace)
            if recordDrain.invalidTimestamps > 0 || (tap?.counters.captureOverflowFrames ?? 0) > 0 {
                runtimeFailed = true
                send(.discontinuity("capture timing invalid or audio lost"))
                return
            }
        }
        let lowRate = to16k.process(samples)
        let frame16 = AudioFrame16k(span: span, samples: lowRate)
        let vadEvents = vad.process(frame16)
        for event in vadEvents {
            send(.vad(event))
        }
        // Labels reach the reducer before any commit they cover.
        for event in tracker?.process(frame16, vad: vadEvents) ?? [] {
            send(.speaker(event))
            noteSpeaker(event)
        }
        if o.asr == "audio" {
            let segments = segmentSpans(mix: frame16, mixEvents: vadEvents, samples: samples.count)
            // Before the reducer commits them, so each closed span's answer is already under way.
            if let hyStreaming { feedHy(hyStreaming, closed: segments) }
            for segment in segments {
                send(.audioSegment(segment))
                recentSegments.append(segment)
                if recentSegments.count > 32 { recentSegments.removeFirst() }
                requestLanguage(segment)
                if acquiresReference { deferredAudioReferences.append((segment, now)) }
            }
            if !deferredAudioReferences.isEmpty { offerAudioReferences() }
        }
        recognizer.accept(frame16)
        for h in recognizer.drainHypotheses() {
            send(.hypothesis(h))
            acquireReference(h)
        }
        if !deferredReferences.isEmpty { offerDeferredReferences() }
        if let error = (synthesizer as? CloneVoiceSynthesizer)?.failure
            ?? (synthesizer as? DualCloneVoiceSynthesizer)?.failure, !runtimeFailed {
            log(o.researchText ? "voice worker stopped: \(error)" : "voice worker stopped")
            runtimeFailed = true
            send(.workerHealthy(false))
        }

        for e in inbox.drain() { deliver(e) }
        deliveries.sort { ($0.at, $0.seq) < ($1.at, $1.seq) }
        while let first = deliveries.first, first.at <= now {
            deliveries.removeFirst()
            deliver(first.event)
        }

        render(chunk == nil ? nil : samples, frames: synthetic ? 0 : frames)

        if now >= lastTick + 100_000_000 {
            lastTick = now
            let backlog = Double(a2a_ring_readable(a2a_engine_translated_ring(engine))) / Double(outRate)
            send(.translatedBacklog(seconds: backlog))
            send(.tick)
        }
        if now >= lastResource + 1_000_000_000 {
            lastResource = now
            emitResource()
        }
        trace.flush()
    }

    /// A period without capture: the global tap gets no callbacks while nothing plays, for
    /// example a paused video. Model results, playout tracking, ticks and phrase slots still
    /// advance by the clock (a delayed picture keeps showing the last D seconds meanwhile);
    /// nothing touches the source timeline, VAD, tracker or separator.
    func idleStep(frames: Int) {
        now = clock()
        // A paused tap delivers no frames, not silence, so the phrase spoken just before the
        // pause would stay open until playback resumes and miss its slot. Once per pause, 0.6 s
        // of silence closes it through the usual VAD and segment rules. It gets no capture
        // anchor (slots stay on captured time) and no separated bed.
        let since = idleSince ?? now
        idleSince = since
        if !pauseFlushed, now - since >= 300_000_000 {
            pauseFlushed = true
            let flush = canonicalRate * 3 / 5
            step(nil, frames: flush, synthetic: true)
            syntheticFrames += Int64(flush)
            idleSince = since
            pauseFlushed = true
            return
        }
        if let mode = playbackControls?.takeLatest() { send(.playbackMode(mode)) }
        for e in inbox.drain() { deliver(e) }
        deliveries.sort { ($0.at, $0.seq) < ($1.at, $1.seq) }
        while let first = deliveries.first, first.at <= now {
            deliveries.removeFirst()
            deliver(first.event)
        }
        // ADR-0018: the held bed keeps its wall-clock delay through a pause.
        if bedHoldNs > 0, backgroundSeparator != nil, !backgroundFailed {
            do { try flushBed(now: now) } catch { failBackground() }
        }
        // The device renders on its own; a file replay renders the same span of output.
        render(nil, frames: tap == nil ? frames : 0)
        // Every input re-checks slots, so due phrases start within one idle step.
        send(.translatedBacklog(seconds: Double(a2a_ring_readable(a2a_engine_translated_ring(engine))) / Double(outRate)))
        if now >= lastTick + 100_000_000 {
            lastTick = now
            send(.tick)
        }
        if now >= lastResource + 1_000_000_000 {
            lastResource = now
            emitResource()
        }
        trace.flush()
    }

    func emitResource() {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard kr == KERN_SUCCESS else { return }
        trace.emit(TraceEvent(tNs: now, session: o.session, epoch: pipeline.gate.epoch, gen: pipeline.gate.generation,
                              kind: .resourceSample, fields: [
                                .string("process", "pipeline"),
                                .int64("phys_footprint_bytes", Int64(info.phys_footprint)),
                                .int64("resident_bytes", Int64(info.resident_size)),
                              ]))
    }

    func run(source: Source) async {
        t0 = virtual ? 1_000_000_000 : HostClock.nowNs()
        now = clock()
        send(.arm)
        // Honor clicks made during model warm-up before the first playback decision.
        if let mode = playbackControls?.takeLatest() { send(.playbackMode(mode)) }
        send(.armSucceeded)
        let frames = source.chunk
        let tailFrames = Int(o.tailSeconds * Double(canonicalRate))
        var tail = 0
        if tap != nil {
            source.startLive(seconds: o.seconds)
            while let chunk = await source.nextLive() {
                autoreleasepool { chunk.isEmpty ? idleStep(frames: 0) : step(chunk, frames: chunk.count) }
                if runtimeFailed { break }
            }
            log("stopping: finishing in-flight units over live audio (up to \(o.tailSeconds) s)")
            recognizer.requestFinalize()
            source.startTail(seconds: o.tailSeconds)
            while !runtimeFailed, source.failure == nil, tail < tailFrames, let chunk = await source.nextLive() {
                autoreleasepool { chunk.isEmpty ? idleStep(frames: 0) : step(chunk, frames: chunk.count) }
                tail += chunk.count
                if tail >= canonicalRate, idle { break }
            }
        } else {
            let realtimeFile = !virtual && source.totalFrames != nil && o.pace == "realtime"
            let wallStart = HostClock.nowNs()
            var gap = o.captureGap
            while let chunk = await source.next() {
                if let g = gap, Double(srcFrames) / Double(canonicalRate) >= g.at {
                    gap = nil
                    for _ in 0..<Int((g.seconds * 50).rounded()) {
                        idleNs += 20_000_000
                        autoreleasepool { idleStep(frames: canonicalRate / 50) }
                    }
                }
                autoreleasepool { step(chunk, frames: chunk.count) }
                if runtimeFailed { break }
                if realtimeFile { pace(wallStart) }
            }
            recognizer.requestFinalize()
            // Trailing silence lets finals, translation, and playout complete.
            while !runtimeFailed, tail < tailFrames {
                autoreleasepool { step(nil, frames: frames) }
                tail += frames
                if !virtual { pace(wallStart) }
                if tail >= canonicalRate, idle { break }
            }
        }
        now = clock()
        if let failure = source.failure {
            runtimeFailed = true
            log(failure)
            send(.discontinuity(failure))
        }
        send(.stop)
        (synthesizer as? CloneVoiceSynthesizer)?.shutdown()
        (synthesizer as? DualCloneVoiceSynthesizer)?.shutdown()
        (translator as? ContextualTranslator)?.shutdown()
        languageIdentifier?.shutdown()
        if tap != nil { settleGains(timeoutMs: pipeline.config.health.rampMs + 50) }
        // Freeze the producer before the final monitor drain so every rendered frame
        // has exactly one sample in the diagnostic file, with no callback race at close.
        tap?.stop()
        writeRendered()
        trace.close()
        voiceMemory?.flush()
        try? outWriter?.close()
        try? stemWriter?.close()
        try? sourceWriter?.close()
        try? backgroundWriter?.close()
        try? translatedWriter?.close()
        try? renderedWriter?.close()
        summary()
    }

    /// Live stop: the device keeps rendering until the closing gain ramp lands, so speech
    /// still playing fades out instead of being cut off when the device stops.
    func settleGains(timeoutMs: Int) {
        let deadline = HostClock.nowNs() + UInt64(timeoutMs) * 1_000_000
        func settled() -> Bool {
            abs(a2a_engine_current_original_gain(engine) - gainTarget.original) < 1e-3
                && abs(a2a_engine_current_translated_gain(engine) - gainTarget.translated) < 1e-3
                && abs(a2a_engine_current_background_gain(engine) - backgroundGainTarget) < 1e-3
        }
        while !settled(), HostClock.nowNs() < deadline { usleep(2_000) }
    }

    func pace(_ wallStart: UInt64) {
        let target = wallStart + UInt64(srcFrames) * 1_000_000_000 / UInt64(canonicalRate)
        let current = HostClock.nowNs()
        if target > current { usleep(useconds_t((target - current) / 1_000)) }
    }

    func summary() {
        var s: [String: Any] = [
            "session": o.session, "clock": o.clock, "asr": o.asr, "mt": o.mt, "tts": o.tts, "mode": "file",
            "direction": o.direction.rawValue, "pair": o.direction.tracePair,
            "tts_preserves_speaker_identity": synthesizer.capabilities.preservesSpeakerIdentity,
            // No synthesizer has passed the identity study (M2); never gate evidence.
            "tts_identity_qualified": false,
            "source_frames": srcFrames, "output_frames": outFrames, "trace_events": trace.eventCount,
            "pcm_dropped": stats["pcm_dropped", default: 0], "underruns": stats["underruns", default: 0],
            "final_state": pipeline.state.description,
            "runtime_failed": runtimeFailed, "captions_only": o.captionsOnly,
            "translation_only": o.translationOnly, "output_sample_rate": outRate,
            "playback": pipeline.playbackMode.rawValue,
            "fluent": o.fluent,
            "presentation_delay_ms": o.presentationDelayMs,
            "turn_fill": o.turnFill,
            "slot": slotSummary(),
            "adaptive_prebuffer": o.adaptivePrebuffer,
            "quality_first": o.qualityFirst,
            "background": ["enabled": o.background, "failed": backgroundFailed, "frames": backgroundFrames] as [String: Any],
            "rendered_frames_recorded": renderedFrames,
            "rendered_capture_overflow_frames": a2a_engine_rendered_ring(engine).map(a2a_ring_overflow_frames) ?? 0,
            "silence_hypotheses_rejected": stats["silence_hypotheses_rejected", default: 0],
            "translated_starved_frames": a2a_engine_translated_starved_frames(engine),
            "max_debt_seconds": pipeline.config.health.maxDebtSeconds,
            "max_source_lag_seconds": pipeline.config.health.maxSourceLagSeconds,
            "max_unit_keys": pipeline.config.commit.maxUnitKeys,
            "max_unit_seconds": pipeline.config.commit.maxUnitSourceSeconds,
            "passthrough": ["enabled": restoresVoice, "identifier": languageIdentifier != nil,
                            "hold_seconds": Double(bedHoldNs) / 1e9,
                            "same_language": stats["lid_same_language", default: 0],
                            "translated": stats["lid_translate", default: 0], "failed": stats["lid_failed", default: 0],
                            "voice_restored_units": stats["voice_restored_units", default: 0],
                            "voice_restored_late": stats["voice_restored_late", default: 0]] as [String: Any],
            "audio_units": ["max_seconds": Double(audioSegments.maxSamples) / Double(canonicalRate),
                            "detector": segmentVAD == nil ? "session" : (voiceTo16k == nil ? "mix" : "voice"),
                            "pause_closes": audioSegments.pauseCloses, "cap_closes": audioSegments.capCloses] as [String: Any],
            "word_cuts": options.wordCuts,
            "speaker_carry_seconds": pipeline.config.unlabeledSpeakerCarrySeconds,
            "speaker_carry_after_pause_seconds": pipeline.config.unlabeledSpeakerCarryAfterPauseSeconds,
            "speaker_carry_max_piece_seconds": pipeline.config.unlabeledSpeakerCarryMaxPieceSeconds,
            "speaker_carried_units": stats["speaker_carried_units", default: 0],
            "shorten_targets": pipeline.config.validator.shortensOverBudgetTargets,
            "shortened_targets": stats["shortened_targets", default: 0],
            "filler_silence": pipeline.config.validator.silencesFillers,
            "silence_units": stats["silence_units", default: 0],
            "tts_truncated_units": stats["tts_truncated_units", default: 0],
            "tts_silent_retries": stats["tts_silent_retries", default: 0],
            "speakers": ["mode": o.speakers, "detected": tracker == nil ? 1 : knownSpeakers.count] as [String: Any],
        ]
        if tracker != nil, o.tts != "clone" { s["voices"] = profileLog }
        if o.tts == "clone" {
            let info = (synthesizer as? CloneVoiceSynthesizer)?.info ?? (synthesizer as? DualCloneVoiceSynthesizer)?.info
            s["clone"] = ["model": info?.model ?? "", "selected_model": o.cloneModel.rawValue,
                          "audio_output_streaming": synthesizer.capabilities.audioOutputStreaming,
                          "conditioning": o.cloneMode.rawValue, "workers": o.dualClone ? 2 : 1,
                          "delivery_conditioning": o.cloneDelivery,
                          "session_voice_locked": pipeline.config.pinSpeakerProfiles,
                          "speaker_attribution_required": pipeline.config.requireSpeakerAttribution && pipeline.config.multiSpeaker,
                          "reference": o.voiceRef == nil ? "acquired" : "fixed", "profiles": profileLog,
                          "load_seconds": info?.loadSeconds ?? 0, "warmup_seconds": info?.warmupSeconds ?? 0,
                          "reference_stats": stats.filter { $0.key.hasPrefix("reference_") }] as [String: Any]
        }
        if let tap {
            let c = tap.counters
            s["mode"] = "tap"
            s["output_frames"] = c.outputFrames
            s["tap"] = ["bundle_id": tap.source.bundleID, "global": tap.source.global,
                        "pids": tap.source.members.map { Int($0.process.pid) },
                        "muted": tap.tapIsMuted, "device_rate": tap.sampleRate, "callbacks": c.callbacks,
                        "captured_frames": c.capturedFrames, "capture_overflow_frames": c.captureOverflowFrames,
                        "output_frames": c.outputFrames, "record_drops": c.recordDrops,
                        "output_device": tap.outputDevice.name] as [String: Any]
        }
        if let data = try? JSONSerialization.data(withJSONObject: s, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }
}

/// Starts the resident voice worker and waits for its warm-up. Runs before any tap exists,
/// so a slow or failed load never leaves the source muted and default signals still apply.
func startCloneWorker(_ o: Options) async -> CloneVoiceSynthesizer {
    let (executable, arguments) = cloneWorkerCommand(o)
    log("starting \(o.cloneModel.name) voice worker (\(o.cloneMode.rawValue))")
    do {
        let clone = try await CloneVoiceSynthesizer(executable: executable, arguments: arguments, conditioning: o.cloneMode,
                                                   targetLanguage: o.direction.target)
        if let info = clone.info {
            log("voice worker ready: \(info.model), load \(String(format: "%.1f", info.loadSeconds)) s, "
                + "warm-up \(String(format: "%.1f", info.warmupSeconds)) s")
        }
        return clone
    } catch {
        fail("voice worker failed: \(error)")
    }
}

/// The repository that contains this executable (development builds and the local app bundle).
func repositoryRoot() -> URL? {
    var dir = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0]))
        .resolvingSymlinksInPath().deletingLastPathComponent()
    while dir.path != "/" {
        if FileManager.default.fileExists(atPath: dir.appendingPathComponent("research/tts_worker.py").path) { return dir }
        dir = dir.deletingLastPathComponent()
    }
    return nil
}

/// Speech translation straight from source audio, by a Hy-SimulMT server or the worker on this Mac.
protocol AudioTranslator: Sendable {
    func translate(unit: UnitID, samples: [Float]) async throws -> TranslationCandidate
}
extension HySimulTranslator: AudioTranslator {}
extension HyLocalTranslator: AudioTranslator {}

/// Starts Hy-SimulMT on this Mac: explicit paths, or the worker and model in the repository.
/// Loading takes a few seconds with the GGUF in the page cache.
func startHyWorker(_ o: Options) -> Task<HyLocalTranslator, Error> {
    let root = o.hyWorker == nil || o.hyModel == nil ? repositoryRoot() : nil
    guard let worker = o.hyWorker.map(URL.init(fileURLWithPath:)) ?? root.map(HyLocalSetup.worker(root:)),
          FileManager.default.isExecutableFile(atPath: worker.path) else {
        fail("Hy-SimulMT worker failed: no worker; build it with scripts/build_hy_local.sh or pass --hy-worker")
    }
    guard let model = o.hyModel.map(URL.init(fileURLWithPath:)) ?? root.flatMap(HyLocalSetup.model(root:)) else {
        fail("Hy-SimulMT worker failed: no model; fetch hy-simulmt-30b-a3b-q4km-gguf or pass --hy-model")
    }
    let direction = o.direction.rawValue
    log("starting Hy-SimulMT worker (\(model.lastPathComponent))")
    return Task.detached { try await HyLocalTranslator(worker: worker, model: model, direction: direction) }
}

/// The clone worker command: explicit paths, or the research environment in the repository
/// that contains this executable.
func cloneWorkerCommand(_ o: Options) -> (URL, [String]) {
    let root = (o.cloneWorker == nil || (o.clonePython == nil && !o.cloneFake)) ? repositoryRoot() : nil
    guard let worker = o.cloneWorker ?? root?.appendingPathComponent("research/tts_worker.py").path else {
        fail("cannot find research/tts_worker.py; pass --clone-worker")
    }
    let python = o.clonePython ?? (o.cloneFake ? "/usr/bin/python3" : root?.appendingPathComponent("research/.venv/bin/python").path)
    guard let python, FileManager.default.isExecutableFile(atPath: python) else {
        fail("no Python for the voice worker; run `uv sync --project research --locked` or pass --clone-python")
    }
    var arguments = [worker, "--model", o.cloneModel.rawValue]
    if let transcriber = o.referenceTranscriber {
        arguments += ["--reference-transcriber", transcriber, "--reference-locale", o.direction.source.speechLocaleIdentifier]
    }
    if o.cloneFake { arguments.append("--fake") }
    if o.fluent, o.cloneModel == .qwen { arguments += ["--temperature", "0.6"] }
    if o.qualityFirst { arguments.append("--longer-speech-budget") }
    if o.researchText { arguments.append("--research-text") }
    return (URL(fileURLWithPath: python), arguments)
}

/// Resolves the target app (retrying while it opens audio output) and prepares a muted-
/// when-tapped session that re-renders through our mixer. The device starts later, once
/// the models are loaded, so the capture ring cannot overflow during startup.
func openTap(_ o: Options, timeout: Double = 20) throws -> TapSession {
    if CapturePermission.status() == .denied {
        fail("System Audio Recording is denied for the responsible app (the terminal); enable it in System Settings")
    }
    let deadline = Date().addingTimeInterval(timeout)
    while true {
        do {
            let processes = try AudioProcessList.current()
            let source = if let pid = o.tapPid {
                try SourceResolver.resolve(pid: pid, processes: processes)
            } else if o.tapGlobal {
                SourceResolver.systemAudio(processes: processes)
            } else {
                try SourceResolver.resolve(bundleID: o.tapBundle ?? "", processes: processes)
            }
            var options = TapSessionOptions()
            options.mute = o.captionsOnly ? .unmuted : (o.translationOnly ? .muted : .mutedWhenTapped)
            options.renderOutput = !o.captionsOnly
            options.translatedRingSeconds = translatedRingSeconds
            // The original bus is delayed only to follow a delayed picture (ADR-0012); only
            // --rendered-out reads the monitor.
            options.maxDelayMs = o.presentationDelayMs > 0 ? o.presentationDelayMs + 1_000 : 0
            options.delayMs = o.presentationDelayMs
            options.renderedRingSeconds = o.renderedOut == nil ? 0 : 4
            options.name = "A2A Live"
            let session = try TapSession(source: source, options: options)
            log(source.global
                ? "tapping the whole output mix at \(Int(session.sampleRate)) Hz, output \(session.outputDevice.name), "
                    + "excluding our own pids \(source.excluded.map { $0.process.pid })"
                : "tapping \(source.bundleID) pids \(source.members.map { $0.process.pid }) at \(Int(session.sampleRate)) Hz, "
                    + "output \(session.outputDevice.name)")
            return session
        } catch where Date() < deadline {
            usleep(200_000)
        }
    }
}

let options = Options.parse(CommandLine.arguments)
// The cloud key is sent once over the private control pipe, never in arguments,
// environment, trace, or a file. Read it before starting the control reader.
let cloudKey = options.qwenCloud ? readCloudKey() : nil
let playbackControls = options.controlStdin ? PlaybackControls() : nil
// Hy-SimulMT on this Mac loads while the voice workers do. Both are ready before any tap
// exists, so a failed load never leaves the source muted, and no span waits on a load.
let hyLoad = options.mt == "hy-local" ? startHyWorker(options) : nil
let clone: VoiceSynthesizer?
if options.tts == "clone" {
    let first = await startCloneWorker(options)
    if options.dualClone {
        let second = await startCloneWorker(options)
        clone = DualCloneVoiceSynthesizer(first, second)
    } else { clone = first }
} else { clone = nil }
let hyLocal: HyLocalTranslator?
if let hyLoad {
    do {
        let hy = try await hyLoad.value
        if let info = hy.info {
            log("Hy-SimulMT worker ready: \(info.model), load \(String(format: "%.1f", info.loadSeconds)) s, "
                + "warm-up \(String(format: "%.1f", info.warmupSeconds)) s"
                + (info.streamsSpans || options.asr != "audio" ? "" : " (older worker: spans are sent once they close; rebuild with scripts/build_hy_local.sh)"))
        }
        hyLocal = hy
    } catch {
        fail("Hy-SimulMT worker failed: \(error)")
    }
} else { hyLocal = nil }
do {
    let cloud = try await (options.qwenCloud ? QwenCloudStream.connect(key: cloudKey ?? "", region: options.cloudRegion, direction: options.direction) : nil)
    let tap = options.isTap ? try openTap(options) : nil
    tapToRelease = tap
    let source = try Source(options: options, tap: tap)
    // A muted tap exists from here on: stop requests must reach teardown, not kill the process.
    // The remaining start-up (Apple model sessions, a fixed voice profile) takes about a second.
    var signalSources: [DispatchSourceSignal] = []
    if tap != nil {
        let stop = source.stopRequested
        // A top-level async CLI is MainActor-isolated. Dispatching an inferred actor
        // closure on .global() traps when a signal arrives; make this lock-only handler
        // explicitly Sendable and independent of that executor.
        let requestStop: @Sendable () -> Void = { stop.withLock { $0 = true } }
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .global())
            source.setEventHandler(handler: requestStop)
            source.resume()
            signalSources.append(source)
        }
    }
    if let cloud, let tap {
        defer { signalSources.forEach { $0.cancel() }; tap.teardown() }
        try await cloud.run(tap: tap, source: source, controls: playbackControls, initialMode: options.playbackMode, direction: options.direction, tracePath: options.trace)
        tap.teardown()
        exit(0)
    }
    let driver = try await Driver(options: options, tap: tap, clone: clone, hyLocal: hyLocal, playbackControls: playbackControls)
    if let tap {
        defer { signalSources.forEach { $0.cancel() }; tap.teardown() }
        if source.stopRequested.withLock({ $0 }) {
            log("stopped during start-up")
            tap.teardown()
            exit(0)
        }
        try tap.start()
        log("live: translating; Ctrl-C stops and restores the app's audio")
        await driver.run(source: source)
        tap.teardown()
        if driver.runtimeFailed { exit(1) }
    } else {
        await driver.run(source: source)
        if driver.runtimeFailed { exit(1) }
    }
} catch {
    fail("\(error)")
}
