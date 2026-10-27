// Isolated research probes. No taps, playout policy, or audio-device output.
// Raw text/audio outputs are explicit files; keep them under .local/ or restricted storage.
import A2ACore
import A2AMedia
import A2AModels
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a2a-stage-probe: \(message)\n".utf8))
    exit(2)
}
let args = CommandLine.arguments
guard args.count > 1 else { fail("pass asr, mt, tts, resample, or speakers with --input FILE --out DIR") }
let stage = args[1]
func value(_ flag: String) -> String? {
    args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
}
guard let direction = TranslationDirection(rawValue: value("--direction") ?? "zh-en") else {
    fail("--direction is zh-en or en-zh")
}
guard let inputPath = value("--input"), let outPath = value("--out") else { fail("--input and --out are required") }
let inputURL = URL(fileURLWithPath: inputPath)
let outURL = URL(fileURLWithPath: outPath, isDirectory: true)
guard !FileManager.default.fileExists(atPath: outPath) else { fail("output directory already exists") }
try FileManager.default.createDirectory(at: outURL, withIntermediateDirectories: true)
let started = ContinuousClock.now

struct Item: Codable { let id: Int; let text: String }
struct Items: Codable { let items: [Item] }
func loadItems() throws -> [Item] {
    try JSONDecoder().decode(Items.self, from: Data(contentsOf: inputURL)).items
}
func writeJSON(_ value: Any, _ name: String = "result.json") throws {
    try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
        .write(to: outURL.appendingPathComponent(name))
}
func elapsedMs(_ begin: ContinuousClock.Instant) -> Double {
    let components = begin.duration(to: .now).components
    return Double(components.seconds) * 1000 + Double(components.attoseconds) / 1e15
}

