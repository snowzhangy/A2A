// Isolated research probes. No taps, playout policy, or audio-device output.
// Raw text/audio outputs are explicit files; keep them under .local/ or restricted storage.
import A2ACore
import A2AMedia
import A2AModels
import A2ALegacyModels
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a2a-stage-probe: \(message)\n".utf8))
    exit(2)
}
let args = CommandLine.arguments
guard args.count > 1 else { fail("pass asr, mt, tts, resample, speakers, or background with --input FILE --out DIR") }
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
    case "background":
        let wav = try WAV.read(inputURL)
        let samples = StreamingResampler.resample(wav.mono(), from: wav.sampleRate, to: 48_000)
        let separator = try BackgroundSeparator()
        let writer = try WAVStreamWriter(url: outURL.appendingPathComponent("background.wav"), sampleRate: 48_000, channels: 1)
        for offset in stride(from: 0, to: samples.count, by: 960) {
            writer.append(try separator.process(Array(samples[offset..<min(samples.count, offset + 960)])))
        }
        try writer.close()
        try writeJSON(["stage": stage, "frames": samples.count, "sample_rate": 48_000,
                       "elapsed_ms": elapsedMs(started), "scope": "causal 20ms blocks; no device playback"])
        print("{\"stage\":\"background\",\"frames\":\(samples.count)}")
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
        if let v = value("--max-speakers").flatMap(Int.init) { config.maxSpeakers = v }
        if let v = value("--stale-segments").flatMap(Int.init) { config.staleSegments = v }
        var tracker = VoiceprintSpeakerTracker(config: config)
        // Attribution as the reducer sees it: the direct-audio pieces a live session commits,
        // and which of them get a voice at all. `carry` counts the pieces fix 1 recovers —
        // unlabeled pieces inside a stretch the tracker already labeled (no pause since).
        var segments = AudioSegmentPolicy()
        let carrySeconds = value("--speaker-carry").flatMap(Double.init) ?? 6
        let carryAfterPause = value("--speaker-carry-after-pause").flatMap(Double.init) ?? 3
        let carryMaxPiece = value("--speaker-carry-max-piece").flatMap(Double.init) ?? 1
        var lastLabelEnd: Int64?
        var lastLabelSpeaker: SpeakerID?
        var lastPause: Int64?
        var pieceRows: [[String: Any]] = []
        var rows: [[String: Any]] = []
        var labelSpans: [(speaker: SpeakerID, start: Int64, end: Int64)] = []
        // Identity check: for a piece the tracker did label, would the carry have named a
        // different actor? Each conflict is a line the carry could voice with the wrong voice.
        var carryConflicts = 0
        let wall = ContinuousClock.now
        for offset in stride(from: 0, to: samples.count, by: 960) {
            let end = min(offset + 960, samples.count)
            let frame = AudioFrame16k(span: SourceSpan(start: Int64(offset), end: Int64(end)),
                                      samples: resampler.process(Array(samples[offset..<end])))
            let vadEvents = vad.process(frame)
            for event in vadEvents {
                if case .speechEnd(let s) = event { lastPause = max(lastPause ?? 0, s) }
            }
            for event in tracker.process(frame, vad: vadEvents) {
                guard case .assign(let speaker, let span) = event else { continue }
                if lastLabelEnd == nil || span.end >= lastLabelEnd! { (lastLabelSpeaker, lastLabelEnd) = (speaker, span.end) }
                labelSpans.append((speaker, span.start, span.end))
                rows.append(["speaker": speaker.rawValue, "start_s": Double(span.start) / 48_000, "end_s": Double(span.end) / 48_000])
            }
            for piece in segments.process(events: vadEvents, frontier: frame.span.end) {
                let covered = labelSpans.reduce(Int64(0)) { total, label in
                    total + max(0, min(label.end, piece.end) - max(label.start, piece.start))
                }
                let labeled = Double(covered) / Double(max(1, piece.count)) > 0.5
                var carried = false
                if let end = lastLabelEnd, let who = lastLabelSpeaker, piece.start >= end {
                    let gap = Double(piece.start - end) / 48_000
                    let pauseBefore = lastPause != nil && lastPause! >= end
                    if !pauseBefore, gap <= carrySeconds { carried = true }
                    if pauseBefore, Double(piece.count) / 48_000 <= carryMaxPiece, gap <= carryAfterPause { carried = true }
                    if labeled, carried,
                       let own = labelSpans.filter({ $0.start < piece.end && $0.end > piece.start })
                           .max(by: { min($0.end, piece.end) - max($0.start, piece.start)
                               < min($1.end, piece.end) - max($1.start, piece.start) }),
                       own.speaker != who { carryConflicts += 1 }
                }
                var row: [String: Any] = ["start_s": Double(piece.start) / 48_000, "end_s": Double(piece.end) / 48_000,
                                          "labeled": labeled, "carried": carried]
                if carried, let who = lastLabelSpeaker { row["carried_speaker"] = who.rawValue }
                pieceRows.append(row)
            }
        }
        let counts = Dictionary(grouping: rows, by: { $0["speaker"] as! String }).mapValues(\.count)
        let labeledPieces = pieceRows.filter { $0["labeled"] as! Bool }.count
        let carriedPieces = pieceRows.filter { $0["carried"] as! Bool }.count
        try writeJSON(["stage": stage, "segments": rows, "speakers": counts, "processing_ms": elapsedMs(wall),
                       "source_seconds": Double(samples.count) / 48_000,
                       "pieces": pieceRows.count, "pieces_labeled": labeledPieces,
                       "pieces_carried": carriedPieces, "speaker_carry_seconds": carrySeconds,
                       "speaker_carry_after_pause_seconds": carryAfterPause,
                       "speaker_carry_max_piece_seconds": carryMaxPiece,
                       "carry_conflicts": carryConflicts,
                       "pieces_unvoiced": pieceRows.count - labeledPieces - carriedPieces, "piece_rows": pieceRows])
        print("{\"stage\":\"speakers\",\"labels\":\"\(rows.map { ($0["speaker"] as! String).dropFirst() }.joined())\","
              + "\"pieces\":\(pieceRows.count),\"labeled\":\(labeledPieces),\"carried\":\(carriedPieces),"
              + "\"conflicts\":\(carryConflicts),"
              + "\"unvoiced\":\(pieceRows.count - labeledPieces - carriedPieces),\"processing_ms\":\(Int(elapsedMs(wall)))}")
    default: fail("unknown stage \(stage)")
    }
} catch { fail(String(describing: error)) }
