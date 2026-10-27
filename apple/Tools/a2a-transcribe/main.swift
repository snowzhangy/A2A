// a2a-transcribe: transcript of a WAV file with an installed SpeechTranscriber locale.
// QA helper: back-transcribes a run's translated audio, so the check covers what was
// played and not only the trace text. Plumbing evidence, never a model-quality metric.
//
//   a2a-transcribe --input x.wav [--locale en-US]
//
// Prints JSON: {"locale", "duration_s", "segments": [{"start_s", "end_s", "text"}], "text"}.

import A2ACore
import A2AMedia
import A2AModels
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("a2a-transcribe: \(message)\n".utf8))
    exit(2)
}

let args = CommandLine.arguments
func value(_ name: String) -> String? {
    args.firstIndex(of: name).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
}
guard let input = value("--input") else { fail("usage: a2a-transcribe --input x.wav [--locale en-US]") }
let locale = Locale(identifier: value("--locale") ?? "en-US")
let rate = Int(canonicalSampleRate)

do {
    let wav = try WAV.read(URL(fileURLWithPath: input))
    var mono = wav.mono()
    if wav.sampleRate != rate { mono = StreamingResampler.resample(mono, from: wav.sampleRate, to: rate) }
    let low = StreamingResampler.resample(mono, from: rate, to: 16_000)
    let recognizer = try await AppleSpeechRecognizer(locale: locale)
    // 100 ms buffers fed at about 10x real time. The adapter's input stream keeps only the
    // newest 512 buffers, so pushing a whole file at once would silently drop its beginning;
    // much larger buffers are rejected ("timestamp overlaps or precedes prior audio input").
    let chunk = 1_600
    let scale = Int64(rate / 16_000)
    var position = 0
    while position < low.count {
        let n = min(chunk, low.count - position)
        let span = SourceSpan(start: Int64(position) * scale, end: Int64(position + n) * scale)
        recognizer.accept(AudioFrame16k(span: span, samples: Array(low[position..<position + n])))
        position += n
        try await Task.sleep(nanoseconds: 10_000_000)
    }
    await recognizer.finish()
    if let error = recognizer.lastError { fail(error) }
    let finals = recognizer.drainHypotheses().filter(\.isFinal)
    let segments: [[String: Any]] = finals.map { h in
        let spans = h.tokens.compactMap(\.span)
        return ["start_s": Double(spans.map(\.start).min() ?? 0) / Double(rate),
                "end_s": Double(spans.map(\.end).max() ?? 0) / Double(rate),
                "text": h.tokens.map(\.text).joined()]
    }
    let text = segments.compactMap { $0["text"] as? String }.joined(separator: " ")
    let report: [String: Any] = ["locale": locale.identifier, "duration_s": Double(mono.count) / Double(rate),
                                 "segments": segments, "text": text]
    let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
    fail("\(error)")
}