do {
    switch stage {
    case "asr":
        let chunkMs = Int(value("--chunk-ms") ?? "20") ?? 20
        guard (10...100).contains(chunkMs) else { fail("--chunk-ms must be 10...100") }
        let realtime = args.contains("--realtime")
        let finalizeVAD = args.contains("--vad-finalize")
        var samples = try WAV.read(inputURL).mono()
        let source = try WAV.read(inputURL)
        if source.sampleRate != 48_000 { samples = StreamingResampler.resample(samples, from: source.sampleRate, to: 48_000) }
        samples += [Float](repeating: 0, count: 48_000) // let a boundary finish without truncation
        let locale = value("--locale") ?? direction.source.speechLocaleIdentifier
        let recognizer = try await AppleSpeechRecognizer(locale: Locale(identifier: locale))
        let resampler = StreamingResampler(inputRate: 48_000, outputRate: 16_000)
        var vad = EnergyVAD()
        var rows: [[String: Any]] = []
        var vadFinalizations = 0
        let wall = ContinuousClock.now
        let chunk = chunkMs * 48
        func drain() {
            for h in recognizer.drainHypotheses() {
                let spans = h.tokens.compactMap(\.span)
                rows.append(["utterance": h.utterance, "final": h.isFinal, "text": h.text,
                             "start_s": Double(spans.map(\.start).min() ?? 0) / 48_000,
                             "end_s": Double(spans.map(\.end).max() ?? 0) / 48_000,
                             "frontier_s": Double(h.audioFrontier) / 48_000,
                             // Token timing drives clause splits on pauses (CommitPolicy.streamingClauses).
                             "tokens": h.tokens.map { t -> [Any] in
                                 [t.text, t.span.map { Double($0.start) / 48_000 } ?? NSNull(),
                                  t.span.map { Double($0.end) / 48_000 } ?? NSNull()]
                             }])
            }
        }
        for offset in stride(from: 0, to: samples.count, by: chunk) {
            let end = min(samples.count, offset + chunk)
            let frame = AudioFrame16k(span: SourceSpan(start: Int64(offset), end: Int64(end)),
                                     samples: resampler.process(Array(samples[offset..<end])))
            if finalizeVAD {
                for event in vad.process(frame) {
                    if case .speechEnd = event { recognizer.requestFinalize(); vadFinalizations += 1 }
                }
            }
            recognizer.accept(frame)
            drain()
            if let error = recognizer.lastError { fail(error) }
            if realtime {
                let target = wall.advanced(by: .nanoseconds(Int64(Double(end) / 48_000 * 1e9)))
                try await ContinuousClock().sleep(until: target)
            } else { try await Task.sleep(for: .milliseconds(10)) }
        }
        await recognizer.finish()
        drain()
        if let error = recognizer.lastError { fail(error) }
        let finals = rows.filter { $0["final"] as? Bool == true }
        try writeJSON(["stage": stage, "locale": locale, "chunk_ms": chunkMs, "realtime": realtime,
                       "vad_finalizations": vadFinalizations, "results": rows,
                       "final_text": finals.compactMap { $0["text"] as? String }.joined(separator: " "),
                       "elapsed_ms": elapsedMs(started)])
        print("{\"stage\":\"asr\",\"final_results\":\(finals.count),\"vad_finalizations\":\(vadFinalizations)}")
    case "mt":
        let source = Locale.Language(identifier: direction.source.translationIdentifier)
        let target = Locale.Language(identifier: direction.target.translationIdentifier)
        try await AppleTranslator.checkInstalled(source: source, target: target)
        let translator = AppleTranslator(source: source, target: target)
        var rows: [[String: Any]] = []
        for item in try loadItems() {
            let begin = ContinuousClock.now
            let candidate = try await translator.translate(TranslationRequest(unitID: UnitID(item.id), context: [],
                glossary: [:], uncommittedSource: item.text))
            rows.append(["id": item.id, "source": item.text, "text": candidate.text,
                         "elapsed_ms": elapsedMs(begin), "complete": candidate.complete])
        }
        try writeJSON(["stage": stage, "direction": direction.rawValue, "items": rows, "elapsed_ms": elapsedMs(started)])
        print("{\"stage\":\"mt\",\"items\":\(rows.count)}")
    case "tts":
        let synth = SystemVoiceSynthesizer(language: direction.target.speechLocaleIdentifier)
        let voice = PreparedVoice(speakerID: SpeakerID("diagnostic"), profileVersion: 1, handle: 0)
        let combined = try WAVStreamWriter(url: outURL.appendingPathComponent("speech.wav"), sampleRate: 24_000, channels: 1)
        var rows: [[String: Any]] = []
        for item in try loadItems() {
            let begin = ContinuousClock.now
            var firstMs: Double?
            var frames = 0
            var resampler: StreamingResampler?
            let writer = try WAVStreamWriter(url: outURL.appendingPathComponent("\(item.id).wav"), sampleRate: 24_000, channels: 1)
            for try await audio in synth.synthesize(unit: UnitID(item.id), text: item.text, voice: voice) {
                if firstMs == nil { firstMs = elapsedMs(begin) }
                if resampler == nil { resampler = StreamingResampler(inputRate: audio.format.sampleRate, outputRate: 24_000) }
                let mono = WAVAudio(sampleRate: audio.format.sampleRate, channels: audio.format.channelCount, samples: audio.samples).mono()
                let pcm = resampler!.process(mono)
                writer.append(pcm); combined.append(pcm); frames += pcm.count
            }
            let last = resampler?.flush() ?? []
            writer.append(last); combined.append(last); frames += last.count
            try writer.close()
            combined.append([Float](repeating: 0, count: 4_800))
            rows.append(["id": item.id, "text": item.text, "frames": frames,
                         "first_chunk_ms": firstMs ?? -1, "elapsed_ms": elapsedMs(begin)])
        }
        try combined.close()
        try writeJSON(["stage": stage, "direction": direction.rawValue, "items": rows, "sample_rate": 24_000,
                       "preserves_speaker_identity": false, "elapsed_ms": elapsedMs(started)])
        print("{\"stage\":\"tts\",\"items\":\(rows.count)}")
    case "resample":
        let wav = try WAV.read(inputURL)
        let mono = wav.mono()
        let outputRate = Int(value("--rate") ?? "16000") ?? 16_000
        guard (8_000...96_000).contains(outputRate) else { fail("invalid --rate") }
        let full = StreamingResampler.resample(mono, from: wav.sampleRate, to: outputRate)
        let streamed = StreamingResampler(inputRate: wav.sampleRate, outputRate: outputRate)
        var chunks: [Float] = []
        for offset in stride(from: 0, to: mono.count, by: 960) {
            chunks += streamed.process(Array(mono[offset..<min(offset+960,mono.count)]))
        }
        chunks += streamed.flush()
        let maxDifference = zip(full, chunks).map { abs($0-$1) }.max() ?? 0
        try WAV.write(WAVAudio(sampleRate: outputRate, channels: 1, samples: chunks), to: outURL.appendingPathComponent("resampled.wav"))
        try writeJSON(["stage": stage, "input_rate": wav.sampleRate, "output_rate": outputRate,
                       "full_frames": full.count, "streamed_frames": chunks.count, "max_sample_difference": maxDifference])
        print("{\"stage\":\"resample\",\"max_sample_difference\":\(maxDifference)}")
    case "speakers":
        // DSP speaker tracking only: labels, never features or audio.
        var samples = try WAV.read(inputURL).mono()
        let source = try WAV.read(inputURL)
        if source.sampleRate != 48_000 { samples = StreamingResampler.resample(samples, from: source.sampleRate, to: 48_000) }
        samples += [Float](repeating: 0, count: 48_000)
        let resampler = StreamingResampler(inputRate: 48_000, outputRate: 16_000)
        var vad = EnergyVAD()
        // Research overrides of the tracker's thresholds.
        var config = VoiceprintSpeakerTracker.Config()
        if let v = value("--new-distance").flatMap(Double.init) { config.newSpeakerDistance = v }
        if let v = value("--immediate-distance").flatMap(Double.init) { config.immediateDistance = v }
        if let v = value("--confirmed-distance").flatMap(Double.init) { config.confirmedDistance = v }
        if let v = value("--min-new-frames").flatMap(Int.init) { config.minNewSpeakerFrames = v }
        if let v = value("--candidate-seconds").flatMap(Double.init) { config.candidateSamples = Int64(v * 48_000) }
        var tracker = VoiceprintSpeakerTracker(config: config)
        var rows: [[String: Any]] = []
        let wall = ContinuousClock.now
        for offset in stride(from: 0, to: samples.count, by: 960) {
            let end = min(offset + 960, samples.count)
            let frame = AudioFrame16k(span: SourceSpan(start: Int64(offset), end: Int64(end)),
                                      samples: resampler.process(Array(samples[offset..<end])))
            for event in tracker.process(frame, vad: vad.process(frame)) {
                guard case .assign(let speaker, let span) = event else { continue }
                rows.append(["speaker": speaker.rawValue, "start_s": Double(span.start) / 48_000, "end_s": Double(span.end) / 48_000])
            }
        }
        let counts = Dictionary(grouping: rows, by: { $0["speaker"] as! String }).mapValues(\.count)
        try writeJSON(["stage": stage, "segments": rows, "speakers": counts, "processing_ms": elapsedMs(wall),
                       "source_seconds": Double(samples.count) / 48_000])
        print("{\"stage\":\"speakers\",\"labels\":\"\(rows.map { ($0["speaker"] as! String).dropFirst() }.joined())\",\"processing_ms\":\(Int(elapsedMs(wall)))}")
    default: fail("unknown stage \(stage)")
    }
} catch { fail(String(describing: error)) }
